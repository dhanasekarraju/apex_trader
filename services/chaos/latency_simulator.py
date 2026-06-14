"""Controlled latency injection for chaos testing."""

from __future__ import annotations

import asyncio
import random
from enum import Enum

from services.chaos.scenarios import LatencyProfile


PROFILE_RANGES_MS: dict[LatencyProfile, tuple[int, int]] = {
    LatencyProfile.NORMAL: (10, 50),
    LatencyProfile.STRESSED: (100, 500),
    LatencyProfile.DEGRADED: (1000, 3000),
    LatencyProfile.CRITICAL: (5000, 15000),
}


class LatencySimulator:
    def __init__(self, profile: LatencyProfile = LatencyProfile.NORMAL, seed: int = 42) -> None:
        self.profile = profile
        self._rng = random.Random(seed)

    def sample_ms(self) -> float:
        lo, hi = PROFILE_RANGES_MS[self.profile]
        return float(self._rng.randint(lo, hi))

    async def apply(self, override_ms: float | None = None) -> float:
        delay_ms = override_ms if override_ms is not None else self.sample_ms()
        await asyncio.sleep(delay_ms / 1000.0)
        return delay_ms

    async def apply_to_call(self, coro, *, override_ms: float | None = None):
        await self.apply(override_ms)
        return await coro
