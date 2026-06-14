"""Global system state — re-export from ICB."""

from services.icb.system_state import (
    SystemState,
    clear_system_state,
    get_kill_switch_latched,
    get_state_reason,
    get_system_state,
    persist_system_state,
    set_kill_switch_latched,
)

# Backward-compatible aliases
clear_persisted_system_state = clear_system_state
get_persisted_system_state = get_system_state

__all__ = [
    "SystemState",
    "clear_persisted_system_state",
    "clear_system_state",
    "get_kill_switch_latched",
    "get_persisted_system_state",
    "get_state_reason",
    "get_system_state",
    "persist_system_state",
    "set_kill_switch_latched",
]
