"""One live writer per PostgreSQL database; fail closed on ownership loss."""
import asyncio
from sqlalchemy import text

LOCK_KEY = 90231077


class ExecutionOwner:
    def __init__(self):
        self.connection = None
        self.active = False
        self._serial = asyncio.Lock()

    async def acquire(self, engine):
        if engine.dialect.name != "postgresql":
            raise RuntimeError("Live execution ownership requires PostgreSQL")
        self.connection = await asyncio.wait_for(engine.connect(), 5)
        try:
            result = await asyncio.wait_for(self.connection.execute(
                text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))"), {"key": LOCK_KEY}), 5)
            acquired = bool(result.scalar())
            await self.connection.commit()
            if not acquired:
                raise RuntimeError("Another trading process owns this database; refusing live startup")
            self.active = True
        except BaseException:
            await self.close()
            raise

    async def check(self):
        async with self._serial:
            if not self.active or self.connection is None:
                raise RuntimeError("Live execution ownership is absent or lost; broker mutation blocked")
            try:
                result = await asyncio.wait_for(self.connection.execute(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                    "AND pid=pg_backend_pid() AND classid=0 AND objid=:key AND granted)"
                ), {"key": LOCK_KEY}), 3)
                held = bool(result.scalar())
                await self.connection.commit()
                if not held:
                    raise RuntimeError("Live ownership lock was lost")
            except BaseException:
                self.active = False
                raise

    async def close(self):
        self.active = False
        if self.connection is not None:
            connection, self.connection = self.connection, None
            # Invalidate the physical session: returning it to a pool would retain
            # its advisory lock. Never silently re-acquire after losing ownership.
            try:
                await connection.invalidate()
            finally:
                await connection.close()


execution_owner = ExecutionOwner()


async def require_execution_owner():
    from shared.config import get_settings
    if get_settings().trading_mode != "live":
        raise RuntimeError("Real broker writes are allowed only in explicit live mode")
    await execution_owner.check()
