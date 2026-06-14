"""Compliance report generator — JSON + Markdown."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.compliance.drift import DriftDetector
from services.compliance.replay import ReplayEngine
from services.compliance.store import EventStore

_REPORT_DIR = Path(__file__).resolve().parents[2] / "data" / "compliance" / "reports"


class ComplianceReportGenerator:
    def __init__(
        self,
        store: EventStore | None = None,
        replay: ReplayEngine | None = None,
        drift: DriftDetector | None = None,
    ) -> None:
        self.store = store or EventStore()
        self.replay = replay or ReplayEngine(self.store)
        self.drift = drift or DriftDetector(self.store)

    def generate(
        self,
        *,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        reference_snapshot: dict | None = None,
    ) -> dict[str, Any]:
        integrity = self.store.verify_chain()
        events = self.store.load_range(start_time, end_time)
        replay_result = self.replay.replay(
            start_time,
            end_time,
            reference_snapshot=reference_snapshot,
        )
        drifts = self.drift.scan(events)
        chains = self._decision_chains(events)

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period": {
                "start": start_time.isoformat() if start_time else None,
                "end": end_time.isoformat() if end_time else None,
            },
            "integrity": integrity,
            "event_count": len(events),
            "replay": replay_result,
            "drifts": drifts,
            "decision_chains": chains[:100],
            "kill_switch_events": [
                e for e in events if e.get("event_type") == "KILL_SWITCH_TRIGGERED"
            ],
        }

        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = _REPORT_DIR / f"compliance_{stamp}.json"
        md_path = _REPORT_DIR / f"compliance_{stamp}.md"
        json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        report["paths"] = {"json": str(json_path), "markdown": str(md_path)}
        return report

    @staticmethod
    def _decision_chains(events: list[dict]) -> list[dict]:
        chains: list[dict] = []
        by_symbol: dict[str, list] = {}
        for ev in events:
            sym = (ev.get("symbol") or "").upper()
            if not sym:
                continue
            by_symbol.setdefault(sym, []).append(
                {
                    "timestamp": ev.get("timestamp"),
                    "event_type": ev.get("event_type"),
                    "decision": ev.get("decision"),
                    "reason": ev.get("reason"),
                }
            )
        for sym, steps in by_symbol.items():
            if any(s["event_type"] in ("ORDER_FILLED", "ORDER_PLACED") for s in steps):
                chains.append({"symbol": sym, "chain": steps})
        return chains

    @staticmethod
    def _to_markdown(report: dict) -> str:
        lines = [
            "# Apex Trader Compliance Report",
            "",
            f"Generated: {report.get('generated_at')}",
            "",
            "## Integrity",
            f"- Hash chain valid: **{report['integrity'].get('valid')}**",
            f"- Events verified: **{report['integrity'].get('events', 0)}**",
            "",
            "## Replay Summary",
            f"- Events processed: {report['replay'].get('events_processed')}",
            f"- Reconstructed equity: {report['replay']['reconstructed'].get('equity')}",
            f"- Open positions: {report['replay']['reconstructed'].get('open_positions')}",
            "",
            "## Drift Findings",
        ]
        drifts = report.get("drifts") or []
        if not drifts:
            lines.append("- None detected")
        for d in drifts:
            lines.append(
                f"- **{d['severity']}** `{d['drift_type']}` {d['symbol']}: "
                f"expected `{d['expected']}` vs actual `{d['actual']}`"
            )
        lines.extend(["", "## Kill Switch Timeline"])
        for k in report.get("kill_switch_events") or []:
            lines.append(f"- {k.get('timestamp')}: {k.get('reason', 'triggered')}")
        return "\n".join(lines) + "\n"
