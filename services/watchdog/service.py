"""Operational watchdog — safe mode on infrastructure failure."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shared.config import get_settings
from shared.logging import audit


@dataclass
class HealthStatus:
    ok: bool
    postgres: bool
    redis: bool
    broker: bool
    api: bool
    clock_ok: bool
    safe_mode: bool
    issues: list[str] = field(default_factory=list)
    checked_at: str = ""


class WatchdogService:
    def __init__(self) -> None:
        self.cfg = get_settings()
        self.safe_mode = False
        self._broker = None

    async def check_all(self, broker_connected: bool = True) -> HealthStatus:
        issues: list[str] = []
        pg_ok = await self._check_postgres()
        redis_ok = await self._check_redis()
        if not pg_ok:
            issues.append("PostgreSQL unreachable")
        if not redis_ok:
            issues.append("Redis unreachable")
        if not broker_connected:
            issues.append("Broker disconnected")

        strict = self.cfg.env == "production" or self.cfg.trading_mode == "live"
        if issues and strict:
            self.safe_mode = True
            audit("watchdog_safe_mode", issues=issues)
        else:
            self.safe_mode = False

        return HealthStatus(
            ok=len(issues) == 0,
            postgres=pg_ok,
            redis=redis_ok,
            broker=broker_connected,
            api=True,
            clock_ok=True,
            safe_mode=self.safe_mode,
            issues=issues,
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

    async def _check_postgres(self) -> bool:
        try:
            from shared.database import engine
            async with engine.connect() as conn:
                await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
            return True
        except Exception:
            return False

    async def _check_redis(self) -> bool:
        try:
            from shared.events import get_redis
            r = await get_redis()
            await r.ping()
            return True
        except Exception:
            return False

    async def run_loop(self, interval: int = 30) -> None:
        while True:
            await self.check_all()
            await asyncio.sleep(interval)
