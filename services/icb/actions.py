"""ICB action taxonomy."""

from __future__ import annotations

from enum import Enum


class ICBAction(str, Enum):
    ANALYZE_SYMBOL = "analyze_symbol"
    PLACE_ORDER = "place_order"
    PLACE_EXIT = "place_exit"
    AUTONOMOUS_TICK = "autonomous_tick"
    START_AUTONOMOUS = "start_autonomous"
    STOP_AUTONOMOUS = "stop_autonomous"
    STRATEGY_SCAN = "strategy_scan"
    KILL_SWITCH = "kill_switch"
    RESUME = "resume"
    RECONCILIATION = "reconciliation"
    LIFECYCLE_TICK = "lifecycle_tick"
    ADMIN_RESET = "admin_reset"
    RUN_CHAOS = "run_chaos"
