"""Strategy governance policy evaluation."""

from __future__ import annotations

from services.governance.models import GovernanceDecision, StrategyRecord
from services.governance.states import DEFAULT_THROTTLE_FACTOR, TRADEABLE_STATES, StrategyState


def evaluate_strategy(
    record: StrategyRecord,
    *,
    regime: str,
    regime_recommended: list[str],
    lab_enabled: bool,
) -> GovernanceDecision:
    """Return whether a strategy may operate under current conditions."""
    if record.state == StrategyState.KILLED:
        return GovernanceDecision(
            allowed=False,
            state=record.state,
            reason=record.reason or "Strategy permanently killed",
        )

    if record.state == StrategyState.DISABLED:
        return GovernanceDecision(
            allowed=False,
            state=record.state,
            reason=record.reason or "Strategy disabled by governance",
        )

    if record.state == StrategyState.PAUSED:
        return GovernanceDecision(
            allowed=False,
            state=record.state,
            reason=record.reason or "Strategy paused by governance",
        )

    if not lab_enabled:
        return GovernanceDecision(
            allowed=False,
            state=record.state,
            reason="Strategy lab performance gate closed",
        )

    if record.name not in regime_recommended:
        return GovernanceDecision(
            allowed=False,
            state=record.state,
            reason=f"Regime {regime} — {record.name} not in recommended set",
        )

    if record.state == StrategyState.THROTTLED:
        factor = record.throttle_factor or DEFAULT_THROTTLE_FACTOR
        return GovernanceDecision(
            allowed=True,
            state=record.state,
            reason=record.reason or f"Throttled to {factor:.0%} size",
            size_multiplier=factor,
        )

    if record.state == StrategyState.ACTIVE:
        return GovernanceDecision(
            allowed=True,
            state=record.state,
            reason="Active — regime aligned",
            size_multiplier=1.0,
        )

    return GovernanceDecision(
        allowed=False,
        state=record.state,
        reason=f"Unknown governance state {record.state.value}",
    )


def is_tradeable_state(state: StrategyState) -> bool:
    return state in TRADEABLE_STATES
