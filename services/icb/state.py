"""ICB state machine — institutional control brain health."""

from __future__ import annotations

import json
from enum import Enum

from shared.events import cache_get, cache_set
from shared.logging import audit

STATE_KEY = "apex:icb:state"
REASON_KEY = "apex:icb:state_reason"

_memory_state: str = "HEALTHY"
_memory_reason: str = ""


class ICBState(str, Enum):
    HEALTHY = "HEALTHY"
    WATCH = "WATCH"
    DEGRADED = "DEGRADED"
    RESTRICTED = "RESTRICTED"
    FROZEN = "FROZEN"
    EMERGENCY_LOCK = "EMERGENCY_LOCK"


AUTONOMOUS_BLOCKED_STATES = frozenset({
    ICBState.DEGRADED,
    ICBState.RESTRICTED,
    ICBState.FROZEN,
    ICBState.EMERGENCY_LOCK,
})


async def get_icb_state() -> ICBState:
    global _memory_state
    try:
        raw = await cache_get(STATE_KEY)
        if raw:
            return ICBState(raw)
    except Exception:
        pass
    try:
        return ICBState(_memory_state)
    except ValueError:
        return ICBState.HEALTHY


async def get_icb_state_reason() -> str:
    global _memory_reason
    try:
        raw = await cache_get(REASON_KEY)
        if raw:
            return raw
    except Exception:
        pass
    return _memory_reason


async def persist_icb_state(state: ICBState, reason: str = "") -> None:
    global _memory_state, _memory_reason
    _memory_state = state.value
    _memory_reason = reason
    try:
        await cache_set(STATE_KEY, state.value, ttl=86400 * 7)
        await cache_set(REASON_KEY, reason, ttl=86400 * 7)
    except Exception:
        pass
    audit("icb_state_change", state=state.value, reason=reason)


async def clear_icb_state() -> None:
    global _memory_state, _memory_reason
    _memory_state = ICBState.HEALTHY.value
    _memory_reason = ""
    await persist_icb_state(ICBState.HEALTHY, "")
