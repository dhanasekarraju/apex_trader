"""Reconciliation health — fail-safe broker sync status."""

from __future__ import annotations

import json

from shared.events import cache_get, cache_set
from shared.logging import audit

STATUS_KEY = "apex:reconciliation:status"
_memory_status: dict | None = None


async def set_reconciliation_degraded(reason: str) -> None:
    global _memory_status
    payload = {"status": "DEGRADED", "reason": reason}
    _memory_status = payload
    try:
        await cache_set(STATUS_KEY, json.dumps(payload), ttl=86400)
    except Exception as e:
        audit("reconciliation_cache_fallback", error=str(e))
    audit("reconciliation_degraded", reason=reason)


async def clear_reconciliation_degraded() -> None:
    global _memory_status
    _memory_status = {"status": "OK"}
    try:
        await cache_set(STATUS_KEY, json.dumps({"status": "OK"}), ttl=86400)
    except Exception:
        pass
    audit("reconciliation_ok")


async def get_reconciliation_status() -> dict:
    try:
        raw = await cache_get(STATUS_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    if _memory_status:
        return _memory_status
    return {"status": "OK"}


async def is_reconciliation_degraded() -> bool:
    status = await get_reconciliation_status()
    return status.get("status") == "DEGRADED"
