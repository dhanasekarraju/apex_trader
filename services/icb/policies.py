"""Institutional policy rules for ICB."""

from __future__ import annotations

from dataclasses import dataclass

from services.icb.actions import ICBAction, TRADING_ACTIONS
from services.icb.decisions import ICBDecision
from services.icb.signals import SystemSignals
from services.icb.state import ICBState
from shared.config import Settings


@dataclass
class PolicyOutcome:
    decision: ICBDecision
    reason: str
    size_multiplier: float = 1.0
    suggested_state: ICBState | None = None


def apply_policies(
    action: ICBAction,
    signals: SystemSignals,
    icb_state: ICBState,
    cfg: Settings,
) -> PolicyOutcome | None:
    """Return a blocking/degrading outcome, or None to allow."""
    if icb_state == ICBState.EMERGENCY_LOCK and action not in TRADING_ACTIONS:
        return None

    if signals.sg_conflict and action in TRADING_ACTIONS:
        return PolicyOutcome(
            ICBDecision.DENY,
            f"Governance conflict: {', '.join(signals.sg_conflict_strategies)}",
            suggested_state=ICBState.RESTRICTED,
        )

    if cfg.trading_mode == "live" and signals.crce_integrity != "ok":
        return PolicyOutcome(
            ICBDecision.DENY,
            "CRCE integrity failed — LIVE trading blocked",
            suggested_state=ICBState.FROZEN,
        )

    if signals.drift_count >= cfg.icb_drift_restrict_threshold:
        if action in (ICBAction.AUTONOMOUS_TICK, ICBAction.START_AUTONOMOUS):
            return PolicyOutcome(
                ICBDecision.DENY,
                f"Portfolio drift detected ({signals.drift_count}) — autonomous halted",
                suggested_state=ICBState.RESTRICTED,
            )
        if action in TRADING_ACTIONS:
            return PolicyOutcome(
                ICBDecision.ESCALATE,
                f"Portfolio drift detected ({signals.drift_count}) — admin review advised",
                suggested_state=ICBState.RESTRICTED,
            )

    if signals.reconciliation_degraded and action in TRADING_ACTIONS:
        return PolicyOutcome(
            ICBDecision.DEGRADE,
            "Reconciliation degraded — reduced sizing",
            size_multiplier=cfg.icb_degraded_size_multiplier,
            suggested_state=ICBState.DEGRADED,
        )

    if signals.recent_trades_1h >= cfg.icb_max_trades_per_hour and action in TRADING_ACTIONS:
        return PolicyOutcome(
            ICBDecision.FREEZE,
            f"Trade velocity limit exceeded ({signals.recent_trades_1h}/hr)",
            suggested_state=ICBState.FROZEN,
        )

    if signals.portfolio_heat_pct >= cfg.icb_heat_degrade_pct and action in TRADING_ACTIONS:
        return PolicyOutcome(
            ICBDecision.DEGRADE,
            f"Portfolio heat {signals.portfolio_heat_pct:.1f}% — size reduced",
            size_multiplier=cfg.icb_degraded_size_multiplier,
            suggested_state=ICBState.DEGRADED,
        )

    if signals.correlated_exposure >= cfg.max_correlated_exposure_pct and action == ICBAction.PLACE_ORDER:
        return PolicyOutcome(
            ICBDecision.DEGRADE,
            f"Correlated exposure {signals.correlated_exposure:.0f}% — throttled",
            size_multiplier=0.5,
            suggested_state=ICBState.WATCH,
        )

    if signals.autonomous_buy_spike and action == ICBAction.AUTONOMOUS_TICK:
        return PolicyOutcome(
            ICBDecision.ESCALATE,
            "Autonomous buy spike — cycle flagged",
            suggested_state=ICBState.RESTRICTED,
        )

    if icb_state == ICBState.DEGRADED and action in TRADING_ACTIONS:
        return PolicyOutcome(
            ICBDecision.DEGRADE,
            "ICB DEGRADED — institutional size reduction active",
            size_multiplier=cfg.icb_degraded_size_multiplier,
        )

    if icb_state == ICBState.WATCH and signals.issues:
        return PolicyOutcome(
            ICBDecision.ALLOW,
            f"WATCH — elevated scrutiny: {'; '.join(signals.issues[:2])}",
        )

    return None
