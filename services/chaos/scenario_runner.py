"""Chaos scenario execution and observation."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from services.brokers.base import OrderRequest, OrderType
from services.chaos.fault_injector import FaultInjector
from services.chaos.scenarios import ChaosScenario
from services.compliance.events import EventType
from services.compliance.recorder import crce
from services.core.orchestrator import TradingOrchestrator
from services.icb.actions import ICBAction
from services.icb.engine import icb
from services.icb.system_state import SystemState, clear_system_state, get_kill_switch_latched
from services.portfolio.manager import PortfolioManager
from shared.config import get_settings
from shared.logging import audit


@dataclass
class ScenarioResult:
    scenario_id: str
    passed: bool
    safe: bool
    duration_ms: float
    icb_decision: str = ""
    risk_verdict: str = ""
    execution_status: str = ""
    reconciliation_ok: bool = True
    portfolio_consistent: bool = True
    kill_switch_triggered: bool = False
    safe_mode_triggered: bool = False
    duplicate_detected: bool = False
    failures: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    recovery_time_ms: float = 0.0


class ScenarioRunner:
    """Runs a single chaos scenario through the institutional pipeline."""

    def __init__(self, orchestrator: TradingOrchestrator | None = None) -> None:
        self.orch = orchestrator or TradingOrchestrator()

    async def run(self, scenario: ChaosScenario) -> ScenarioResult:
        start = time.perf_counter()
        await clear_system_state()
        await icb.recover_safe_mode()

        await self._log_chaos(
            EventType.CHAOS_SCENARIO_STARTED,
            scenario.id,
            decision="STARTED",
            reason=scenario.name,
        )
        await self._log_chaos(
            EventType.FAULT_INJECTED,
            scenario.id,
            decision="INJECTED",
            reason=str(scenario.fault_config),
        )

        result = ScenarioResult(scenario_id=scenario.id, passed=False, safe=False, duration_ms=0)
        injector = FaultInjector(scenario)

        try:
            async with injector.activate() as broker:
                cfg = get_settings()
                monkeypatch_env = {"ENFORCE_MARKET_HOURS": "false", "TRADING_MODE": "paper"}
                import os
                for k, v in monkeypatch_env.items():
                    os.environ[k] = v
                get_settings.cache_clear()
                self.orch.cfg = get_settings()
                self.orch.execution.refresh_broker()

                portfolio_before = len(self.orch.portfolio.state.positions)
                icb_result = await icb.authorize(
                    ICBAction.ANALYZE_SYMBOL,
                    {
                        "portfolio": self.orch.portfolio,
                        "trading_mode": "paper",
                        "symbol": "RELIANCE",
                        "risk_status": "SAFE",
                    },
                )
                result.icb_decision = icb_result.decision
                result.observations.append(f"ICB: {icb_result.decision} — {icb_result.reason}")

                if icb_result.system_state == SystemState.SAFE_MODE:
                    result.safe_mode_triggered = True
                if await get_kill_switch_latched():
                    result.kill_switch_triggered = True

                decision = {"action": "NO_TRADE", "reason": icb_result.reason, "execution": {}}
                exec_info: dict = {}
                if icb_result.allowed:
                    decision = await self.orch.analyze_symbol("RELIANCE")
                    exec_info = decision.get("execution") or {}
                    result.risk_verdict = decision.get("risk_verdict", decision.get("action", ""))
                    result.execution_status = exec_info.get("status", decision.get("action", ""))

                await self._log_chaos(
                    EventType.SYSTEM_RESPONSE,
                    scenario.id,
                    decision=decision.get("action", "UNKNOWN"),
                    reason=decision.get("reason", decision.get("risk_reason", "")),
                )

                if decision.get("action") == "BUY":
                    await self._log_chaos(
                        EventType.RISK_DECISION,
                        scenario.id,
                        decision="ALLOW",
                        reason=decision.get("risk_reason", ""),
                    )
                    await self._log_chaos(
                        EventType.EXECUTION_OUTCOME,
                        scenario.id,
                        decision=result.execution_status or "UNKNOWN",
                        reason=str(exec_info),
                    )

                positions = await broker.fetch_open_positions()
                internal = len(self.orch.portfolio.state.positions)
                broker_count = len(positions)
                result.portfolio_consistent = internal <= broker_count or broker_count == 0
                if not result.portfolio_consistent:
                    result.failures.append(
                        f"Portfolio mismatch: internal={internal} broker={broker_count}",
                    )

                result.reconciliation_ok = result.portfolio_consistent
                await self._log_chaos(
                    EventType.RECONCILIATION_RESULT,
                    scenario.id,
                    decision="OK" if result.reconciliation_ok else "DRIFT",
                    reason=f"internal={internal} broker={broker_count}",
                )

                result.safe_mode_triggered = result.safe_mode_triggered or not icb.healthy
                result.kill_switch_triggered = result.kill_switch_triggered or await get_kill_switch_latched()

                if scenario.fault_config.get("reconciliation_drift"):
                    from services.control.reconciliation_state import is_reconciliation_degraded

                    if not await is_reconciliation_degraded():
                        result.failures.append("Expected reconciliation degraded state")
                    if icb_result.allowed and result.execution_status.lower() in ("filled", "partial"):
                        result.failures.append("Expected trading blocked under reconciliation drift")

                if scenario.fault_config.get("api_timeout_burst"):
                    circuit = self.orch.execution.circuit_status()
                    if not circuit.get("open"):
                        result.failures.append("Expected API circuit breaker open")
                    result.observations.append(f"circuit={circuit}")

                result.safe = self._validate_safety(result, scenario, portfolio_before)
                result.passed = result.safe and self._validate_expectations(result, scenario)

        except Exception as exc:
            result.failures.append(f"Scenario exception: {exc}")
            result.observations.append(str(exc))
            result.safe = False
            result.passed = False

        result.duration_ms = (time.perf_counter() - start) * 1000
        result.recovery_time_ms = result.duration_ms

        await self._log_chaos(
            EventType.CHAOS_SCENARIO_COMPLETED,
            scenario.id,
            decision="PASS" if result.passed else "FAIL",
            reason=f"safe={result.safe} failures={len(result.failures)}",
        )
        audit("chaos_scenario_complete", scenario=scenario.id, passed=result.passed, safe=result.safe)
        return result

    def _validate_safety(
        self,
        result: ScenarioResult,
        scenario: ChaosScenario,
        portfolio_before: int,
    ) -> bool:
        safe = True
        if not result.portfolio_consistent:
            safe = False
        if result.duplicate_detected:
            safe = False
            result.failures.append("Duplicate trade detected")
        unaccounted = len(self.orch.portfolio.state.positions) - portfolio_before
        if unaccounted > 1 and result.execution_status == "rejected":
            result.failures.append(f"Unaccounted positions: +{unaccounted}")
            safe = False
        return safe and not result.failures

    def _validate_expectations(self, result: ScenarioResult, scenario: ChaosScenario) -> bool:
        if scenario.expect_deny:
            executed = result.execution_status.lower() in ("filled", "partial")
            denied = result.icb_decision == "DENY" or result.execution_status.upper() in (
                "REJECTED", "NO_TRADE", "REJECT",
            )
            if executed and scenario.category.value != "broker":
                result.failures.append("Expected deny/block but trade executed")
                return False
            if scenario.fault_config.get("reconciliation_drift") and result.icb_decision != "DENY":
                result.failures.append("Expected ICB deny under reconciliation drift")
                return False
            if scenario.fault_config.get("api_timeout_burst") and not denied and not executed:
                if "Expected API circuit breaker open" not in result.failures:
                    pass
        if scenario.expect_safe_mode and not result.safe_mode_triggered:
            result.failures.append("Expected SAFE_MODE but not triggered")
            return False
        return True

    async def _log_chaos(
        self,
        event_type: EventType,
        scenario_id: str,
        *,
        decision: str,
        reason: str,
    ) -> None:
        try:
            await crce.record(
                event_type=event_type,
                action=scenario_id,
                decision=decision,
                reason=reason,
                metadata={"chaos_scenario": scenario_id},
            )
        except Exception as exc:
            audit("chaos_crce_log_failed", event=event_type.value, scenario=scenario_id, error=str(exc))
