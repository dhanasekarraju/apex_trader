"""Strategy governance data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from services.governance.states import StrategyState


@dataclass
class StrategyRecord:
    name: str
    state: StrategyState = StrategyState.ACTIVE
    reason: str = ""
    throttle_factor: float = 0.5
    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_by: str = "system"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "reason": self.reason,
            "throttle_factor": self.throttle_factor,
            "trades": self.trades,
            "wins": self.wins,
            "total_pnl": round(self.total_pnl, 2),
            "win_rate": round(self.win_rate, 1),
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> StrategyRecord:
        return cls(
            name=data["name"],
            state=StrategyState(data.get("state", StrategyState.ACTIVE.value)),
            reason=data.get("reason", ""),
            throttle_factor=float(data.get("throttle_factor", 0.5)),
            trades=int(data.get("trades", 0)),
            wins=int(data.get("wins", 0)),
            total_pnl=float(data.get("total_pnl", 0)),
            win_rate=float(data.get("win_rate", 0)),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
            updated_by=data.get("updated_by", "system"),
        )


@dataclass
class GovernanceDecision:
    allowed: bool
    state: StrategyState
    reason: str
    size_multiplier: float = 1.0
