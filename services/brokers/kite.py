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
            return False
        try:
            from kiteconnect import KiteConnect
            self._kite = KiteConnect(api_key=self.cfg.kite_api_key)
            self._kite.set_access_token(token)
            await asyncio.get_event_loop().run_in_executor(None, self._kite.profile)
            self._connected = True
            return True
        except Exception as e:
            audit("kite_connect_failed", error=str(e))
            self._connected = False
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
            return OrderResult(
                req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0, str(e),
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
        while time.monotonic() < deadline:
            try:
                history = await loop.run_in_executor(
                    None, partial(self._kite.order_history, broker_order_id)
                )
                if history:
                    last = history[-1]
                    status = last.get("status", "")
                    if status == "COMPLETE":
                        return OrderResult(
                            req.client_order_id,
                            broker_order_id,
                            OrderStatus.FILLED,
                            float(last.get("filled_quantity", 0)),
                            float(last.get("average_price", 0)),
                            0,
                            "Fill confirmed",
                            raw=last,
                        )
                    if status in ("CANCELLED", "REJECTED"):
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
        return OrderResult(
            req.client_order_id, broker_order_id, OrderStatus.FAILED,
            0, 0, 0, "Reconciliation timeout",
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
            params["price"] = req.limit_price
        if req.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and req.stop_price:
            params["trigger_price"] = req.stop_price
        if order_type in (self._kite.ORDER_TYPE_MARKET, self._kite.ORDER_TYPE_SLM):
            params["market_protection"] = self._market_protection()
        return params

    async def _submit_order(self, params: dict) -> str:
        loop = asyncio.get_event_loop()
        if self.cfg.kite_autoslice:
            resp = await loop.run_in_executor(
                None, partial(self._kite.place_autoslice_order, **params)
            )
            if isinstance(resp, dict):
                return str(resp.get("order_id", ""))
            return str(resp)
        resp = await loop.run_in_executor(
            None, partial(self._kite.place_order, **params)
        )
        return str(resp)

    async def cancel_order(self, broker_order_id: str) -> bool:
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
            return []
        loop = asyncio.get_event_loop()
        try:
            positions_resp = await loop.run_in_executor(None, self._kite.positions)
        except Exception as e:
            audit("kite_positions_failed", error=str(e))
            return []
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
                    "side": "long" if qty > 0 else "short",
                    "product": pos.get("product", ""),
                    "exchange": pos.get("exchange", ""),
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
        """Pull live equity/cash from Kite margins (equity segment)."""
        if not self._kite and not await self.connect():
            return {"ok": False, "error": "not_connected"}
        try:
            loop = asyncio.get_event_loop()
            margins = await loop.run_in_executor(None, self._kite.margins)
            eq = margins.get("equity") or {}
            net = float(eq.get("net") or 0)
            available = eq.get("available") or {}
            cash = float(
                available.get("live_balance")
                or available.get("cash")
                or available.get("opening_balance")
                or net
            )
            if net <= 0:
                return {"ok": False, "error": "zero_equity", "raw": eq}
            return {
                "ok": True,
                "equity": round(net, 2),
                "cash": round(max(0.0, cash), 2),
                "source": "kite_margins",
            }
        except Exception as exc:
            audit("kite_margins_failed", error=str(exc))
            return {"ok": False, "error": str(exc)}
