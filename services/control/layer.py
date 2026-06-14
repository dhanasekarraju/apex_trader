"""
Deprecated compatibility shim — ICL merged into ICB.

Use `services.icb.engine.icb` directly for new code.
"""

from __future__ import annotations

from typing import Any

from services.control.actions import ControlAction
from services.icb.actions import ICBAction
from services.icb.engine import icb
from services.icb.system_state import SystemState


_ACTION_MAP = {
    ControlAction.ANALYZE_SYMBOL: ICBAction.ANALYZE_SYMBOL,
    ControlAction.PLACE_ORDER: ICBAction.PLACE_ORDER,
    ControlAction.PLACE_EXIT: ICBAction.PLACE_EXIT,
    ControlAction.START_AUTONOMOUS: ICBAction.START_AUTONOMOUS,
    ControlAction.STOP_AUTONOMOUS: ICBAction.STOP_AUTONOMOUS,
    ControlAction.RECONCILE_PORTFOLIO: ICBAction.RECONCILIATION,
    ControlAction.ADMIN_KILL_SWITCH: ICBAction.KILL_SWITCH,
    ControlAction.ADMIN_RESET_KILL_SWITCH: ICBAction.ADMIN_RESET,
    ControlAction.ADMIN_RESUME: ICBAction.RESUME,
    ControlAction.GOVERN_STRATEGY: ICBAction.ANALYZE_SYMBOL,
}


class ControlDecision:
    def __init__(self, allowed: bool, reason: str, system_state: SystemState) -> None:
        self.allowed = allowed
        self.reason = reason
        self.system_state = system_state


class ControlLayer:
    @property
    def healthy(self) -> bool:
        return icb.healthy

    async def allow(self, action: ControlAction, context: dict[str, Any] | None = None) -> ControlDecision:
        icb_action = _ACTION_MAP.get(action, ICBAction.ANALYZE_SYMBOL)
        result = await icb.authorize(icb_action, context or {})
        return ControlDecision(result.allowed, result.reason, result.system_state)

    async def require(self, action: ControlAction, context: dict[str, Any] | None = None) -> ControlDecision:
        decision = await self.allow(action, context)
        if not decision.allowed:
            raise ControlDenied(decision.reason, decision.system_state)
        return decision

    async def enter_safe_mode(self, reason: str) -> None:
        await icb.enter_safe_mode(reason)

    async def recover_safe_mode(self) -> None:
        await icb.recover_safe_mode()

    async def resolve_state(self, context: dict[str, Any] | None = None) -> SystemState:
        status = await icb.status(context)
        return SystemState(status["system_state"])

    async def activate_kill_switch(self) -> None:
        await icb.activate_emergency_lock("Kill switch activated")

    async def admin_reset_kill_switch(self) -> None:
        await icb.admin_reset_emergency()

    async def sync_trading_mode_state(self, trading_mode: str) -> None:
        if await icb.status().get("kill_switch_latched"):
            return
        await icb.recover_safe_mode()

    async def set_emergency_halt_state(self) -> None:
        await icb.enter_safe_mode("Emergency halt")

    async def status(self, context: dict[str, Any] | None = None) -> dict:
        return await icb.status(context)


class ControlDenied(Exception):
    def __init__(self, reason: str, state: SystemState) -> None:
        self.reason = reason
        self.state = state
        super().__init__(reason)


control_layer = ControlLayer()
