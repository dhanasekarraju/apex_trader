"""Interactive Brokers adapter — structured for ib_insync integration."""

from __future__ import annotations

from services.brokers.base import (
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
)
from shared.config import get_settings
from shared.logging import audit


class InteractiveBrokersAdapter(BrokerAdapter):
    name = "ib"
    live_ready = False

    def __init__(self) -> None:
        self.cfg = get_settings()
        self._connected = False
        self._ib = None

    async def connect(self) -> bool:
        try:
            from ib_insync import IB
            self._ib = IB()
            await self._ib.connectAsync(self.cfg.ib_host, self.cfg.ib_port, clientId=self.cfg.ib_client_id)
            self._connected = True
            audit("ib_connected", host=self.cfg.ib_host)
            return True
        except Exception as e:
            audit("ib_connect_failed", error=str(e))
            return False

    async def disconnect(self) -> None:
        if self._ib:
            self._ib.disconnect()
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected and self._ib is not None and self._ib.isConnected()

    async def place_order(self, req: OrderRequest, market_price: float) -> OrderResult:
        if not await self.is_connected():
            return OrderResult(req.client_order_id, "", OrderStatus.FAILED, 0, 0, 0, "IB disconnected")
        try:
            from ib_insync import MarketOrder, LimitOrder, StopOrder, Stock
            contract = Stock(req.symbol, "SMART", "USD")
            if req.order_type == OrderType.LIMIT:
                order = LimitOrder("BUY" if req.side == "long" else "SELL", req.qty, req.limit_price or market_price)
            elif req.order_type == OrderType.STOP:
                order = StopOrder("BUY" if req.side == "long" else "SELL", req.qty, req.stop_price or market_price)
            else:
                order = MarketOrder("BUY" if req.side == "long" else "SELL", req.qty)
            trade = self._ib.placeOrder(contract, order)
            oid = str(trade.order.orderId)
            audit("ib_order_placed", order_id=oid, symbol=req.symbol)
            return OrderResult(
                req.client_order_id, oid, OrderStatus.SUBMITTED, 0, market_price, 0,
                "Submitted to IB",
            )
        except Exception as e:
            return OrderResult(req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0, str(e))

    async def cancel_order(self, broker_order_id: str) -> bool:
        return False

    async def cancel_all(self) -> int:
        return 0

    async def flatten_all(self) -> int:
        audit("ib_flatten_all")
        return 0
