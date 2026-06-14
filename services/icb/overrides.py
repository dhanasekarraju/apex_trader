"""Admin override controls for ICB."""

from __future__ import annotations

from services.icb.engine import icb
from services.icb.system_state import SystemState, persist_system_state
from shared.logging import audit


async def admin_set_state(state: SystemState, reason: str, actor: str = "admin") -> dict:
    if state == SystemState.EMERGENCY_LOCK:
        await icb.activate_emergency_lock(reason or f"Admin lock by {actor}")
    elif state == SystemState.SAFE_MODE:
        await icb.enter_safe_mode(reason or f"Admin safe mode by {actor}")
    elif state == SystemState.PAUSED:
        await icb.pause_system(reason or f"Admin pause by {actor}")
    else:
        await persist_system_state(state, reason)
        await icb.recover_safe_mode()
    audit("icb_admin_override", state=state.value, reason=reason, actor=actor)
    return {"ok": True, "system_state": state.value, "reason": reason}


async def admin_clear_emergency_lock(actor: str = "admin") -> dict:
    return await icb.admin_reset_emergency()
