"""ICB scoring and final decision synthesis."""

from __future__ import annotations

from typing import Any

from services.icb.actions import ICBAction
from services.icb.decisions import ICBDecision, ICBResult
from services.icb.policies import apply_policies
from services.icb.signals import SystemSignals
from services.icb.state import ICBState
from shared.config import Settings


class ICBevaluator:
    def evaluate(
        self,
        action: ICBAction,
        signals: SystemSignals,
        icb_state: ICBState,
        cfg: Settings,
        *,
        context: dict[str, Any] | None = None,
    ) -> ICBResult:
        context = context or {}
        risk_summary = signals.risk_status or "unknown"

        policy = apply_policies(action, signals, icb_state, cfg)
        if policy:
            if policy.decision in (ICBDecision.DENY, ICBDecision.FREEZE, ICBDecision.ESCALATE):
                return ICBResult(
                    decision=policy.decision,
                    reason=policy.reason,
                    icb_state=policy.suggested_state or icb_state,
                    risk_summary=risk_summary,
                    sg_conflict=signals.sg_conflict,
                    crce_integrity=signals.crce_integrity,
                )
            if policy.decision == ICBDecision.DEGRADE:
                return ICBResult(
                    decision=ICBDecision.DEGRADE,
                    reason=policy.reason,
                    icb_state=policy.suggested_state or icb_state,
                    size_multiplier=policy.size_multiplier,
                    risk_summary=risk_summary,
                    sg_conflict=signals.sg_conflict,
                    crce_integrity=signals.crce_integrity,
                )

        if context.get("risk_denied"):
            return ICBResult(
                decision=ICBDecision.DENY,
                reason=str(context.get("risk_reason", "Risk engine denied")),
                icb_state=icb_state,
                risk_summary=risk_summary,
                sg_conflict=signals.sg_conflict,
                crce_integrity=signals.crce_integrity,
                layer_denies=["risk"],
            )

        if icb_state == ICBState.WATCH and signals.issues:
            return ICBResult(
                decision=ICBDecision.ALLOW,
                reason=f"WATCH: {'; '.join(signals.issues[:3])}",
                icb_state=icb_state,
                risk_summary=risk_summary,
                sg_conflict=signals.sg_conflict,
                crce_integrity=signals.crce_integrity,
            )

        return ICBResult(
            decision=ICBDecision.ALLOW,
            reason="Institutional coherence verified",
            icb_state=icb_state,
            risk_summary=risk_summary,
            sg_conflict=signals.sg_conflict,
            crce_integrity=signals.crce_integrity,
        )
