"""
Institutional Control Brain — single authority gate.

Pipeline: ICB → Risk → Execution → Broker → Portfolio → CRCE (audit)
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from services.icb.actions import ICBAction
from services.icb.decisions import ICBDecision, ICBResult
from services.icb.signals import collect_signals
from services.icb.system_state import (
    TRADING_ACTIONS,
    SystemState,
    get_kill_switch_latched,
    get_state_reason,
    get_system_state,
    persist_system_state,
    set_kill_switch_latched,
)
from services.icb.telemetry import log_icb_crce, log_icb_decision
from shared.config import get_settings
from shared.logging import audit

_IST = ZoneInfo("Asia/Kolkata")


class InstitutionalControlBrain:
    """Single authority layer — system health and trading permission."""

    def __init__(self) -> None:
        self.cfg = get_settings()
        self._healthy = True
        self._safe_mode_reason = ""

    @property
    def healthy(self) -> bool:
        return self._healthy

    async def authorize(self, action: ICBAction, context: dict[str, Any]) -> ICBResult:
        start = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self._authorize_inner(action, context),
                timeout=self.cfg.icb_timeout_sec,
            )
        except asyncio.TimeoutError:
            await self.enter_safe_mode("ICB authorization timeout")
            result = ICBResult(
                ICBDecision.DENY,
                "ICB timeout — SAFE_MODE engaged",
                SystemState.SAFE_MODE,
                crce_integrity="failed",
            )
        except Exception as exc:
            await self.enter_safe_mode(str(exc))
            result = ICBResult(
                ICBDecision.DENY,
                f"ICB error: {exc}",
                SystemState.SAFE_MODE,
                crce_integrity="failed",
            )

        result.latency_ms = (time.perf_counter() - start) * 1000
        log_icb_decision(action, result)
        await log_icb_crce(action, result)
        return result

    async def _authorize_inner(self, action: ICBAction, context: dict[str, Any]) -> ICBResult:
        state = await self._resolve_state(context)
        reason = await get_state_reason()

        if action == ICBAction.RUN_CHAOS:
            cfg = get_settings()
            if cfg.trading_mode == "live":
                return ICBResult(
                    ICBDecision.DENY,
                    "Chaos testing forbidden while LIVE mode is active",
                    state,
                )
            from services.autonomous.state import is_autonomous_running

            if await is_autonomous_running():
                return ICBResult(
                    ICBDecision.DENY,
                    "Stop autonomous engine before running chaos tests",
                    state,
                )
            return ICBResult(ICBDecision.ALLOW, "Chaos testing authorized", state)

        if state == SystemState.EMERGENCY_LOCK:
            if action not in (ICBAction.RECONCILIATION, ICBAction.ADMIN_RESET, ICBAction.STOP_AUTONOMOUS):
                return ICBResult(
                    ICBDecision.EMERGENCY_LOCK,
                    reason or "EMERGENCY_LOCK — manual reset required",
                    state,
                )

        if state == SystemState.SAFE_MODE and action.value in TRADING_ACTIONS:
            return ICBResult(
                ICBDecision.DENY,
                self._safe_mode_reason or reason or "SAFE_MODE active",
                state,
            )

        if state == SystemState.PAUSED and action.value in (
            ICBAction.PLACE_ORDER.value,
            ICBAction.START_AUTONOMOUS.value,
            ICBAction.AUTONOMOUS_TICK.value,
        ):
            return ICBResult(
                ICBDecision.PAUSE_SYSTEM,
                reason or "System PAUSED — analysis only",
                state,
            )

        signals = await collect_signals(context)

        if signals.reconciliation_degraded and action.value in TRADING_ACTIONS:
            return ICBResult(
                ICBDecision.DENY,
                "Broker reconciliation degraded — trading stopped",
                SystemState.SAFE_MODE if action == ICBAction.PLACE_ORDER else state,
                crce_integrity=signals.crce_integrity,
            )

        if signals.drift_count > 0 and action.value in (
            ICBAction.PLACE_ORDER.value,
            ICBAction.AUTONOMOUS_TICK.value,
            ICBAction.START_AUTONOMOUS.value,
        ):
            return ICBResult(
                ICBDecision.DENY,
                f"Portfolio drift detected ({signals.drift_count})",
                state,
                crce_integrity=signals.crce_integrity,
            )

        if self.cfg.trading_mode == "live" and signals.crce_integrity != "ok":
            return ICBResult(
                ICBDecision.DENY,
                "CRCE integrity failure — LIVE blocked",
                SystemState.SAFE_MODE,
                crce_integrity=signals.crce_integrity,
            )

        if self.cfg.trading_mode == "live" and action.value in TRADING_ACTIONS:
            from services.chaos.live_gate import ChaosLiveGate

            approved, chaos_blockers = ChaosLiveGate.check_for_live(require_full_suite=True)
            if not approved:
                return ICBResult(
                    ICBDecision.DENY,
                    chaos_blockers[0] if chaos_blockers else "Chaos gate failed — LIVE blocked",
                    state,
                    crce_integrity=signals.crce_integrity,
                )

        if action.value in TRADING_ACTIONS:
            risk_status = str(context.get("risk_status", ""))
            if risk_status in ("DANGER", "HALTED"):
                return ICBResult(
                    ICBDecision.DENY,
                    f"Risk status {risk_status} — trading blocked",
                    state,
                    crce_integrity=signals.crce_integrity,
                )

        if action.value in (
            ICBAction.ANALYZE_SYMBOL.value,
            ICBAction.PLACE_ORDER.value,
            ICBAction.START_AUTONOMOUS.value,
        ):
            cfg = get_settings()
            if cfg.enforce_market_hours:
                open_ok, msg = self._market_open()
                if not open_ok:
                    return ICBResult(ICBDecision.DENY, msg, state, crce_integrity=signals.crce_integrity)

        portfolio = context.get("portfolio")
        if portfolio is not None and portfolio.is_trading_halted():
            if action.value in TRADING_ACTIONS and action != ICBAction.PLACE_EXIT:
                return ICBResult(
                    ICBDecision.EMERGENCY_LOCK,
                    "Trading halted — kill switch active",
                    SystemState.EMERGENCY_LOCK,
                )

        return ICBResult(
            ICBDecision.ALLOW,
            "Authority checks passed",
            state,
            crce_integrity=signals.crce_integrity,
        )

    async def _resolve_state(self, context: dict[str, Any]) -> SystemState:
        if not self._healthy:
            return SystemState.SAFE_MODE

        if await get_kill_switch_latched():
            return SystemState.EMERGENCY_LOCK

        portfolio = context.get("portfolio")
        if portfolio is not None:
            if getattr(portfolio.state, "black_swan_mode", False):
                return SystemState.EMERGENCY_LOCK
            if portfolio.is_trading_halted() and await get_kill_switch_latched():
                return SystemState.EMERGENCY_LOCK

        if context.get("watchdog_safe_mode"):
            return SystemState.SAFE_MODE

        return await get_system_state()

    def _market_open(self) -> tuple[bool, str]:
        if not self.cfg.enforce_market_hours:
            return True, "Market hours check disabled"
        ts = datetime.now(_IST)
        if ts.weekday() >= 5:
            return False, "Market closed — weekend"
        if dt_time(9, 15) <= ts.time() <= dt_time(15, 30):
            return True, "NSE session open"
        return False, "Market closed — outside NSE hours"

    async def enter_safe_mode(self, reason: str) -> None:
        self._healthy = False
        self._safe_mode_reason = reason
        await persist_system_state(SystemState.SAFE_MODE, reason)
        from services.autonomous.state import set_autonomous_running

        try:
            await set_autonomous_running(False)
        except Exception:
            pass
        audit("icb_safe_mode", reason=reason)

    async def recover_safe_mode(self) -> None:
        self._healthy = True
        self._safe_mode_reason = ""
        if not await get_kill_switch_latched():
            await persist_system_state(SystemState.ACTIVE, "")
        audit("icb_safe_mode_cleared")

    async def pause_system(self, reason: str) -> None:
        await persist_system_state(SystemState.PAUSED, reason)

    async def activate_emergency_lock(self, reason: str) -> None:
        await set_kill_switch_latched(True)
        await persist_system_state(SystemState.EMERGENCY_LOCK, reason)

    async def admin_reset_emergency(self) -> dict:
        if not await get_kill_switch_latched():
            return {"ok": False, "message": "System is not in EMERGENCY_LOCK"}
        await set_kill_switch_latched(False)
        await self.recover_safe_mode()
        return {"ok": True, "system_state": SystemState.ACTIVE.value}

    async def fail_safe(self, reason: str) -> None:
        await self.enter_safe_mode(reason)
        try:
            from services.compliance.events import EventType
            from services.compliance.recorder import crce

            await crce.record(
                event_type=EventType.ICB_FAILURE,
                action="FAIL_SAFE",
                decision="DENY",
                reason=reason,
                system_state="SAFE_MODE",
            )
        except Exception:
            pass

    async def status(self, context: dict | None = None) -> dict:
        state = await self._resolve_state(context or {})
        return {
            "system_state": state.value,
            "reason": await get_state_reason() or self._safe_mode_reason,
            "healthy": self._healthy,
            "kill_switch_latched": await get_kill_switch_latched(),
            "trading_allowed": state == SystemState.ACTIVE,
            "analysis_allowed": state in (SystemState.ACTIVE, SystemState.PAUSED),
        }


icb = InstitutionalControlBrain()
