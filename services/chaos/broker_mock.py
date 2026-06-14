"""Deterministic broker simulator for chaos scenarios."""

from __future__ import annotations

import asyncio
import random
from typing import Any

from services.brokers.base import (
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
)
from services.brokers.paper import PaperBroker
from services.chaos.latency_simulator import LatencySimulator
from services.chaos.scenarios import LatencyProfile


class ChaosBroker(BrokerAdapter):
    """
    Wraps PaperBroker with deterministic fault injection per scenario seed.
    """

    name = "chaos"

    def __init__(
        self,
        *,
        seed: int = 42,
        mode: str = "normal",
        latency_profile: LatencyProfile = LatencyProfile.NORMAL,
        fault_config: dict[str, Any] | None = None,
    ) -> None:
        self._inner = PaperBroker()
        self._rng = random.Random(seed)
        self._mode = mode
        self._cfg = fault_config or {}
        self._latency = LatencySimulator(latency_profile, seed)
        self._connected = True
        self._order_count = 0
        self._duplicate_emitted: set[str] = set()

    async def connect(self) -> bool:
        if self._mode == "disconnect" and not self._cfg.get("disconnect_on_order"):
            self._connected = False
            return False
        self._connected = True
        return await self._inner.connect()

    async def disconnect(self) -> None:
        self._connected = False
        await self._inner.disconnect()

    async def is_connected(self) -> bool:
        return self._connected and await self._inner.is_connected()

    async def place_order(self, req: OrderRequest, market_price: float) -> OrderResult:
        self._order_count += 1
        delay = self._cfg.get("delay_ms")
        await self._latency.apply(delay)

        if self._mode == "disconnect" and self._cfg.get("disconnect_on_order"):
            self._connected = False
            return OrderResult(
                req.client_order_id, "", OrderStatus.FAILED, 0, 0, 0,
                "Broker disconnected during order",
            )

        if self._mode == "rejection_spike":
            rate = float(self._cfg.get("reject_rate", 0.8))
            if self._rng.random() < rate:
                return OrderResult(
                    req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0,
                    "Chaos: broker rejection spike",
                )

        if self._mode == "illiquidity":
            return OrderResult(
                req.client_order_id, "", OrderStatus.REJECTED, 0, 0, 0,
                "Chaos: illiquidity — no fills",
            )

        if self._mode == "spread_widen":
            spread_bps = float(self._cfg.get("spread_bps", 100))
            market_price = market_price * (1 + spread_bps / 10000)

        gap = float(self._cfg.get("price_gap_pct", 0))
        if gap:
            market_price = market_price * (1 + gap / 100)

        vol = float(self._cfg.get("volatility_spike_pct", 0))
        if vol:
            direction = 1 if self._rng.random() > 0.5 else -1
            market_price = market_price * (1 + direction * vol / 100)

        if self._cfg.get("packet_loss_rate"):
            rate = float(self._cfg["packet_loss_rate"])
            if self._rng.random() < rate:
                return OrderResult(
                    req.client_order_id, "", OrderStatus.FAILED, 0, 0, 0,
                    "Chaos: packet loss — request dropped",
                )

        if self._mode == "missing_confirmation":
            return OrderResult(
                req.client_order_id, f"CHAOS-PENDING-{self._order_count}",
                OrderStatus.SUBMITTED, 0, 0, 0,
                "Chaos: missing fill confirmation",
            )

        result = await self._inner.place_order(req, market_price)

        if self._mode == "partial_fill":
            ratio = float(self._cfg.get("fill_ratio", 0.5))
            filled = max(1, int(req.qty * ratio))
            result = OrderResult(
                result.client_order_id,
                result.broker_order_id,
                OrderStatus.PARTIAL,
                filled,
                result.avg_price,
                result.slippage_bps,
                f"Chaos: partial fill {filled}/{req.qty}",
                raw=result.raw,
            )

        if self._mode == "duplicate_fill" and req.client_order_id not in self._duplicate_emitted:
            self._duplicate_emitted.add(req.client_order_id)
            result.raw["duplicate_fill"] = True

        if self._mode == "sl_not_confirmed":
            result.raw["stop_order_id"] = ""
            result.message = "Chaos: entry filled but SL not confirmed"

        return result

    async def place_stop_loss(self, req: OrderRequest, market_price: float) -> OrderResult:
        if self._mode == "sl_not_confirmed":
            return OrderResult(
                req.client_order_id, "", OrderStatus.FAILED, 0, 0, 0,
                "Chaos: SL placement failed",
            )
        return await self._inner.place_stop_loss(req, market_price)

    async def cancel_order(self, broker_order_id: str) -> bool:
        return await self._inner.cancel_order(broker_order_id)

    async def cancel_all(self) -> int:
        return await self._inner.cancel_all()

    async def flatten_all(self) -> int:
        return await self._inner.flatten_all()

    async def fetch_open_positions(self) -> list[dict]:
        positions = await self._inner.fetch_open_positions()
        if self._mode == "position_mismatch":
            if positions:
                extra = dict(positions[0])
                extra["symbol"] = "PHANTOM"
                extra["qty"] = 999
                positions.append(extra)
        return positions

    async def reconcile_order(
        self,
        broker_order_id: str,
        req: OrderRequest,
        timeout_sec: float = 30.0,
    ) -> OrderResult:
        if self._mode == "missing_confirmation":
            await asyncio.sleep(min(0.05, timeout_sec))
            return OrderResult(
                req.client_order_id, broker_order_id, OrderStatus.SUBMITTED,
                0, 0, 0, "Chaos: reconciliation timeout — no confirmation",
            )
        return await self._inner.reconcile_order(broker_order_id, req, timeout_sec)
