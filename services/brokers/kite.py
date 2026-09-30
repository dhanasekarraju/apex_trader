"""Zerodha Kite Connect broker adapter — live-ready order lifecycle."""

from __future__ import annotations

import asyncio
import time
from functools import partial

from services.brokers.base import (
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
)
from services.brokers.kite_auth import kite_auth
from shared.config import get_settings
from shared.logging import audit


class KiteBroker(BrokerAdapter):
    name = "kite"
    live_ready = True

    def __init__(self) -> None:
        self.cfg = get_settings()
        self._kite = None
        self._connected = False

    async def connect(self) -> bool:
        token = kite_auth.get_access_token_sync()
        if not self.cfg.kite_api_key or not token:
            self._connected = False
            self._kite = None
            return False
        try:
            from kiteconnect import KiteConnect
            # Always rebuild client so a fresh OAuth token replaces a stale one
            self._kite = KiteConnect(api_key=self.cfg.kite_api_key)
            self._kite.set_access_token(token)
            await asyncio.get_event_loop().run_in_executor(None, self._kite.profile)
            self._connected = True
            return True
        except Exception as e:
            audit("kite_connect_failed", error=str(e))
            self._connected = False
            self._kite = None
            return False

    async def disconnect(self) -> None:
        self._connected = False
        self._kite = None

    async def is_connected(self) -> bool:
        return self._connected and self._kite is not None

    async def place_order(self, req: OrderRequest, market_price: float) -> OrderResult:
        if not self._kite:
            return OrderResult(
                req.client_order_id, "", OrderStatus.FAILED, 0, 0, 0,
                "Kite not connected",
            )
        qty = int(req.qty)
        if qty <= 0:
            return OrderResult(
                req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0,
                "Invalid quantity",
            )
        try:
            params = self._build_params(req, qty)
            order_id = await self._submit_order(params)
            audit("kite_order_placed", order_id=order_id, symbol=req.symbol)
            return OrderResult(
                req.client_order_id, order_id, OrderStatus.SUBMITTED,
                0, market_price, 0, "Submitted to Kite",
                raw={"order_id": order_id},
            )
        except Exception as e:
            audit("kite_order_failed", symbol=req.symbol, error=str(e))
            # A transport failure may occur after acceptance. Never label it rejected.
            return OrderResult(
                req.client_order_id, "", OrderStatus.SUBMITTED, 0, 0, 0,
                f"Submission outcome unknown: {e}", raw={"unknown": True},
            )

    async def reconcile_order(
        self,
        broker_order_id: str,
        req: OrderRequest,
        timeout_sec: float = 30.0,
    ) -> OrderResult:
        if not self._kite:
            return OrderResult(
                req.client_order_id, broker_order_id, OrderStatus.FAILED,
                0, 0, 0, "Kite not connected",
            )
        loop = asyncio.get_event_loop()
        deadline = time.monotonic() + timeout_sec
        last_partial: OrderResult | None = None
        while time.monotonic() < deadline:
            try:
                history = await loop.run_in_executor(
                    None, partial(self._kite.order_history, broker_order_id)
                )
                if history:
                    last = history[-1]
                    status = str(last.get("status", "") or "").upper()
                    filled = float(last.get("filled_quantity", 0) or 0)
                    avg = float(last.get("average_price", 0) or 0)
                    qty_req = float(last.get("quantity", req.qty) or req.qty)
                    pending = float(
                        last.get("pending_quantity", max(0.0, qty_req - filled)) or 0
                    )
                    if status == "COMPLETE" or (filled > 0 and pending <= 0):
                        return OrderResult(
                            req.client_order_id,
                            broker_order_id,
                            OrderStatus.FILLED,
                            filled,
                            avg,
                            0,
                            "Fill confirmed",
                            raw=last,
                        )
                    if filled > 0 and status not in ("CANCELLED", "REJECTED"):
                        # OPEN / TRIGGER PENDING with partial qty — keep polling
                        last_partial = OrderResult(
                            req.client_order_id,
                            broker_order_id,
                            OrderStatus.PARTIAL,
                            filled,
                            avg,
                            0,
                            f"Partial fill {filled}/{qty_req} ({status})",
                            raw=last,
                        )
                    if status in ("CANCELLED", "REJECTED"):
                        if filled > 0:
                            return OrderResult(
                                req.client_order_id,
                                broker_order_id,
                                OrderStatus.PARTIAL,
                                filled,
                                avg,
                                0,
                                f"Order {status.lower()} after partial fill {filled}",
                                raw=last,
                            )
                        return OrderResult(
                            req.client_order_id,
                            broker_order_id,
                            OrderStatus.REJECTED,
                            0,
                            0,
                            0,
                            f"Order {status.lower()}",
                            raw=last,
                        )
            except Exception as e:
                audit("kite_reconcile_error", order_id=broker_order_id, error=str(e))
            await asyncio.sleep(0.5)
        if last_partial is not None:
            audit(
                "kite_reconcile_partial_timeout",
                order_id=broker_order_id,
                filled=last_partial.filled_qty,
            )
            return last_partial
        return OrderResult(
            req.client_order_id, broker_order_id, OrderStatus.SUBMITTED,
            0, 0, 0, "Reconciliation pending; do not resubmit", raw={"unknown": True},
        )

    async def place_stop_loss(
        self,
        req: OrderRequest,
        market_price: float,
    ) -> OrderResult:
        if not req.stop_price or req.stop_price <= 0:
            return OrderResult(
                req.client_order_id, "", OrderStatus.FAILED, 0, 0, 0,
                "Stop price missing",
            )
        sl_req = OrderRequest(
            symbol=req.symbol,
            side="short" if req.side == "long" else "long",
            qty=req.qty,
            order_type=OrderType.STOP,
            stop_price=req.stop_price,
            strategy=req.strategy,
            client_order_id=f"{req.client_order_id}-sl",
            metadata=dict(req.metadata),
        )
        return await self.place_order(sl_req, market_price)

    def _market_protection(self) -> int:
        """Kite rejects MARKET/SL-M with protection=0 (SEBI algo rule, enforced Apr 2025)."""
        val = self.cfg.kite_market_protection
        return -1 if val == 0 else val

    def _resolve_exchange(self, req: OrderRequest) -> str:
        return str(req.metadata.get("exchange") or self.cfg.kite_exchange).upper()

    def _resolve_product(self, req: OrderRequest) -> str:
        return str(req.metadata.get("product") or self.cfg.kite_product).upper()

    @staticmethod
    def _net_positions(positions_resp: dict | list) -> list[dict]:
        if isinstance(positions_resp, dict):
            return list(positions_resp.get("net") or [])
        return list(positions_resp)

    @staticmethod
    def _flatten_side(qty: int) -> tuple[str, int]:
        if qty > 0:
            return "short", qty
        if qty < 0:
            return "long", abs(qty)
        return "", 0

    def _build_params(self, req: OrderRequest, qty: int) -> dict:
        ot_map = {
            OrderType.MARKET: self._kite.ORDER_TYPE_MARKET,
            OrderType.LIMIT: self._kite.ORDER_TYPE_LIMIT,
            # Protective stop: SL-M + trigger (not SL, which needs limit price too)
            OrderType.STOP: self._kite.ORDER_TYPE_SLM,
            OrderType.STOP_LIMIT: self._kite.ORDER_TYPE_SL,
        }
        txn = (
            self._kite.TRANSACTION_TYPE_BUY
            if req.side == "long"
            else self._kite.TRANSACTION_TYPE_SELL
        )
        order_type = ot_map.get(req.order_type, self._kite.ORDER_TYPE_MARKET)
        params = {
            "variety": self._kite.VARIETY_REGULAR,
            "exchange": self._resolve_exchange(req),
            "tradingsymbol": req.symbol,
            "transaction_type": txn,
            "quantity": qty,
            "product": self._resolve_product(req),
            "order_type": order_type,
            "validity": self._kite.VALIDITY_DAY,
            "tag": req.client_order_id[:20],
        }
        if req.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and req.limit_price:
            params["price"] = self._round_tick(req.limit_price)
        if req.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and req.stop_price:
            params["trigger_price"] = self._round_tick(req.stop_price)
        if order_type in (self._kite.ORDER_TYPE_MARKET, self._kite.ORDER_TYPE_SLM):
            params["market_protection"] = self._market_protection()
        return params

    def _round_tick(self, price: float) -> float:
        """Snap a price to a valid NSE tick multiple — Kite rejects off-tick prices."""
        tick = self.cfg.kite_tick_size or 0.05
        if tick <= 0 or price <= 0:
            return round(price, 2)
        return round(round(price / tick) * tick, 2)

    async def _submit_order(self, params: dict) -> str:
        from services.control.execution_owner import require_execution_owner
        if self.cfg.kite_autoslice:
            raise RuntimeError("Auto-slice is unsupported until all child orders can be reconciled")
        await require_execution_owner()
        resp = await asyncio.get_event_loop().run_in_executor(
            None, partial(self._kite.place_order, **params)
        )
        return self._extract_order_id(resp)

    @staticmethod
    def _extract_order_id(resp) -> str:
        """Kite returns an order_id string, a dict, or (auto-slice) a list of them."""
        if isinstance(resp, dict):
            return str(resp.get("order_id", ""))
        if isinstance(resp, (list, tuple)) and resp:
            first = resp[0]
            if isinstance(first, dict):
                return str(first.get("order_id", ""))
            return str(first)
        return str(resp)

    async def cancel_order(self, broker_order_id: str) -> bool:
        from services.control.execution_owner import require_execution_owner
        await require_execution_owner()
        if not self._kite:
            return False
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                partial(
                    self._kite.cancel_order,
                    variety=self._kite.VARIETY_REGULAR,
                    order_id=broker_order_id,
                ),
            )
            return True
        except Exception:
            return False

    async def cancel_all(self) -> int:
        if not self._kite:
            return 0
        loop = asyncio.get_event_loop()
        try:
            orders = await loop.run_in_executor(None, self._kite.orders)
        except Exception:
            return 0
        count = 0
        open_status = {"OPEN", "TRIGGER PENDING", "PUT ORDER REQ RECEIVED"}
        for order in orders:
            if order.get("status") in open_status:
                if await self.cancel_order(str(order.get("order_id", ""))):
                    count += 1
        audit("kite_cancel_all", count=count)
        return count

    async def flatten_all(self) -> int:
        if not self._kite:
            return 0
        loop = asyncio.get_event_loop()
        count = 0
        try:
            await self.cancel_all()
            positions_resp = await loop.run_in_executor(None, self._kite.positions)
            for pos in self._net_positions(positions_resp):
                qty = int(pos.get("quantity", 0))
                side, close_qty = self._flatten_side(qty)
                if close_qty <= 0:
                    continue
                symbol = pos.get("tradingsymbol", "")
                product = pos.get("product", self._resolve_product(OrderRequest("", side, 0, OrderType.MARKET)))
                exchange = pos.get("exchange", self.cfg.kite_exchange)
                req = OrderRequest(
                    symbol=symbol,
                    side=side,
                    qty=close_qty,
                    order_type=OrderType.MARKET,
                    client_order_id=f"flatten-{symbol[:8]}",
                    metadata={"product": product, "exchange": exchange},
                )
                params = self._build_params(req, close_qty)
                await self._submit_order(params)
                count += 1
        except Exception as e:
            audit("kite_flatten_failed", error=str(e))
        audit("kite_flatten_all", count=count)
        return count

    async def fetch_open_positions(self) -> list[dict]:
        if not self._kite:
            raise ConnectionError("Kite is not connected; positions are unknown")
        loop = asyncio.get_event_loop()
        try:
            positions_resp = await loop.run_in_executor(None, self._kite.positions)
        except Exception as e:
            audit("kite_positions_failed", error=str(e))
            raise ConnectionError("Kite positions unavailable") from e
        out: list[dict] = []
        for pos in self._net_positions(positions_resp):
            qty = int(pos.get("quantity", 0))
            if qty == 0:
                continue
            out.append(
                {
                    "symbol": pos.get("tradingsymbol", ""),
                    "qty": abs(qty),
                    "entry": float(pos.get("average_price") or 0),
                    "ltp": float(pos.get("last_price") or 0),
                    "side": "long" if qty > 0 else "short",
                    "product": pos.get("product", ""),
                    "exchange": pos.get("exchange", ""),
                    "pnl": round(float(pos.get("pnl") or 0), 2),
                    "m2m": round(float(pos.get("m2m") or 0), 2),
                    "value": round(float(pos.get("value") or 0), 2),
                }
            )
        return out

    async def fetch_order_status(self, broker_order_id: str) -> dict:
        if not self._kite:
            return {"status": "UNKNOWN", "average_price": 0.0}
        loop = asyncio.get_event_loop()
        try:
            history = await loop.run_in_executor(
                None, partial(self._kite.order_history, broker_order_id)
            )
            if not history:
                return {"status": "UNKNOWN", "average_price": 0.0}
            last = history[-1]
            return {
                "status": last.get("status", "UNKNOWN"),
                "average_price": float(last.get("average_price") or 0),
                "filled_quantity": float(last.get("filled_quantity") or 0),
                "pending_quantity": float(last.get("pending_quantity") or 0),
                "quantity": float(last.get("quantity") or 0),
                **{k: last.get(k) for k in ("tradingsymbol", "transaction_type", "product", "exchange", "order_type", "trigger_price")},
            }
        except Exception as e:
            audit("kite_order_status_failed", order_id=broker_order_id, error=str(e))
            return {"status": "UNKNOWN", "average_price": 0.0}

    async def flatten_symbol(self, symbol: str) -> int:
        if not self._kite:
            return 0
        sym = symbol.upper()
        loop = asyncio.get_event_loop()
        count = 0
        try:
            positions_resp = await loop.run_in_executor(None, self._kite.positions)
            for pos in self._net_positions(positions_resp):
                if pos.get("tradingsymbol", "").upper() != sym:
                    continue
                qty = int(pos.get("quantity", 0))
                side, close_qty = self._flatten_side(qty)
                if close_qty <= 0:
                    continue
                product = pos.get("product", self.cfg.kite_product)
                exchange = pos.get("exchange", self.cfg.kite_exchange)
                req = OrderRequest(
                    symbol=sym,
                    side=side,
                    qty=close_qty,
                    order_type=OrderType.MARKET,
                    client_order_id=f"flatten-{sym[:8]}",
                    metadata={"product": product, "exchange": exchange},
                )
                params = self._build_params(req, close_qty)
                await self._submit_order(params)
                count += 1
        except Exception as e:
            audit("kite_flatten_symbol_failed", symbol=sym, error=str(e))
        audit("kite_flatten_symbol", symbol=sym, count=count)
        return count

    async def fetch_account_equity(self) -> dict:
        """Pull live equity/cash/buying-power from Kite margins (equity segment)."""
        if not self._kite and not await self.connect():
            return {"ok": False, "error": "not_connected"}
        try:
            loop = asyncio.get_event_loop()
            margins = await loop.run_in_executor(None, self._kite.margins)
            eq = margins.get("equity") or {}
            net = float(eq.get("net") or 0)
            available = eq.get("available") or {}
            cash = float(available.get("cash") or 0)
            live_balance = float(available.get("live_balance") or 0)
            collateral = float(available.get("collateral") or 0)
            adhoc = float(available.get("adhoc_margin") or 0)
            intraday_payin = float(available.get("intraday_payin") or 0)

            liquid_cash = max(0.0, float(available.get("live_balance", cash)))
            buying_power = max(0.0, min(net, liquid_cash))
            # Available margin is not total account equity. Preserve the configured
            # capital ledger; sync only spendable funds to avoid margin-driven sizing.
            equity = max(0.0, net)
            if net < 0:
                return {"ok": False, "error": "negative_available_margin"}

            return {
                "ok": True,
                "equity": round(equity, 2),
                "cash": round(max(0.0, liquid_cash), 2),
                "buying_power": round(buying_power, 2),
                "margins_detail": {
                    "net": round(net, 2),
                    "cash": round(cash, 2),
                    "live_balance": round(live_balance, 2),
                    "collateral": round(collateral, 2),
                    "adhoc_margin": round(adhoc, 2),
                    "intraday_payin": round(intraday_payin, 2),
                },
                "source": "kite_margins",
            }
        except Exception as exc:
            audit("kite_margins_failed", error=str(exc))
            return {"ok": False, "error": str(exc)}
