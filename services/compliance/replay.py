"""Deterministic replay of compliance events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from services.compliance.events import EventType
from services.compliance.store import EventStore


@dataclass
class ReplayState:
    equity: float = 1_000_000.0
    cash: float = 1_000_000.0
    daily_pnl: float = 0.0
    positions: dict[str, dict] = field(default_factory=dict)
    system_state: str = "PAPER_TRADING"
    timeline: list[dict] = field(default_factory=list)


class ReplayEngine:
    """Rebuild portfolio and control state from immutable event log."""

    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or EventStore()

    def replay(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        *,
        initial_capital: float = 1_000_000.0,
        reference_snapshot: dict | None = None,
    ) -> dict[str, Any]:
        events = self.store.load_range(start_time, end_time)
        state = ReplayState(equity=initial_capital, cash=initial_capital)
        missing: list[str] = []
        applied = 0

        for ev in events:
            et = ev.get("event_type", "")
            snap = ev.get("state_snapshot") or {}
            if snap.get("system_state"):
                state.system_state = snap["system_state"]

            if et == EventType.KILL_SWITCH_TRIGGERED.value:
                state.system_state = "KILL_SWITCHED"

            if et in (EventType.PORTFOLIO_UPDATE.value, EventType.ORDER_FILLED.value):
                self._apply_portfolio_snapshot(state, snap.get("portfolio") or snap)
                applied += 1

            if et == EventType.ORDER_EXITED.value:
                sym = (ev.get("symbol") or "").upper()
                state.positions.pop(sym, None)
                pf = snap.get("portfolio") or {}
                if pf:
                    state.equity = pf.get("equity", state.equity)
                    state.cash = pf.get("cash", state.cash)
                    state.daily_pnl = pf.get("daily_pnl", state.daily_pnl)
                applied += 1

            if et == EventType.CONTROL_DECISION.value and ev.get("decision") == "DENY":
                state.timeline.append(
                    {"ts": ev["timestamp"], "blocked": ev.get("action"), "reason": ev.get("reason")}
                )

            if not ev.get("event_id"):
                missing.append(f"missing event_id at {ev.get('timestamp')}")

        divergence = self._compare_reference(state, reference_snapshot)

        return {
            "events_processed": len(events),
            "events_applied": applied,
            "reconstructed": {
                "equity": round(state.equity, 2),
                "cash": round(state.cash, 2),
                "daily_pnl": round(state.daily_pnl, 2),
                "open_positions": len(state.positions),
                "positions": list(state.positions.values()),
                "system_state": state.system_state,
            },
            "timeline": state.timeline[-50:],
            "missing_events": missing,
            "divergence": divergence,
        }

    @staticmethod
    def _apply_portfolio_snapshot(state: ReplayState, pf: dict) -> None:
        if not pf:
            return
        state.equity = pf.get("equity", state.equity)
        state.cash = pf.get("cash", state.cash)
        state.daily_pnl = pf.get("daily_pnl", state.daily_pnl)
        state.positions = {
            p["symbol"].upper(): p for p in pf.get("positions", []) if p.get("symbol")
        }

    @staticmethod
    def _compare_reference(state: ReplayState, reference: dict | None) -> dict:
        if not reference:
            return {"checked": False}
        ref_pf = reference.get("portfolio") or reference
        mismatches: list[dict] = []
        for key in ("equity", "cash", "daily_pnl"):
            expected = ref_pf.get(key)
            actual = getattr(state, key, None)
            if expected is not None and abs(float(expected) - float(actual)) > 0.01:
                mismatches.append({"field": key, "expected": expected, "actual": actual})
        ref_pos = ref_pf.get("positions") or []
        if len(ref_pos) != len(state.positions):
            mismatches.append(
                {
                    "field": "open_positions",
                    "expected": len(ref_pos),
                    "actual": len(state.positions),
                }
            )
        return {"checked": True, "matches": not mismatches, "mismatches": mismatches}
