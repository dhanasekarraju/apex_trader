"""Chaos live-capital gate — final pre-live validation enforcement."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from shared.config import get_settings

_REPORT_PATH = Path(__file__).resolve().parents[2] / "data" / "chaos" / "reports" / "chaos_report.json"
_REQUIRED_CLASSIFICATION = "INSTITUTIONAL-GRADE"
_MIN_RESILIENCE_SCORE = 90


def _required_scenario_count() -> int:
    from services.chaos.scenarios import CHAOS_SCENARIOS

    return len(CHAOS_SCENARIOS)


class ChaosLiveGate:
    """Enforces chaos resilience requirements before live capital deployment."""

    @staticmethod
    def report_path() -> Path:
        return _REPORT_PATH

    @staticmethod
    def load_report(path: Path | None = None) -> dict | None:
        target = path or _REPORT_PATH
        if not target.is_file():
            return None
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def report_age_hours(report: dict | None) -> float | None:
        if not report:
            return None
        generated = report.get("generated_at")
        if not generated:
            return None
        try:
            ts = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
            return round((datetime.now(timezone.utc) - ts).total_seconds() / 3600, 2)
        except ValueError:
            return None

    @staticmethod
    def validate(report: dict | None, *, require_full_suite: bool = True) -> tuple[bool, list[str]]:
        if report is None:
            return False, ["No chaos resilience report — run full chaos suite first"]

        blockers: list[str] = []
        required_count = _required_scenario_count()

        if require_full_suite and not report.get("full_suite"):
            blockers.append(
                "Full chaos suite not completed — run POST /api/chaos/run?quick=false",
            )

        scenario_count = int(report.get("scenario_count", 0))
        if require_full_suite and scenario_count < required_count:
            blockers.append(
                f"Full suite requires {required_count} scenarios (got {scenario_count})",
            )

        classification = report.get("stability_classification", "")
        score = int(report.get("resilience_score", 0))
        safe = report.get("safe_for_live_capital", False)

        if classification != _REQUIRED_CLASSIFICATION:
            blockers.append(
                f"Stability classification must be {_REQUIRED_CLASSIFICATION} (got {classification})",
            )
        if score < _MIN_RESILIENCE_SCORE:
            blockers.append(
                f"Resilience score must be >= {_MIN_RESILIENCE_SCORE} (got {score})",
            )
        if not safe:
            blockers.append("safe_for_live_capital is false — chaos validation failed")

        max_age_hours = get_settings().chaos_report_max_age_hours
        if max_age_hours > 0:
            age = ChaosLiveGate.report_age_hours(report)
            if age is None:
                blockers.append("Chaos report timestamp missing or invalid")
            elif age > max_age_hours:
                blockers.append(
                    f"Chaos report expired ({age:.0f}h old, max {max_age_hours}h)",
                )

        return len(blockers) == 0, blockers

    @staticmethod
    def check_for_live(*, require_full_suite: bool = True) -> tuple[bool, list[str]]:
        """Validate chaos approval for LIVE mode — never bypassed."""
        return ChaosLiveGate.validate(
            ChaosLiveGate.load_report(),
            require_full_suite=require_full_suite,
        )

    @staticmethod
    def check(*, require_full_suite: bool = True) -> tuple[bool, list[str]]:
        """
        Validate chaos approval for non-live checks.
        CHAOS_GATE_ENFORCE=false skips validation (dev/CI only).
        LIVE mode must always use check_for_live().
        """
        cfg = get_settings()
        if not cfg.chaos_gate_enforce:
            return True, []
        return ChaosLiveGate.check_for_live(require_full_suite=require_full_suite)

    @staticmethod
    def status() -> dict:
        report = ChaosLiveGate.load_report()
        approved, blockers = ChaosLiveGate.validate(report)
        cfg = get_settings()
        age = ChaosLiveGate.report_age_hours(report)
        return {
            "live_capital_approved": approved,
            "blockers": blockers,
            "report_present": report is not None,
            "resilience_score": report.get("resilience_score") if report else None,
            "stability_classification": report.get("stability_classification") if report else None,
            "safe_for_live_capital": report.get("safe_for_live_capital") if report else False,
            "full_suite": report.get("full_suite") if report else False,
            "scenario_count": report.get("scenario_count") if report else 0,
            "required_scenario_count": _required_scenario_count(),
            "generated_at": report.get("generated_at") if report else None,
            "report_age_hours": age,
            "report_max_age_hours": cfg.chaos_report_max_age_hours,
            "report_valid": age is not None and age <= cfg.chaos_report_max_age_hours if report else False,
            "chaos_gate_enforce": cfg.chaos_gate_enforce,
            "live_capital_requirements": {
                "stability_classification": _REQUIRED_CLASSIFICATION,
                "min_resilience_score": _MIN_RESILIENCE_SCORE,
                "safe_for_live_capital": True,
                "full_suite": True,
                "scenario_count": _required_scenario_count(),
            },
        }
