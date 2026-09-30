"""Autonomous engine runtime state — Redis-backed."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from shared.events import cache_get, cache_set, publish
from shared.logging import audit
from services.gateway.desk_snapshot import observe
from shared.database import SessionLocal

ENABLED_KEY = "apex:autonomous:enabled"
STATUS_KEY = "apex:autonomous:status"


async def is_autonomous_running() -> bool:
    try:
        val = await cache_get(ENABLED_KEY)
        observe("running", {"running": val == "true"})
        if val is not None:
            return val == "true"
    except Exception:
        pass
    return False


async def set_autonomous_running(active: bool) -> None:
    await cache_set(ENABLED_KEY, "true" if active else "false", ttl=86400 * 7)
    observe("running", {"running": active})
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
    observe("autonomous", status)
    await cache_set(STATUS_KEY, json.dumps(status, default=str), ttl=3600)


async def operator_paused() -> bool:
    from shared.models import OperatorControl
    try:
        async with SessionLocal() as session:
            row = await session.get(OperatorControl, "entries_paused")
            return bool(row and row.enabled)
    except Exception:
        return True  # Unknown operator intent must not auto-enable entries.


async def set_operator_paused(paused: bool) -> None:
    from shared.models import OperatorControl
    async with SessionLocal() as session:
        row = await session.get(OperatorControl, "entries_paused")
        if row is None:
            row = OperatorControl(key="entries_paused", enabled=paused)
            session.add(row)
        else:
            row.enabled = paused
        await session.commit()
