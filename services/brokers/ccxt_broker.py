"""CCXT crypto exchange adapter."""

from __future__ import annotations

import asyncio
from functools import partial

from services.brokers.base import (
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
)
from shared.config import get_settings
from shared.logging import audit


class CCXTBroker(BrokerAdapter):
    name = "ccxt"
    live_ready = False

    def __init__(self) -> None:
        self.cfg = get_settings()
        self._exchange = None
        self._connected = False

    async def connect(self) -> bool:
        try:
            import ccxt
            klass = getattr(ccxt, self.cfg.ccxt_exchange, None)
            if not klass:
                return False
            self._exchange = klass({
                "apiKey": self.cfg.ccxt_api_key,
                "secret": self.cfg.ccxt_api_secret,
                "enableRateLimit": True,
            })
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._exchange.load_markets)
            self._connected = True
            audit("ccxt_connected", exchange=self.cfg.ccxt_exchange)
            return True
        except Exception as e:
            audit("ccxt_connect_failed", error=str(e))
            return False

    async def disconnect(self) -> None:
        self._connected = False
        self._exchange = None

    async def is_connected(self) -> bool:
        return self._connected and self._exchange is not None

    async def place_order(self, req: OrderRequest, market_price: float) -> OrderResult:
        if not self._exchange:
            return OrderResult(req.client_order_id, "", OrderStatus.FAILED, 0, 0, 0, "CCXT not connected")
        try:
            side = "buy" if req.side == "long" else "sell"
            otype = "market" if req.order_type == OrderType.MARKET else "limit"
            params = {"clientOrderId": req.client_order_id}
            kwargs = {"symbol": req.symbol, "type": otype, "side": side, "amount": req.qty}
            if otype == "limit" and req.limit_price:
                kwargs["price"] = req.limit_price
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, partial(self._exchange.create_order, **kwargs))
            oid = str(resp.get("id", ""))
            filled = float(resp.get("filled", 0) or 0)
            avg = float(resp.get("average", market_price) or market_price)
            status = OrderStatus.FILLED if filled >= req.qty * 0.99 else OrderStatus.PARTIAL
            audit("ccxt_order", symbol=req.symbol, id=oid, status=status.value)
            return OrderResult(req.client_order_id, oid, status, filled, avg, 0, "CCXT order")
        except Exception as e:
            return OrderResult(req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0, str(e))

    async def cancel_order(self, broker_order_id: str) -> bool:
        return False

    async def cancel_all(self) -> int:
        return 0

    async def flatten_all(self) -> int:
        return 0
