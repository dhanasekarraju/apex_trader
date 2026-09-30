"""Central execution engine — sole path for order placement and lifecycle."""

from __future__ import annotations

import asyncio

from services.brokers.base import OrderRequest, OrderResult, OrderStatus, OrderType
from services.brokers.factory import get_broker
from services.execution.circuit_breaker import ApiCircuitBreaker
from services.execution.dead_letter import DeadLetterQueue
from services.execution.idempotency_store import claim_order_id, release_order_id, seed_order_id
from services.execution.live_gate import LiveSafetyGate
from services.execution.reconciliation import reconcile_on_startup
from services.control.reconciliation_state import is_reconciliation_degraded
from services.market_data.service import MarketDataService
from services.portfolio.manager import PortfolioManager
from services.shadow.engine import ShadowEngine
from services.trades.repository import TradeRepository
from shared.config import Settings, get_settings
from shared.logging import audit, trade_log
from shared.timeout import with_timeout


class ExecutionEngine:
    """
    Single gate for all order execution.
    PAPER  → simulated broker + SL attach
    SHADOW → simulated fills only (never Kite)
    LIVE   → real broker with reconciliation + SL-M
    """

    max_retries = 3
    retry_base_sec = 1.0

    def __init__(
        self,
        *,
        portfolio: PortfolioManager | None = None,
        market_data: MarketDataService | None = None,
    ) -> None:
        self.cfg = get_settings()
        self._broker = get_broker()
        self._shadow = ShadowEngine()
        self._portfolio = portfolio
        self._market_data = market_data
        self._trades = TradeRepository()
        self._dlq = DeadLetterQueue()
        self._circuit = ApiCircuitBreaker()
        self.order_lock = asyncio.Lock()

    def bind(self, portfolio: PortfolioManager, market_data: MarketDataService) -> None:
        self._portfolio = portfolio
        self._market_data = market_data

    def refresh_broker(self) -> None:
        from services.brokers.factory import reset_broker_cache

        reset_broker_cache()
        self._broker = get_broker()
        self.cfg = get_settings()

    async def connect(self) -> bool:
        if self.cfg.trading_mode == "shadow":
            return True
        return await self._broker.connect()

    async def disconnect(self) -> None:
        await self.shutdown()

    async def recover(self) -> dict:
        """Rebuild idempotency, reconcile broker positions, resume tracking."""
        open_rows = await self._trades.open_trades()
        for row in open_rows:
            await seed_order_id(row.client_order_id)

        broker_ok = await self.connect()
        reconcile_report: dict = {}
        if self._portfolio is not None:
            reconcile_report = await reconcile_on_startup(
                broker=self._broker,
                portfolio=self._portfolio,
                trades=self._trades,
                trading_mode=self.cfg.trading_mode,
            )

        report = {
            "open_trades": len(open_rows),
            "broker_connected": broker_ok,
            "mode": self.cfg.trading_mode,
            **reconcile_report,
        }
        audit("execution_recovery", **report)
        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        await crce.record(
            event_type=EventType.RECONCILIATION_RUN,
            action="RECONCILE_PORTFOLIO",
            decision="EXECUTED",
            reason=str(reconcile_report.get("reconciliation_status", "OK")),
            portfolio=self._portfolio,
            **{k: v for k, v in reconcile_report.items() if isinstance(v, (str, int, float, bool))},
        )
        return report

    async def retry_reconciliation(self) -> dict:
        """Retry broker reconciliation when in DEGRADED state."""
        if not await is_reconciliation_degraded():
            return {"skipped": "not_degraded"}
        if self._portfolio is None:
            return {"skipped": "no_portfolio"}
        report = await reconcile_on_startup(
            broker=self._broker,
            portfolio=self._portfolio,
            trades=self._trades,
            trading_mode=self.cfg.trading_mode,
        )
        audit("reconciliation_retry", **report)
        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        await crce.record(
            event_type=EventType.RECONCILIATION_RUN,
            action="RECONCILE_RETRY",
            decision="EXECUTED",
            reason=str(report.get("reconciliation_status", report.get("skipped", "OK"))),
            portfolio=self._portfolio,
        )
        return report

    async def shutdown(self) -> None:
        if hasattr(self._broker, "disconnect"):
            await self._broker.disconnect()

    async def place_order(self, req: OrderRequest, market_price: float) -> OrderResult:
        """Single entry point for order placement — executes risk-approved orders only."""
        cfg = get_settings()
        if not req.stop_price or req.stop_price <= 0 or req.stop_price >= market_price or req.qty <= 0:
            return OrderResult(req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0, "Valid protective stop and quantity required before entry")

        blocked = await self._pre_execution_block(req)
        if blocked:
            return blocked

        if req.client_order_id:
            existing = await self._trades.get_by_client_id(req.client_order_id)
            if existing and existing.status in ("pending", "submitted", "unknown", "filled", "sl_placed"):
                audit("duplicate_order_blocked", id=req.client_order_id)
                return OrderResult(
                    req.client_order_id,
                    existing.broker_order_id or "",
                    OrderStatus.REJECTED,
                    0,
                    0,
                    0,
                    "Duplicate order blocked (idempotent)",
                )

            if not await claim_order_id(req.client_order_id):
                audit("duplicate_order_blocked", id=req.client_order_id)
                return OrderResult(
                    req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0,
                    "Duplicate order blocked",
                )

        cfg = get_settings()
        mode = cfg.trading_mode

        await self._trades.create_pending(
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            strategy=req.strategy,
            side=req.side,
            qty=req.qty,
            stop_loss=req.stop_price,
            take_profit=req.take_profit,
            trading_mode=mode,
        )
        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        await crce.record(
            event_type=EventType.ORDER_PLACED,
            action="PLACE_ORDER",
            symbol=req.symbol,
            decision="EXECUTED",
            reason="submitted",
            portfolio=self._portfolio,
            client_order_id=req.client_order_id,
            qty=req.qty,
        )
        trade_log(
            symbol=req.symbol,
            strategy=req.strategy,
            action="SUBMIT",
            result="pending",
            mode=mode,
            client_order_id=req.client_order_id,
        )

        if mode == "shadow":
            result = self._shadow.simulate(req, market_price)
            await self._record_result(req, result, shadow=True)
            return result

        if mode == "paper":
            result = await self._submit_with_stop(req, market_price, cfg)
            await self._record_result(req, result)
            if result.status in (OrderStatus.FAILED, OrderStatus.REJECTED):
                await release_order_id(req.client_order_id)
            return result

        if mode == "live":
            ok, blockers = await self._live_allowed()
            if not ok:
                result = OrderResult(
                    req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0,
                    "Live blocked: " + "; ".join(blockers[:3]),
                )
                await self._record_result(req, result)
                await release_order_id(req.client_order_id)
                return result
            result = await self._submit_live(req, market_price, cfg)
            await self._record_result(req, result)
            if result.status in (OrderStatus.FAILED, OrderStatus.REJECTED):
                await release_order_id(req.client_order_id)
            return result

        result = await self._place_with_retry(req, market_price, cfg)
        await self._record_result(req, result)
        if result.status in (OrderStatus.FAILED, OrderStatus.REJECTED):
            await release_order_id(req.client_order_id)
        return result

    async def submit(self, req: OrderRequest, market_price: float) -> OrderResult:
        """Backward-compatible alias."""
        return await self.place_order(req, market_price)

    async def _pre_execution_block(self, req: OrderRequest) -> OrderResult | None:
        from services.control.halt import is_emergency_halt
        from services.autonomous.state import operator_paused

        if await operator_paused():
            return OrderResult(req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0, "New entries paused by operator or pause state unavailable")

        if await is_reconciliation_degraded():
            return OrderResult(
                req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0,
                "Reconciliation degraded — trading paused until broker sync recovers",
            )
        if await is_emergency_halt():
            return OrderResult(
                req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0,
                "EMERGENCY_HALT active — execution blocked",
            )
        if self._portfolio and self._portfolio.is_trading_halted():
            return OrderResult(
                req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0,
                "Kill switch active — execution blocked",
            )
        if get_settings().trading_mode == "live":
            pending = await self._trades.open_trades()
            if any(r.status in ("pending", "submitted", "unknown") for r in pending):
                return OrderResult(req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0, "Unresolved broker order; new entries blocked")
        if self._circuit.is_open():
            remaining = self._circuit.pause_remaining_sec()
            return OrderResult(
                req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0,
                f"API circuit breaker open — paused {remaining}s",
            )
        return None

    async def _record_result(
        self,
        req: OrderRequest,
        result: OrderResult,
        *,
        shadow: bool = False,
    ) -> None:
        status_map = {
            OrderStatus.FILLED: "filled",
            OrderStatus.PARTIAL: "filled",
            OrderStatus.SUBMITTED: "submitted",
            OrderStatus.REJECTED: "rejected",
            OrderStatus.FAILED: "failed",
            OrderStatus.CANCELLED: "cancelled",
        }
        status = status_map.get(result.status, "submitted")
        if result.raw.get("stop_order_id"):
            status = "sl_placed"
        if result.raw.get("unknown"):
            status = "unknown"

        if result.status in (OrderStatus.FAILED, OrderStatus.REJECTED):
            await self._dlq.enqueue(
                client_order_id=req.client_order_id,
                symbol=req.symbol,
                strategy=req.strategy,
                side=req.side,
                qty=req.qty,
                trading_mode=get_settings().trading_mode,
                failure_reason=result.message,
                payload={
                    "stop_loss": req.stop_price,
                    "take_profit": req.take_profit,
                    "broker_order_id": result.broker_order_id,
                },
            )

        await self._trades.update_status(
            req.client_order_id,
            status=status,
            entry_price=result.avg_price or None,
            broker_order_id=result.broker_order_id or None,
            stop_order_id=result.raw.get("stop_order_id"),
            message=result.message,
        )
        trade_log(
            symbol=req.symbol,
            strategy=req.strategy,
            action="EXECUTE",
            result=status,
            shadow=shadow,
            broker_order_id=result.broker_order_id,
            message=result.message,
        )
        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        if result.status in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            et = EventType.ORDER_FILLED
            decision = "EXECUTED"
        elif result.status in (OrderStatus.REJECTED, OrderStatus.FAILED):
            et = EventType.ORDER_REJECTED
            decision = "FAILED"
        else:
            et = EventType.ORDER_PLACED
            decision = "EXECUTED"
        await crce.record(
            event_type=et,
            action="PLACE_ORDER",
            symbol=req.symbol,
            decision=decision,
            reason=result.message,
            portfolio=self._portfolio,
            client_order_id=req.client_order_id,
            qty=result.filled_qty or req.qty,
            broker_order_id=result.broker_order_id,
            metadata={
                "broker_filled_qty": result.filled_qty,
                "internal_qty": req.qty,
                "stop_order_id": result.raw.get("stop_order_id"),
            },
        )

    async def _live_allowed(self) -> tuple[bool, list[str]]:
        if self._portfolio is None or self._market_data is None:
            return False, ["Execution engine not bound to portfolio/market data"]
        return await LiveSafetyGate.check(
            market_data=self._market_data,
            broker=self._broker,
            portfolio=self._portfolio,
        )

    async def _submit_live(
        self,
        req: OrderRequest,
        market_price: float,
        cfg: Settings,
    ) -> OrderResult:
        entry = await self._place_with_retry(req, market_price, cfg)
        await self._record_result(req, entry)
        if entry.status in (OrderStatus.REJECTED, OrderStatus.FAILED) or not entry.broker_order_id:
            return entry
        try:
            entry = await self._broker.reconcile_order(entry.broker_order_id, req, timeout_sec=cfg.external_api_timeout_sec)
            if entry.status not in (OrderStatus.FILLED, OrderStatus.REJECTED):
                await self._broker.cancel_order(entry.broker_order_id)
                status = await self._broker.fetch_order_status(entry.broker_order_id)
                if status.get("status") not in ("COMPLETE", "CANCELLED", "REJECTED"):
                    await self._unknown(req, "Entry cancellation/fill outcome unknown")
                    entry.raw["unknown"] = True
                    if entry.filled_qty > 0:
                        entry = await self._attach_stop_loss(req, entry, market_price, cfg)
                    return entry
                entry.filled_qty = float(status.get("filled_quantity") or 0)
                entry.avg_price = float(status.get("average_price") or 0)
                entry.status = OrderStatus.FILLED if entry.filled_qty else OrderStatus.REJECTED
            if entry.filled_qty > 0:
                return await self._attach_stop_loss(req, entry, market_price, cfg)
            return entry
        except (Exception, asyncio.CancelledError) as exc:
            await self._unknown(req, f"Fill/protection outcome unknown: {exc}")
            entry.raw["unknown"] = True
            entry.status = OrderStatus.SUBMITTED
            return entry

    async def _unknown(self, req: OrderRequest, reason: str) -> None:
        from services.control.reconciliation_state import set_reconciliation_degraded
        await set_reconciliation_degraded(reason)
        if self._portfolio:
            self._portfolio.emergency_shutdown()
            await self._portfolio.persist()
        await self._trades.update_status(req.client_order_id, status="unknown", message=reason)

    async def _submit_with_stop(
        self,
        req: OrderRequest,
        market_price: float,
        cfg: Settings,
    ) -> OrderResult:
        entry = await self._place_with_retry(req, market_price, cfg)
        if entry.status not in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            return entry
        if not req.stop_price:
            return OrderResult(
                req.client_order_id, entry.broker_order_id, OrderStatus.REJECTED,
                0, 0, 0, "Stop-loss price required",
            )
        return await self._attach_stop_loss(req, entry, market_price, cfg)

    async def _place_with_retry(
        self,
        req: OrderRequest,
        market_price: float,
        cfg: Settings,
    ) -> OrderResult:
        if cfg.trading_mode == "live":
            try:
                result = await with_timeout(self._broker.place_order(req, market_price),
                                            seconds=cfg.external_api_timeout_sec, label="broker_submit_once")
                if result.status == OrderStatus.FAILED:
                    result.status = OrderStatus.SUBMITTED
                    result.raw["unknown"] = True
                if result.raw.get("unknown"):
                    await self._unknown(req, result.message)
                return result
            except (Exception, asyncio.CancelledError) as exc:
                await self._unknown(req, f"Submission outcome unknown: {exc}")
                return OrderResult(req.client_order_id, "", OrderStatus.SUBMITTED, 0, 0, 0,
                                   "Submission outcome unknown; reconciliation required", raw={"unknown": True})
        last: OrderResult | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                result = await with_timeout(
                    self._broker.place_order(req, market_price),
                    seconds=cfg.external_api_timeout_sec,
                    label="broker_place_order",
                )
                audit(
                    "order_state",
                    client_order_id=req.client_order_id,
                    status=result.status.value,
                    attempt=attempt,
                )
                if result.status not in (OrderStatus.FAILED,):
                    self._circuit.record_success()
                    return result
                last = result
                self._circuit.record_failure(result.message)
            except Exception as e:
                audit(
                    "order_retry",
                    client_order_id=req.client_order_id,
                    attempt=attempt,
                    error=str(e),
                )
                last = OrderResult(
                    req.client_order_id, "", OrderStatus.FAILED,
                    0, 0, 0, str(e),
                )
                self._circuit.record_failure(str(e))
            if attempt < self.max_retries:
                await asyncio.sleep(self.retry_base_sec * (2 ** (attempt - 1)))
        return last or OrderResult(
            req.client_order_id, "", OrderStatus.FAILED, 0, 0, 0, "Max retries exceeded",
        )

    async def _attach_stop_loss(
        self,
        req: OrderRequest,
        entry: OrderResult,
        market_price: float,
        cfg: Settings,
    ) -> OrderResult:
        sl_req = OrderRequest(
            symbol=req.symbol,
            side=req.side,
            qty=int(entry.filled_qty or req.qty),
            order_type=OrderType.STOP,
            stop_price=req.stop_price,
            take_profit=req.take_profit,
            strategy=req.strategy,
            client_order_id=f"{req.client_order_id}-sl",
            metadata=dict(req.metadata),
        )
        try:
            sl = await with_timeout(
                self._broker.place_stop_loss(sl_req, market_price),
                seconds=cfg.external_api_timeout_sec,
                label="broker_stop_loss",
            )
            self._circuit.record_success()
        except Exception as e:
            self._circuit.record_failure(str(e))
            sl = OrderResult(
                req.client_order_id, "", OrderStatus.FAILED, 0, 0, 0, str(e),
            )
        if sl.broker_order_id:
            entry.raw["stop_order_id"] = sl.broker_order_id
        if cfg.trading_mode == "live" and sl.broker_order_id:
            status = await self._broker.fetch_order_status(sl.broker_order_id)
            from services.execution.protection import stop_problem
            problem = stop_problem(status, symbol=req.symbol, qty=sl_req.qty,
                product=req.metadata.get("product", cfg.kite_product),
                exchange=req.metadata.get("exchange", cfg.kite_exchange))
            if problem:
                sl.status = OrderStatus.FAILED
                sl.message = "Protective stop not confirmed: " + problem
        if sl.status in (OrderStatus.FAILED, OrderStatus.REJECTED) or not sl.broker_order_id:
            await self._unknown(req, f"Unprotected fill: {sl.message}")
            entry.message = "UNPROTECTED FILL — halted; broker review / emergency exit required"
            entry.raw["unknown"] = True
            return entry
        entry.raw["stop_order_id"] = sl.broker_order_id
        entry.message = f"{entry.message}; SL {sl.broker_order_id}"
        audit("stop_loss_attached", symbol=req.symbol, stop_order=sl.broker_order_id)
        return entry

    async def place_exit(self, *, symbol: str, qty: float, reason: str,
                         market_price: float, strategy: str = "exit") -> OrderResult:
        try:
            return await self._place_exit(symbol=symbol, qty=qty, reason=reason,
                                          market_price=market_price, strategy=strategy)
        except (Exception, asyncio.CancelledError) as exc:
            request = OrderRequest(symbol, "short", qty, OrderType.MARKET, client_order_id="")
            await self._unknown(request, f"Exit outcome unknown: {exc}")
            return OrderResult("", "", OrderStatus.SUBMITTED, 0, 0, 0,
                               "Exit unresolved; broker review required", raw={"unknown": True})

    async def _place_exit(self, *, symbol: str, qty: float, reason: str,
                         market_price: float, strategy: str = "exit") -> OrderResult:
        """Reduce exposure even when entry gates halt. Caller holds order_lock."""
        import uuid
        cfg = get_settings()
        pos = next((p for p in self._portfolio.state.positions if p.symbol.upper() == symbol.upper()), None) if self._portfolio else None
        if pos is None or qty <= 0 or qty > pos.qty:
            return OrderResult("", "", OrderStatus.REJECTED, 0, 0, 0, "No matching position/valid exit quantity")
        req = OrderRequest(symbol.upper(), "short", int(qty), OrderType.MARKET,
                           strategy=strategy, client_order_id="exit-" + uuid.uuid4().hex[:15])
        if cfg.trading_mode == "shadow":
            return self._shadow.simulate_exit(req, market_price)
        if cfg.trading_mode == "live":
            rows = await self._trades.open_trades()
            if any(r.symbol == req.symbol and r.side == "short" and r.status in ("pending", "submitted", "unknown") for r in rows):
                return OrderResult(req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0, "Unresolved exit; reconcile before retry")
            await self._trades.create_pending(client_order_id=req.client_order_id, symbol=req.symbol,
                strategy=strategy, side="short", qty=req.qty, stop_loss=None, take_profit=None, trading_mode="live")
            if pos.stop_order_id:
                await self._broker.cancel_order(pos.stop_order_id)
                stop = await self._broker.fetch_order_status(pos.stop_order_id)
                if stop.get("status") not in ("CANCELLED", "COMPLETE", "REJECTED"):
                    await self._unknown(req, "Cannot confirm stop cancellation; exit withheld to prevent double sell")
                    return OrderResult(req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0, "Stop cancellation unknown")
                stop_qty = float(stop.get("filled_quantity") or 0)
                if stop_qty > 0:
                    executed_stop_id = pos.stop_order_id
                    pos.stop_order_id = ""
                    await self._portfolio.persist()
                    await self._unknown(req, "Stop filled during exit cancellation; residual protection requires review")
                    # Do not submit a second sell against an already executed stop.
                    return OrderResult(req.client_order_id, executed_stop_id, OrderStatus.PARTIAL,
                                       min(stop_qty, pos.qty), float(stop.get("average_price") or 0), 0,
                                       "Stop filled during cancellation", raw={"stop_fill": True})
                pos.stop_order_id = ""
            positions = await self._broker.fetch_open_positions()
            matching = [p for p in positions if p["symbol"].upper() == req.symbol]
            if len(matching) != 1 or matching[0].get("side", "long") != "long" or abs(float(matching[0]["qty"]) - pos.qty) > 0.0001:
                await self._unknown(req, "Exit broker quantity mismatch; no sell submitted")
                return OrderResult(req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0, "Exit quantity mismatch")
            req.metadata = {k: matching[0][k] for k in ("product", "exchange") if k in matching[0]}

        result = await self._place_with_retry(req, market_price, cfg)
        if cfg.trading_mode == "live":
            await self._record_result(req, result)
            if result.broker_order_id:
                result = await self._broker.reconcile_order(result.broker_order_id, req, timeout_sec=cfg.external_api_timeout_sec)
                if result.status not in (OrderStatus.FILLED, OrderStatus.REJECTED):
                    await self._broker.cancel_order(result.broker_order_id)
                    status = await self._broker.fetch_order_status(result.broker_order_id)
                    if status.get("status") not in ("CANCELLED", "COMPLETE", "REJECTED"):
                        await self._unknown(req, "Exit still pending; quantity cannot be accounted safely")
                        return OrderResult(req.client_order_id, result.broker_order_id, OrderStatus.SUBMITTED, 0, 0, 0, "Exit unresolved", raw={"unknown": True})
                    result.filled_qty = float(status.get("filled_quantity") or 0)
                    result.avg_price = float(status.get("average_price") or 0)
                    result.status = OrderStatus.PARTIAL if result.filled_qty else OrderStatus.REJECTED
            if result.raw.get("unknown") or result.status == OrderStatus.SUBMITTED:
                await self._unknown(req, "Exit outcome unknown; manual broker reconciliation required")
            else:
                await self._trades.update_status(req.client_order_id, status="closed", exit_price=result.avg_price)
                residual = pos.qty - result.filled_qty
                if residual > 0:
                    protective = OrderRequest(req.symbol, "long", residual, OrderType.MARKET,
                                              stop_price=pos.stop_loss, client_order_id=req.client_order_id + "-r",
                                              metadata=dict(req.metadata))
                    fill = OrderResult(protective.client_order_id, "", OrderStatus.FILLED, residual, pos.entry, 0, "Residual protection")
                    protected = await self._attach_stop_loss(protective, fill, market_price, cfg)
                    pos.stop_order_id = protected.raw.get("stop_order_id", "")
        return result

    async def activate_kill_switch(self) -> dict:
        if self._portfolio:
            self._portfolio.emergency_shutdown()
            await self._portfolio.persist()
        result = await self.eod_square_off("kill_switch_flatten")
        return {**result, "halted": True}

    async def eod_square_off(self, reason: str = "mis_eod_square_off") -> dict:
        """Retain unresolved exposure and only book broker-confirmed fills."""
        async with self.order_lock:
            accounted = 0
            errors = []
            if not self._portfolio:
                return {"ok": False, "flattened": 0, "cancelled": 0, "accounted": 0, "reason": "No portfolio"}
            for pos in list(self._portfolio.state.positions):
                try:
                    prices = await self._market_data.fetch_ltps([pos.symbol]) if self._market_data else {}
                    result = await self.place_exit(symbol=pos.symbol, qty=pos.qty, reason=reason,
                                                   market_price=prices.get(pos.symbol, pos.entry))
                    if result.status in (OrderStatus.FILLED, OrderStatus.PARTIAL) and result.filled_qty > 0 and result.avg_price > 0:
                        await self._portfolio.record_exit(symbol=pos.symbol, qty=result.filled_qty,
                            exit_price=result.avg_price, exit_reason=reason,
                            pnl=(result.avg_price - pos.entry) * result.filled_qty - pos.entry * result.filled_qty * get_settings().estimated_round_trip_cost_bps / 10000)
                        if pos.qty <= 0:
                            row = await self._trades.get_open_by_symbol(pos.symbol)
                            if row:
                                await self._trades.update_status(row.client_order_id, status="closed", exit_price=result.avg_price, exit_reason=reason)
                        accounted += 1
                    else:
                        errors.append(f"{pos.symbol}: {result.message}")
                except Exception as exc:
                    errors.append(f"{pos.symbol}: {exc}")
            remaining = []
            if get_settings().trading_mode == "live":
                unresolved = [r for r in await self._trades.open_trades() if r.status in ("pending", "submitted", "unknown")]
                if unresolved:
                    errors.append("Unresolved order intents remain; account cannot be declared flat")
                try:
                    remaining = await self._broker.fetch_open_positions()
                except Exception as exc:
                    errors.append(f"Broker closure unverified: {exc}")
            ok = not errors and not remaining and not self._portfolio.state.positions
            if not ok:
                self._portfolio.emergency_shutdown()
                await self._portfolio.persist()
            return {"ok": ok, "cancelled": 0, "flattened": accounted, "accounted": accounted,
                    "remaining_positions": remaining, "errors": errors, "reason": reason}

    async def flatten_all(self) -> int:
        if self.cfg.trading_mode == "shadow":
            audit("flatten_skipped_shadow")
            return 0
        audit("flatten_all_requested")
        return await self._broker.flatten_all()

    async def cancel_all(self) -> int:
        if self.cfg.trading_mode == "shadow":
            return 0
        return await self._broker.cancel_all()

    def shadow_report(self) -> dict:
        return self._shadow.weekly_report()

    async def live_blockers(self) -> list[str]:
        _, blockers = await self._live_allowed()
        return blockers

    def circuit_status(self) -> dict:
        return self._circuit.status()

    async def dead_letter_pending(self) -> list[dict]:
        return await self._dlq.pending()


ExecutionRouter = ExecutionEngine
