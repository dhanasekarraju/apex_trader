"""Global emergency halt flag — Redis + Postgres."""

from __future__ import annotations

import json

from shared.events import cache_get, cache_set, publish
from shared.logging import audit

HALT_KEY = "apex:EMERGENCY_HALT"
PNL_CACHE_KEY = "apex:pnl:live"
RISK_CACHE_KEY = "apex:risk:status"

_memory_halt: bool = False


async def set_emergency_halt(active: bool) -> None:
    global _memory_halt
    _memory_halt = active
    try:
        await cache_set(HALT_KEY, "true" if active else "false", ttl=86400)
        await publish(
            "apex:system",
            {"event": "halt", "active": active},
        )
    except Exception:
        pass
    audit("redis_emergency_halt", active=active)


async def is_emergency_halt() -> bool:
    try:
        val = await cache_get(HALT_KEY)
        if val is not None:
            return val == "true"
    except Exception:
        pass
    return _memory_halt


async def cache_pnl_snapshot(snapshot: dict) -> None:
    try:
        await cache_set(PNL_CACHE_KEY, json.dumps(snapshot, default=str), ttl=10)
    except Exception:
        pass


async def cache_risk_snapshot(snapshot: dict) -> None:
    try:
        await cache_set(RISK_CACHE_KEY, json.dumps(snapshot, default=str), ttl=10)
    except Exception:
        pass


async def get_cached_pnl() -> dict | None:
    raw = await cache_get(PNL_CACHE_KEY)
    if not raw:
        return None
    return json.loads(raw)


async def get_cached_risk() -> dict | None:
    raw = await cache_get(RISK_CACHE_KEY)
    if not raw:
        return None
    return json.loads(raw)
