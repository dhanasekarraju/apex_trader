"""Fault injection for chaos scenarios."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

from services.brokers.base import BrokerAdapter
from services.brokers.factory import get_broker
from services.chaos.broker_mock import ChaosBroker
from services.chaos.latency_simulator import LatencySimulator
from services.chaos.scenarios import ChaosScenario


class FaultInjector:
    """Applies and removes fault patches for a chaos scenario."""

    def __init__(self, scenario: ChaosScenario) -> None:
        self.scenario = scenario
        self.broker: ChaosBroker | None = None
        self._patches: list[Any] = []
        self.latency = LatencySimulator(scenario.latency_profile, scenario.seed)

    def build_broker(self) -> ChaosBroker:
        cfg = dict(self.scenario.fault_config)
        mode = cfg.pop("broker_mode", "normal")
        self.broker = ChaosBroker(
            seed=self.scenario.seed,
            mode=mode,
            latency_profile=self.scenario.latency_profile,
            fault_config=cfg,
        )
        return self.broker

    @asynccontextmanager
    async def activate(self) -> AsyncIterator[ChaosBroker]:
        broker = self.build_broker()
        patches = []

        def _broker_factory(mode: str | None = None) -> BrokerAdapter:
            return broker

        patches.append(patch("services.brokers.factory.get_broker", side_effect=_broker_factory))
        patches.append(patch("services.execution.execution_engine.get_broker", side_effect=_broker_factory))
        patches.append(patch("services.execution.lifecycle.get_broker", side_effect=_broker_factory))

        cfg = self.scenario.fault_config

        if cfg.get("icb_delay_ms"):
            from services.icb import engine as icb_mod

            original = icb_mod.InstitutionalControlBrain._authorize_inner

            async def delayed_inner(self, action, context):
                await asyncio.sleep(cfg["icb_delay_ms"] / 1000.0)
                return await original(self, action, context)

            patches.append(patch.object(icb_mod.InstitutionalControlBrain, "_authorize_inner", delayed_inner))

        if cfg.get("crce_fail"):
            from services.compliance import recorder as rec_mod

            async def failing_append(self, event):
                self._healthy = False
                await self._engage_safe_mode("Chaos: CRCE write failure")
                return None

            patches.append(patch.object(rec_mod.ComplianceRecorder, "_append", failing_append))

        if cfg.get("crce_delay_ms"):
            original_append = None

            async def delayed_append(self, event):
                await asyncio.sleep(cfg["crce_delay_ms"] / 1000.0)
                return await original_append(self, event)

            from services.compliance import recorder as rec_mod
            original_append = rec_mod.ComplianceRecorder._append
            patches.append(patch.object(rec_mod.ComplianceRecorder, "_append", delayed_append))

        if cfg.get("risk_timeout"):
            async def timeout_eval(*args, **kwargs):
                raise TimeoutError("Chaos: risk timeout")

            patches.append(patch(
                "services.risk.unified.UnifiedRiskEngine.evaluate_trade",
                side_effect=timeout_eval,
            ))

        if cfg.get("intermittent_fail_rate"):
            rate = float(cfg["intermittent_fail_rate"])
            import random
            rng = random.Random(self.scenario.seed)

            original_connect = broker.connect

            async def flaky_connect():
                if rng.random() < rate:
                    broker._connected = False
                    return False
                return await original_connect()

            broker.connect = flaky_connect  # type: ignore[method-assign]

        if cfg.get("packet_loss_rate"):
            rate = float(cfg["packet_loss_rate"])
            import random
            rng = random.Random(self.scenario.seed)
            original_place = broker.place_order

            async def lossy_place(req, market_price):
                if rng.random() < rate:
                    from services.brokers.base import OrderResult, OrderStatus
                    return OrderResult(
                        req.client_order_id, "", OrderStatus.FAILED, 0, 0, 0,
                        "Chaos: network packet loss",
                    )
                return await original_place(req, market_price)

            broker.place_order = lossy_place  # type: ignore[method-assign]

        if cfg.get("redis_crash"):
            async def redis_down():
                raise ConnectionError("Chaos: Redis crash")

            patches.append(patch("shared.events.get_redis", side_effect=redis_down))

        if cfg.get("db_latency_ms"):
            from services.portfolio import manager as pm_mod

            original_persist = pm_mod.PortfolioManager.persist

            async def delayed_persist(self):
                await asyncio.sleep(cfg["db_latency_ms"] / 1000.0)
                return await original_persist(self)

            patches.append(patch.object(pm_mod.PortfolioManager, "persist", delayed_persist))

        if cfg.get("inject_latency"):
            from services.icb import engine as icb_mod

            latency_sim = self.latency
            original_authorize = icb_mod.InstitutionalControlBrain.authorize

            async def latency_authorize(self, action, context):
                await latency_sim.apply()
                return await original_authorize(self, action, context)

            patches.append(patch.object(icb_mod.InstitutionalControlBrain, "authorize", latency_authorize))

        if cfg.get("api_timeout_burst"):
            from services.execution.execution_engine import ExecutionEngine
            from shared.config import get_settings

            threshold = get_settings().api_failure_threshold
            original_pre = ExecutionEngine._pre_execution_block
            burst_applied = {"done": False}

            async def pre_with_burst(self, req):
                if not burst_applied["done"]:
                    for _ in range(threshold):
                        self._circuit.record_failure("Chaos: API timeout burst")
                    burst_applied["done"] = True
                return await original_pre(self, req)

            patches.append(patch.object(ExecutionEngine, "_pre_execution_block", pre_with_burst))

        if cfg.get("reconciliation_drift"):
            from services.control.reconciliation_state import set_reconciliation_degraded

            await set_reconciliation_degraded("Chaos: reconciliation drift")

        for p in patches:
            p.start()
            self._patches.append(p)

        try:
            yield broker
        finally:
            if cfg.get("reconciliation_drift"):
                from services.control.reconciliation_state import clear_reconciliation_degraded

                await clear_reconciliation_degraded()
            for p in reversed(self._patches):
                p.stop()
            self._patches.clear()
