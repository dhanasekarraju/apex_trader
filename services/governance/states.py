"""Strategy lifecycle states — Strategy Governance Engine."""

from __future__ import annotations

from enum import Enum


class StrategyState(str, Enum):
    ACTIVE = "ACTIVE"
    THROTTLED = "THROTTLED"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"
    KILLED = "KILLED"


TRADEABLE_STATES = frozenset({StrategyState.ACTIVE, StrategyState.THROTTLED})

DEFAULT_THROTTLE_FACTOR = 0.5
