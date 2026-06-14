"""Redis event bus — microservice-ready pub/sub."""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from shared.config import get_settings

_client: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def publish(channel: str, payload: dict[str, Any]) -> None:
    r = await get_redis()
    await r.publish(channel, json.dumps(payload))


async def cache_set(key: str, value: str, ttl: int = 300) -> None:
    r = await get_redis()
    await r.setex(key, ttl, value)


async def cache_get(key: str) -> str | None:
    r = await get_redis()
    return await r.get(key)
