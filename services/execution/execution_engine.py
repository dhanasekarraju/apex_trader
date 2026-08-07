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

    def bind(self, portfolio: PortfolioManager, market_data: MarketDataService) -> None:
        self._portfolio = portfolio
        self._market_data = market_data

    def refresh_broker(self) -> None:
        self._broker = get_broker()

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

        blocked = await self._pre_execution_block(req)
        if blocked:
            return blocked

        if req.client_order_id:
            existing = await self._trades.get_by_client_id(req.client_order_id)
            if existing and existing.status in ("filled", "sl_placed"):
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
        if entry.status in (OrderStatus.FAILED, OrderStatus.REJECTED):
            return entry
        if entry.broker_order_id:
            entry = await with_timeout(
                self._broker.reconcile_order(entry.broker_order_id, req),
                seconds=cfg.external_api_timeout_sec,
                label="kite_reconcile",
            )
        if entry.status not in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            return entry
        return await self._attach_stop_loss(req, entry, market_price, cfg)

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
        if sl.status in (OrderStatus.FAILED, OrderStatus.REJECTED):
            audit("stop_loss_failed", symbol=req.symbol, reason=sl.message)
            if hasattr(self._broker, "flatten_symbol"):
                await self._broker.flatten_symbol(req.symbol)
            else:
                await self._broker.flatten_all()
            if cfg.trading_mode == "live":
                return OrderResult(
                    req.client_order_id,
                    entry.broker_order_id,
                    OrderStatus.REJECTED,
                    0,
                    0,
                    0,
                    f"Stop-loss failed — entry flattened: {sl.message}",
                )
            return OrderResult(
                req.client_order_id,
                entry.broker_order_id,
                OrderStatus.REJECTED,
                0,
                0,
                0,
                f"Stop-loss failed — entry flattened: {sl.message}",
            )
        entry.raw["stop_order_id"] = sl.broker_order_id
        entry.message = f"{entry.message}; SL {sl.broker_order_id}"
        audit("stop_loss_attached", symbol=req.symbol, stop_order=sl.broker_order_id)
        return entry

    async def place_exit(
        self,
        *,
        symbol: str,
        qty: float,
        reason: str,
        market_price: float,
        strategy: str = "exit",
    ) -> OrderResult:
        """Single entry point for all sell/exit orders."""
        from services.execution.idempotency import make_order_id

        cfg = get_settings()

        if cfg.trading_mode == "shadow":
            return OrderResult(
                "", "", OrderStatus.REJECTED, 0, 0, 0, "Shadow mode — no exits on broker",
            )

        sell_qty = max(1, int(qty))
        client_id = make_order_id(symbol, strategy, side="sell")
        req = OrderRequest(
            symbol=symbol.upper(),
            side="short",
            qty=sell_qty,
            order_type=OrderType.MARKET,
            strategy=strategy,
            client_order_id=f"{client_id}-{reason}",
            metadata={"exit_reason": reason},
        )
        blocked = await self._pre_execution_block(req)
        if blocked:
            return blocked

        trade_log(
            symbol=symbol,
            strategy=strategy,
            action="SELL",
            result="submit",
            reason=reason,
            qty=sell_qty,
        )

        if cfg.trading_mode == "live":
            result = await self._place_with_retry(req, market_price, cfg)
            if result.broker_order_id:
                result = await with_timeout(
                    self._broker.reconcile_order(result.broker_order_id, req),
                    seconds=cfg.external_api_timeout_sec,
                    label="exit_reconcile",
                )
        else:
            result = await self._place_with_retry(req, market_price, cfg)

        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        pos = next(
            (p for p in (self._portfolio.state.positions if self._portfolio else []) if p.symbol.upper() == symbol.upper()),
            None,
        )
        await crce.record(
            event_type=EventType.ORDER_EXITED,
            action="PLACE_EXIT",
            symbol=symbol,
            decision="EXECUTED" if result.status.value in ("filled", "partial") else "FAILED",
            reason=reason,
            portfolio=self._portfolio,
            exit_price=result.avg_price or market_price,
            exit_reason=reason,
            expected_stop_loss=pos.stop_loss if pos else None,
            qty=sell_qty,
        )
        return result

    async def activate_kill_switch(self) -> dict:
        """Global emergency: halt, cancel, flatten, persist with PnL accounting."""
        if self._portfolio:
            self._portfolio.emergency_shutdown()
            await self._portfolio.persist()
        positions = list(self._portfolio.state.positions) if self._portfolio else []
        cancelled = await self.cancel_all()
        flattened = await self.flatten_all()
        if self._portfolio:
            for pos in positions:
                exit_price = await self._resolve_flatten_price(pos.symbol, pos.entry)
                pnl = (exit_price - pos.entry) * pos.qty
                await self._portfolio.record_exit(
                    symbol=pos.symbol,
                    exit_price=exit_price,
                    exit_reason="kill_switch_flatten",
                    pnl=pnl,
                )
            if self._portfolio.state.positions:
                await self._portfolio.clear_after_flatten()
            await self._portfolio.persist()
        audit("kill_switch_activated", cancelled=cancelled, flattened=flattened)
        return {
            "halted": True,
            "cancelled": cancelled,
            "flattened": flattened,
        }

    async def _resolve_flatten_price(self, symbol: str, fallback: float) -> float:
        if self._market_data is None:
            return fallback
        try:
            if self._market_data.has_real_data_configured():
                ltps = await self._market_data.fetch_ltps([symbol.upper()])
                price = ltps.get(symbol.upper(), 0.0)
                if price > 0:
                    return price
            df = self._market_data.synthetic_ohlcv(symbol, bars=3)
            return float(df["close"].iloc[-1])
        except Exception:
            return fallback

    async def eod_square_off(self, reason: str = "mis_eod_square_off") -> dict:
        """Flatten open book at MIS cutoff without latching the kill switch.

        Stops new entries for the day via autonomous stop (caller), cancels
        open orders, flattens positions, and accounts exits in the portfolio.
        """
        positions = list(self._portfolio.state.positions) if self._portfolio else []
        cancelled = await self.cancel_all()
        flattened = await self.flatten_all()
        accounted = 0
        if self._portfolio:
            for pos in positions:
                exit_price = await self._resolve_flatten_price(pos.symbol, pos.entry)
                pnl = (exit_price - pos.entry) * pos.qty
                await self._portfolio.record_exit(
                    symbol=pos.symbol,
                    exit_price=exit_price,
                    exit_reason=reason,
                    pnl=pnl,
                )
                accounted += 1
            if self._portfolio.state.positions:
                await self._portfolio.clear_after_flatten()
            await self._portfolio.persist()
        audit(
            "mis_eod_square_off",
            cancelled=cancelled,
            flattened=flattened,
            accounted=accounted,
            reason=reason,
        )
        return {
            "ok": True,
            "cancelled": cancelled,
            "flattened": flattened,
            "accounted": accounted,
            "reason": reason,
        }

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
