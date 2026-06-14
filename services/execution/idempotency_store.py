"""Persistent idempotency claims — Redis with in-process fallback for tests."""

from __future__ import annotations

from shared.config import get_settings
from shared.logging import audit

_memory_claims: set[str] = set()


def _ttl_seconds() -> int:
    cfg = get_settings()
    return max(60, int(cfg.idempotency_bucket_minutes * 60 * 2))


async def claim_order_id(client_order_id: str) -> bool:
    """Return True if this process acquired the idempotency claim."""
    key = f"apex:idempotency:{client_order_id}"
    try:
        from shared.events import get_redis

        r = await get_redis()
        acquired = await r.set(key, "1", nx=True, ex=_ttl_seconds())
        return bool(acquired)
    except Exception as e:
        audit("idempotency_redis_fallback", error=str(e))
        if client_order_id in _memory_claims:
            return False
        _memory_claims.add(client_order_id)
        return True


async def release_order_id(client_order_id: str) -> None:
    key = f"apex:idempotency:{client_order_id}"
    try:
        from shared.events import get_redis

        r = await get_redis()
        await r.delete(key)
    except Exception:
        _memory_claims.discard(client_order_id)


async def seed_order_id(client_order_id: str) -> None:
    """Mark an existing open trade as claimed (startup recovery)."""
    key = f"apex:idempotency:{client_order_id}"
    try:
        from shared.events import get_redis

        r = await get_redis()
        await r.set(key, "1", ex=_ttl_seconds())
    except Exception:
        _memory_claims.add(client_order_id)
