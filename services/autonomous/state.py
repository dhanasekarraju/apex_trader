"""Autonomous engine runtime state — Redis-backed."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from shared.events import cache_get, cache_set, publish
from shared.logging import audit

ENABLED_KEY = "apex:autonomous:enabled"
STATUS_KEY = "apex:autonomous:status"


async def is_autonomous_running() -> bool:
    try:
        val = await cache_get(ENABLED_KEY)
        if val is not None:
            return val == "true"
    except Exception:
        pass
    return False


async def set_autonomous_running(active: bool) -> None:
    await cache_set(ENABLED_KEY, "true" if active else "false", ttl=86400 * 7)
    await publish("apex:autonomous", {"event": "state", "running": active})
    audit("autonomous_state", running=active)


async def get_autonomous_status() -> dict | None:
    try:
        raw = await cache_get(STATUS_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


async def set_autonomous_status(status: dict) -> None:
    status["updated_at"] = datetime.now(timezone.utc).isoformat()
    await cache_set(STATUS_KEY, json.dumps(status, default=str), ttl=3600)
