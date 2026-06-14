"""Chaos resilience metrics and institutional report generation."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from services.chaos.scenario_runner import ScenarioResult
from services.chaos.scenarios import SCENARIO_BY_ID, CHAOS_SCENARIOS

_REPORT_DIR = Path(__file__).resolve().parents[2] / "data" / "chaos" / "reports"
_MIN_LIVE_SCORE = 90


class StabilityClass(str, Enum):
    UNSAFE = "UNSAFE"
    DEGRADED = "DEGRADED"
    STABLE = "STABLE"
    INSTITUTIONAL_GRADE = "INSTITUTIONAL-GRADE"


class ResilienceReporter:
    def __init__(self, results: list[ScenarioResult]) -> None:
        self.results = results

    def compute_metrics(self) -> dict:
        total = len(self.results) or 1
        passed = sum(1 for r in self.results if r.passed)
        safe = sum(1 for r in self.results if r.safe)
        executed = [r for r in self.results if r.execution_status.lower() in ("filled", "partial")]
        rejected = [r for r in self.results if r.execution_status.lower() == "rejected"]
        kill_correct = sum(
            1 for r in self.results
            if r.kill_switch_triggered or r.safe_mode_triggered or r.icb_decision == "DENY"
        )
        reconciled = sum(1 for r in self.results if r.reconciliation_ok)
        duplicates = sum(1 for r in self.results if r.duplicate_detected)
        avg_recovery = sum(r.recovery_time_ms for r in self.results) / total

        return {
            "order_success_rate_under_failure_pct": round(len(executed) / total * 100, 1),
            "order_rejection_rate_pct": round(len(rejected) / total * 100, 1),
            "kill_switch_accuracy_pct": round(kill_correct / total * 100, 1),
            "reconciliation_drift_rate_pct": round((total - reconciled) / total * 100, 1),
            "duplicate_order_detection_rate_pct": round((1 - duplicates / total) * 100, 1),
            "system_recovery_time_ms": round(avg_recovery, 1),
            "portfolio_consistency_score": round(safe / total * 100, 1),
            "scenario_pass_rate_pct": round(passed / total * 100, 1),
        }

    def resilience_score(self) -> int:
        m = self.compute_metrics()
        score = (
            m["portfolio_consistency_score"] * 0.35
            + m["kill_switch_accuracy_pct"] * 0.25
            + m["scenario_pass_rate_pct"] * 0.25
            + (100 - m["reconciliation_drift_rate_pct"]) * 0.15
        )
        return max(0, min(100, int(round(score))))

    def classify(self) -> str:
        score = self.resilience_score()
        unsafe = any(not r.safe for r in self.results)
        if unsafe or score < 50:
            return StabilityClass.UNSAFE.value
        if score < 70:
            return StabilityClass.DEGRADED.value
        if score < _MIN_LIVE_SCORE:
            return StabilityClass.STABLE.value
        return StabilityClass.INSTITUTIONAL_GRADE.value

    def safe_for_live_capital(self) -> bool:
        score = self.resilience_score()
        classification = self.classify()
        return (
            classification == StabilityClass.INSTITUTIONAL_GRADE.value
            and score >= _MIN_LIVE_SCORE
            and not any(not r.safe for r in self.results)
        )

    def failure_breakdown(self) -> list[dict]:
        breakdown: list[dict] = []
        for r in self.results:
            if r.failures:
                breakdown.append({
                    "scenario_id": r.scenario_id,
                    "failures": r.failures,
                    "observations": r.observations,
                })
        return breakdown

    def failure_breakdown_by_category(self) -> dict[str, list[dict]]:
        by_category: dict[str, list[dict]] = {}
        for r in self.results:
            if not r.failures:
                continue
            scenario = SCENARIO_BY_ID.get(r.scenario_id)
            category = scenario.category.value if scenario else "unknown"
            by_category.setdefault(category, []).append({
                "scenario_id": r.scenario_id,
                "failures": r.failures,
            })
        return by_category

    def recovery_timeline(self) -> list[dict]:
        return [
            {
                "scenario_id": r.scenario_id,
                "duration_ms": r.duration_ms,
                "recovery_time_ms": r.recovery_time_ms,
                "passed": r.passed,
            }
            for r in self.results
        ]

    def trade_integrity(self) -> dict:
        return {
            "portfolio_consistent_scenarios": sum(1 for r in self.results if r.portfolio_consistent),
            "total_scenarios": len(self.results),
            "duplicate_incidents": sum(1 for r in self.results if r.duplicate_detected),
            "unreconciled": sum(1 for r in self.results if not r.reconciliation_ok),
        }

    def generate(
        self,
        *,
        path: Path | None = None,
        full_suite: bool = False,
        scenario_count: int | None = None,
    ) -> dict:
        score = self.resilience_score()
        classification = self.classify()
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "full_suite": full_suite,
            "scenario_count": scenario_count or len(self.results),
            "resilience_score": score,
            "stability_classification": classification,
            "metrics": self.compute_metrics(),
            "failure_breakdown": self.failure_breakdown(),
            "failure_breakdown_by_category": self.failure_breakdown_by_category(),
            "recovery_timeline": self.recovery_timeline(),
            "trade_integrity": self.trade_integrity(),
            "scenario_results": [asdict(r) for r in self.results],
            "safe_for_live_capital": self.safe_for_live_capital(),
            "live_capital_requirements": {
                "stability_classification": StabilityClass.INSTITUTIONAL_GRADE.value,
                "min_resilience_score": _MIN_LIVE_SCORE,
                "safe_for_live_capital": True,
                "full_suite": True,
                "scenario_count": len(CHAOS_SCENARIOS),
            },
        }
        out = path or _REPORT_DIR / "chaos_report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["report_path"] = str(out)
        return report
