import os

import pytest
import pytest_asyncio
import sys
from unittest.mock import AsyncMock

os.environ.setdefault("KITE_API_KEY", "test")
os.environ.setdefault("KITE_API_SECRET", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-for-ci")
os.environ.setdefault("API_ACCESS_KEY", "test-api-key-for-ci")
os.environ.setdefault("CHAOS_GATE_ENFORCE", "false")


@pytest.fixture(autouse=True)
def reset_system_state(isolated_runtime):
    import services.icb.system_state as state_mod
    from services.icb.engine import icb
    from services.icb.system_state import SystemState

    icb._healthy = True
    icb._safe_mode_reason = ""
    state_mod._memory_state = SystemState.ACTIVE.value
    state_mod._memory_reason = ""
    state_mod._memory_kill_latched = False
    yield
    icb._healthy = True
    state_mod._memory_state = SystemState.ACTIVE.value
    state_mod._memory_kill_latched = False


@pytest_asyncio.fixture(autouse=True)
async def isolated_runtime(monkeypatch, tmp_path):
    """Use isolated storage/cache; tests must never use the operator's services."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from shared.models import Base
    from shared.config import get_settings
    import shared.events as events
    import fakeredis.aioredis
    from services.brokers.factory import reset_broker_cache
    monkeypatch.setenv('TRADING_MODE', 'paper')
    monkeypatch.setenv('GOLIVE_APPROVED', 'false')
    monkeypatch.setenv('ENABLE_LIVE_EXECUTION', 'false')
    get_settings.cache_clear()
    reset_broker_cache()
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'test.db'))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    for name, module in list(sys.modules.items()):
        if module and name.startswith(('services.', 'shared.')) and hasattr(module, 'SessionLocal'):
            monkeypatch.setattr(module, 'SessionLocal', sessions)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(events, '_client', redis)
    import services.shadow.engine as shadow
    monkeypatch.setattr(shadow, 'SHADOW_DIR', tmp_path / 'shadow')
    import services.compliance.store as store
    monkeypatch.setattr(store, 'EVENT_LOG', tmp_path / 'events.jsonl')
    monkeypatch.setattr(store, 'HASH_FILE', tmp_path / 'hash.txt')
    import services.golive.evidence as evidence
    monkeypatch.setattr(evidence, 'EVIDENCE', tmp_path / 'validation.json')
    import services.control.reconciliation_state as reconcile
    monkeypatch.setattr(reconcile, '_memory_status', None)
    # Cached health observations must not leak between isolated test runtimes.
    import services.icb.signals as signals
    monkeypatch.setattr(signals, '_signals_cache', None)
    import services.execution.idempotency_store as ids
    ids._memory_claims.clear()
    yield
    await redis.aclose()
    await engine.dispose()
    get_settings.cache_clear()
