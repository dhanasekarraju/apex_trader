"""ICB decision model — simplified authority outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field

from services.icb.system_state import SystemState


class ICBDecision:
    ALLOW = "ALLOW"
    DENY = "DENY"
    PAUSE_SYSTEM = "PAUSE_SYSTEM"
    EMERGENCY_LOCK = "EMERGENCY_LOCK"


@dataclass
class ICBResult:
    decision: str
    reason: str
    system_state: SystemState
    latency_ms: float = 0.0
    crce_integrity: str = "ok"
    layer_denies: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.decision == ICBDecision.ALLOW
