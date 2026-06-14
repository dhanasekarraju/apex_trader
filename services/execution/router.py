"""Execution router — retry-safe live path with SL + reconciliation."""

from __future__ import annotations

import asyncio
import uuid

from services.brokers.base import OrderRequest, OrderResult, OrderStatus
from services.brokers.factory import get_broker
from services.execution.live_gate import LiveSafetyGate
from services.market_data.service import MarketDataService
from services.portfolio.manager import PortfolioManager
from services.shadow.engine import ShadowEngine
from shared.config import get_settings
from shared.logging import audit

_seen_orders: set[str] = set()


class ExecutionRouter:
    max_retries = 3
    retry_delay = 1.0

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

    def bind(self, portfolio: PortfolioManager, market_data: MarketDataService) -> None:
        self._portfolio = portfolio
        self._market_data = market_data

    async def connect(self) -> bool:
        return await self._broker.connect()

    async def submit(self, req: OrderRequest, market_price: float) -> OrderResult:
        if req.client_order_id in _seen_orders:
            audit("duplicate_order_blocked", id=req.client_order_id)
            return OrderResult(
                req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0,
                "Duplicate order blocked",
            )
        _seen_orders.add(req.client_order_id)

        cfg = get_settings()
        mode = cfg.trading_mode
        if mode == "shadow":
            return self._shadow.simulate(req, market_price)
        if mode == "paper":
            return await self._submit_with_stop(req, market_price)
        if mode == "live":
            ok, blockers = await self._live_allowed()
            if not ok:
                return OrderResult(
                    req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0,
                    "Live blocked: " + "; ".join(blockers[:3]),
                )
            return await self._submit_live(req, market_price)
        return await self._broker.place_order(req, market_price)

    async def _live_allowed(self) -> tuple[bool, list[str]]:
        if self._portfolio is None or self._market_data is None:
            return False, ["Execution router not bound to portfolio/market data"]
        return await LiveSafetyGate.check(
            market_data=self._market_data,
            broker=self._broker,
            portfolio=self._portfolio,
        )

    async def _submit_live(self, req: OrderRequest, market_price: float) -> OrderResult:
        entry = await self._broker.place_order(req, market_price)
        if entry.status in (OrderStatus.FAILED, OrderStatus.REJECTED):
            return entry
        if entry.broker_order_id:
            entry = await self._broker.reconcile_order(entry.broker_order_id, req)
        if entry.status not in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            return entry
        return await self._attach_stop_loss(req, entry, market_price)

    async def _submit_with_stop(self, req: OrderRequest, market_price: float) -> OrderResult:
        entry = await self._broker.place_order(req, market_price)
        if entry.status not in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            return entry
        if not req.stop_price:
            return OrderResult(
                req.client_order_id, entry.broker_order_id, OrderStatus.REJECTED,
                0, 0, 0, "Stop-loss price required",
            )
        return await self._attach_stop_loss(req, entry, market_price)

    async def _attach_stop_loss(
        self,
        req: OrderRequest,
        entry: OrderResult,
        market_price: float,
    ) -> OrderResult:
        sl_req = OrderRequest(
            symbol=req.symbol,
            side=req.side,
            qty=entry.filled_qty or req.qty,
            order_type=req.order_type,
            stop_price=req.stop_price,
            take_profit=req.take_profit,
            strategy=req.strategy,
            client_order_id=f"{req.client_order_id}-sl",
        )
        sl = await self._broker.place_stop_loss(sl_req, market_price)
        if sl.status in (OrderStatus.FAILED, OrderStatus.REJECTED):
            audit("stop_loss_failed", symbol=req.symbol, reason=sl.message)
            await self._broker.flatten_all()
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

    async def flatten_all(self) -> int:
        audit("flatten_all_requested")
        return await self._broker.flatten_all()

    async def cancel_all(self) -> int:
        return await self._broker.cancel_all()

    def shadow_report(self) -> dict:
        return self._shadow.weekly_report()

    async def live_blockers(self) -> list[str]:
        _, blockers = await self._live_allowed()
        return blockers
