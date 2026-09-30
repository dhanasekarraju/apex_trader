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
from services.regime.detector import Regime, RegimeAnalysis
from services.strategies.engine import Signal
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
        from services.control.reconciliation_state import clear_reconciliation_degraded
        from services.icb.signals import invalidate_signals_cache

        await clear_reconciliation_degraded()
        invalidate_signals_cache()
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
                monkeypatch_env = {
                    "ENFORCE_MARKET_HOURS": "false",
                    "TRADING_MODE": "paper",
                    "MAX_ENTRY_DEVIATION_PCT": "1000",
                }
                import os
                for k, v in monkeypatch_env.items():
                    os.environ[k] = v
                get_settings.cache_clear()
                self.orch.cfg = get_settings()
                self.orch.execution.refresh_broker()

                if scenario.fault_config.get("api_timeout_burst"):
                    threshold = get_settings().api_failure_threshold
                    for _ in range(threshold):
                        self.orch.execution._circuit.record_failure("Chaos: API timeout burst")

                probe_symbol = ("C" + scenario.id.replace("_", "").upper())[:15]

                if scenario.fault_config.get("broker_mode") == "position_mismatch":
                    from services.execution.reconciliation import reconcile_on_startup

                    reconciliation = await reconcile_on_startup(
                        broker=broker,
                        portfolio=self.orch.portfolio,
                        trades=self.orch.execution._trades,
                        trading_mode="live",
                    )
                    result.observations.append(
                        f"reconciliation_probe={reconciliation}"
                    )

                portfolio_before = len(self.orch.portfolio.state.positions)
                icb_result = await icb.authorize(
                    ICBAction.ANALYZE_SYMBOL,
                    {
                        "portfolio": self.orch.portfolio,
                        "trading_mode": "paper",
                        "symbol": probe_symbol,
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
                    original_regime = self.orch.regime.analyze
                    original_scan = self.orch.strategies.scan

                    def chaos_regime(_df):
                        return RegimeAnalysis(
                            regime=Regime.TREND_UP,
                            confidence=95.0,
                            volatility_pct=15.0,
                            trend_strength=2.0,
                            recommended_strategies=["trend_following"],
                            trade_allowed=True,
                            explanation="Deterministic chaos probe regime",
                        )

                    def chaos_scan(symbol, df, _regime, _allowed=None):
                        price = float(df["close"].iloc[-1])
                        return [
                            Signal(
                                symbol=symbol,
                                strategy="trend_following",
                                side="long",
                                entry=price,
                                stop_loss=price * 0.98,
                                take_profit=price * 1.04,
                                confidence=95.0,
                                qty_suggestion=1.0,
                                reasons=["Deterministic chaos execution probe"],
                            ),
                        ]

                    self.orch.regime.analyze = chaos_regime
                    self.orch.strategies.scan = chaos_scan
                    try:
                        decision = await self.orch.analyze_symbol(probe_symbol)
                    finally:
                        self.orch.regime.analyze = original_regime
                        self.orch.strategies.scan = original_scan

                    exec_info = decision.get("execution") or {}
                    result.risk_verdict = decision.get("risk_verdict", decision.get("action", ""))
                    result.execution_status = exec_info.get("status", decision.get("action", ""))
                    result.observations.append(
                        f"decision={decision.get('action')} reason={decision.get('reason', decision.get('risk_reason', ''))}"
                    )
                    if exec_info:
                        result.observations.append(f"execution={exec_info}")

                    if scenario.id == "state_duplicate_events" and exec_info.get("client_order_id"):
                        duplicate = await self.orch.execution.place_order(
                            OrderRequest(
                                symbol=probe_symbol,
                                side="long",
                                qty=max(1, int(decision.get("qty") or 1)),
                                order_type=OrderType.MARKET,
                                stop_price=float(decision.get("stop_loss") or 1),
                                take_profit=float(decision.get("take_profit") or 2),
                                strategy=str(decision.get("strategy") or "trend_following"),
                                client_order_id=exec_info["client_order_id"],
                            ),
                            float(decision.get("entry") or 100),
                        )
                        result.observations.append(
                            f"duplicate_replay={duplicate.status.value}:{duplicate.message}"
                        )
                        if (
                            duplicate.status.value != "rejected"
                            or "duplicate" not in duplicate.message.lower()
                        ):
                            result.failures.append(
                                "Duplicate replay was not blocked idempotently"
                            )

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

                if scenario.fault_config.get("broker_mode") == "position_mismatch":
                    from services.control.reconciliation_state import is_reconciliation_degraded

                    caught = await is_reconciliation_degraded()
                    result.portfolio_consistent = bool(caught and result.icb_decision == "DENY")
                    result.reconciliation_ok = result.portfolio_consistent
                    if not caught:
                        result.failures.append("Injected portfolio mismatch was not detected")
                    if result.icb_decision != "DENY":
                        result.failures.append("ICB did not deny after portfolio mismatch")
                else:
                    internal_positions = {
                        p.symbol.upper(): float(p.qty)
                        for p in self.orch.portfolio.state.positions
                    }
                    broker_positions = {
                        str(p.get("symbol", "")).upper(): float(p.get("qty", 0))
                        for p in positions
                    }
                    result.portfolio_consistent = internal_positions == broker_positions
                    if not result.portfolio_consistent:
                        result.failures.append(
                            f"Portfolio mismatch: internal={internal_positions} broker={broker_positions}",
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

                safety_ok = self._validate_safety(result, scenario, portfolio_before)
                expectations_ok = self._validate_expectations(result, scenario)
                result.safe = safety_ok and expectations_ok and not result.failures
                result.passed = result.safe

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
