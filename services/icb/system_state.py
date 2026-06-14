"""Simplified system state — single state machine for Apex Trader."""

from __future__ import annotations

from enum import Enum

from shared.events import cache_get, cache_set
from shared.logging import audit

STATE_KEY = "apex:system_state"
KILL_LATCH_KEY = "apex:kill_switch_latched"
REASON_KEY = "apex:system_state_reason"

_memory_state: str = "ACTIVE"
_memory_reason: str = ""
_memory_kill_latched: bool = False


class SystemState(str, Enum):
    SAFE_MODE = "SAFE_MODE"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    EMERGENCY_LOCK = "EMERGENCY_LOCK"


TRADING_ACTIONS = frozenset({
    "analyze_symbol",
    "place_order",
    "place_exit",
    "autonomous_tick",
    "start_autonomous",
    "strategy_scan",
})


async def get_system_state() -> SystemState:
    global _memory_state, _memory_kill_latched
    if await get_kill_switch_latched():
        return SystemState.EMERGENCY_LOCK
    try:
        raw = await cache_get(STATE_KEY)
        if raw:
            return SystemState(raw)
    except Exception:
        pass
    try:
        return SystemState(_memory_state)
    except ValueError:
        return SystemState.ACTIVE


async def get_state_reason() -> str:
    global _memory_reason
    try:
        raw = await cache_get(REASON_KEY)
        if raw:
            return raw
    except Exception:
        pass
    return _memory_reason


async def persist_system_state(state: SystemState, reason: str = "") -> None:
    global _memory_state, _memory_reason
    _memory_state = state.value
    _memory_reason = reason
    try:
        await cache_set(STATE_KEY, state.value, ttl=86400 * 30)
        await cache_set(REASON_KEY, reason, ttl=86400 * 30)
    except Exception:
        pass
    audit("system_state_change", state=state.value, reason=reason)


async def get_kill_switch_latched() -> bool:
    global _memory_kill_latched
    try:
        val = await cache_get(KILL_LATCH_KEY)
        if val is not None:
            return val == "true"
    except Exception:
        pass
    return _memory_kill_latched


async def set_kill_switch_latched(active: bool) -> None:
    global _memory_kill_latched
    _memory_kill_latched = active
    try:
        await cache_set(KILL_LATCH_KEY, "true" if active else "false", ttl=86400 * 30)
    except Exception:
        pass
    if active:
        await persist_system_state(SystemState.EMERGENCY_LOCK, "Kill switch latched")


async def clear_system_state() -> None:
    global _memory_state, _memory_reason, _memory_kill_latched
    _memory_state = SystemState.ACTIVE.value
    _memory_reason = ""
    _memory_kill_latched = False
    try:
        await cache_set(STATE_KEY, SystemState.ACTIVE.value, ttl=86400 * 30)
        await cache_set(REASON_KEY, "", ttl=86400 * 30)
        await cache_set(KILL_LATCH_KEY, "false", ttl=86400 * 30)
    except Exception:
        pass
