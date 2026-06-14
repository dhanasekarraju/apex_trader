"""Paper broker — deterministic fills with stop-loss support."""

from __future__ import annotations

from services.brokers.base import (
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
)
from shared.logging import audit


class PaperBroker(BrokerAdapter):
    name = "paper"
    live_ready = False
    SLIPPAGE_BPS = 2.5

    def __init__(self) -> None:
        self._connected = True
        self._orders: dict[str, OrderResult] = {}
        self._stop_orders: dict[str, OrderResult] = {}
        self._open_positions: list[dict] = []

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    async def place_order(self, req: OrderRequest, market_price: float) -> OrderResult:
        slip = self.SLIPPAGE_BPS
        if req.order_type == OrderType.LIMIT and req.limit_price:
            fill = req.limit_price
        elif req.order_type == OrderType.STOP and req.stop_price:
            fill = req.stop_price
        else:
            adj = market_price * slip / 10000
            fill = market_price + adj if req.side == "long" else market_price - adj

        oid = f"PAPER-{req.client_order_id[:8]}"
        result = OrderResult(
            client_order_id=req.client_order_id,
            broker_order_id=oid,
            status=OrderStatus.FILLED,
            filled_qty=req.qty,
            avg_price=round(fill, 4),
            slippage_bps=slip,
            message="Paper fill",
        )
        self._orders[oid] = result
        if req.side == "long" and req.order_type == OrderType.MARKET:
            self._open_positions.append(
                {"symbol": req.symbol, "qty": req.qty, "entry": fill, "order_id": oid}
            )
        audit("paper_order_filled", symbol=req.symbol, qty=req.qty, price=fill)
        return result

    async def reconcile_order(
        self,
        broker_order_id: str,
        req: OrderRequest,
        timeout_sec: float = 30.0,
    ) -> OrderResult:
        if broker_order_id in self._orders:
            return self._orders[broker_order_id]
        return OrderResult(
            req.client_order_id, broker_order_id, OrderStatus.FAILED,
            0, 0, 0, "Unknown paper order",
        )

    async def place_stop_loss(
        self,
        req: OrderRequest,
        market_price: float,
    ) -> OrderResult:
        if not req.stop_price:
            return OrderResult(
                req.client_order_id, "", OrderStatus.FAILED, 0, 0, 0,
                "Stop price missing",
            )
        sl_req = OrderRequest(
            symbol=req.symbol,
            side="short",
            qty=req.qty,
            order_type=OrderType.STOP,
            stop_price=req.stop_price,
            client_order_id=f"{req.client_order_id}-sl",
        )
        result = await self.place_order(sl_req, market_price)
        self._stop_orders[result.broker_order_id] = result
        audit("paper_stop_placed", symbol=req.symbol, stop=req.stop_price)
        return result

    async def cancel_order(self, broker_order_id: str) -> bool:
        return broker_order_id in self._orders or broker_order_id in self._stop_orders

    async def cancel_all(self) -> int:
        n = len(self._orders) + len(self._stop_orders)
        self._orders.clear()
        self._stop_orders.clear()
        return n

    async def flatten_all(self) -> int:
        await self.cancel_all()
        count = len(self._open_positions)
        self._open_positions.clear()
        audit("paper_flatten_all", count=count)
        return count
