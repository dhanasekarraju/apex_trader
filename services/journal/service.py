"""Trade journal — every decision logged with full context."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
JOURNAL_DIR = _ROOT / "data" / "journal"


@dataclass
class JournalEntry:
    symbol: str
    action: str
    regime: str
    strategy: str
    entry_reason: str
    exit_reason: str | None
    risk_score: float
    confidence: float
    position_size: float
    entry_price: float
    stop_loss: float
    take_profit: float
    outcome: str | None
    pnl: float | None
    risk_checks: list[dict]
    mode: str
    timestamp: str


class TradeJournal:
    def __init__(self) -> None:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        self.entries: list[JournalEntry] = []

    def record(self, **kwargs) -> JournalEntry:
        entry = JournalEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            exit_reason=None,
            outcome=None,
            pnl=None,
            **kwargs,
        )
        self.entries.insert(0, entry)
        self._persist(entry)
        return entry

    def close_trade(self, symbol: str, exit_reason: str, pnl: float, outcome: str) -> None:
        for e in self.entries:
            if e.symbol == symbol and e.outcome is None:
                e.exit_reason = exit_reason
                e.pnl = pnl
                e.outcome = outcome
                break

    def _persist(self, entry: JournalEntry) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = JOURNAL_DIR / f"journal_{day}.jsonl"
        with path.open("a") as f:
            f.write(json.dumps(asdict(entry), default=str) + "\n")

    def weekly_report(self) -> dict:
        closed = [e for e in self.entries if e.outcome]
        wins = [e for e in closed if (e.pnl or 0) > 0]
        return {
            "total_decisions": len(self.entries),
            "closed_trades": len(closed),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "total_pnl": round(sum(e.pnl or 0 for e in closed), 2),
            "by_strategy": self._group_by("strategy"),
            "by_regime": self._group_by("regime"),
        }

    def monthly_report(self) -> dict:
        w = self.weekly_report()
        w["period"] = "monthly"
        return w

    def _group_by(self, field: str) -> dict:
        groups: dict[str, list] = {}
        for e in self.entries:
            key = getattr(e, field, "unknown")
            groups.setdefault(key, []).append(e.pnl or 0)
        return {k: {"count": len(v), "pnl": round(sum(v), 2)} for k, v in groups.items()}
