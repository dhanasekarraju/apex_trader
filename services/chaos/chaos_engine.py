"""
Chaos Testing + Live Market Hardening Layer.

Validates system resilience under controlled failure injection before live capital deployment.
"""

from __future__ import annotations

from typing import Iterable

from services.chaos.resilience_reporter import ResilienceReporter
from services.chaos.scenario_runner import ScenarioResult, ScenarioRunner
from services.chaos.scenarios import CHAOS_SCENARIOS, SCENARIO_BY_ID, ChaosScenario
from services.core.orchestrator import TradingOrchestrator
from shared.logging import audit


class ChaosEngine:
    """Orchestrates chaos scenario suite execution."""

    def __init__(self, orchestrator: TradingOrchestrator | None = None) -> None:
        self.orch = orchestrator or TradingOrchestrator()
        self.runner = ScenarioRunner(self.orch)
        self.last_results: list[ScenarioResult] = []
        self.last_report: dict | None = None

    async def run_scenario(self, scenario_id: str) -> ScenarioResult:
        scenario = SCENARIO_BY_ID.get(scenario_id)
        if scenario is None:
            raise ValueError(f"Unknown chaos scenario: {scenario_id}")
        result = await self.runner.run(scenario)
        self.last_results = [result]
        return result

    async def run_suite(
        self,
        scenarios: Iterable[ChaosScenario] | None = None,
        *,
        quick: bool = False,
    ) -> dict:
        """Run chaos scenarios and generate institutional resilience report."""
        selected = list(scenarios or CHAOS_SCENARIOS)
        full_suite = scenarios is None and not quick
        if quick:
            selected = selected[:6]

        results: list[ScenarioResult] = []
        for scenario in selected:
            audit("chaos_suite_scenario", scenario=scenario.id)
            result = await self.runner.run(scenario)
            results.append(result)

        self.last_results = results
        reporter = ResilienceReporter(results)
        self.last_report = reporter.generate(
            full_suite=full_suite and len(selected) == len(CHAOS_SCENARIOS),
            scenario_count=len(selected),
        )
        audit(
            "chaos_suite_complete",
            score=self.last_report["resilience_score"],
            classification=self.last_report["stability_classification"],
        )
        return self.last_report

    async def run_category(self, category: str) -> dict:
        filtered = [s for s in CHAOS_SCENARIOS if s.category.value == category]
        return await self.run_suite(filtered)


chaos_engine = ChaosEngine()
