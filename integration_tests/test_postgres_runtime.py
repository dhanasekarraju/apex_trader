"""Run only against an explicitly supplied disposable PostgreSQL database."""
import os
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from services.control.execution_owner import ExecutionOwner

URL = os.getenv("APEX_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="Disposable PostgreSQL URL not supplied")


@pytest.mark.asyncio
async def test_postgres_owner_exclusion_release_and_connection_loss():
    engine = create_async_engine(URL, pool_size=4)
    first, second = ExecutionOwner(), ExecutionOwner()
    try:
        await first.acquire(engine)
        await first.check()
        with pytest.raises(RuntimeError, match="Another trading process"):
            await second.acquire(engine)
        await first.close()
        await second.acquire(engine)
        await second.check()
        pid = (await second.connection.execute(text("SELECT pg_backend_pid()"))).scalar()
        await second.connection.commit()
        async with engine.connect() as admin:
            await admin.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            await admin.commit()
        with pytest.raises(Exception):
            await second.check()
        assert not second.active
        with pytest.raises(RuntimeError, match="absent or lost"):
            await second.check()
    finally:
        await first.close()
        await second.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_partial_position_and_pause_survive_reload(monkeypatch):
    from shared.models import Base
    from shared.config import get_settings
    from services.portfolio.manager import PortfolioManager, PositionView
    import services.portfolio.repository as repository
    import services.autonomous.state as state
    monkeypatch.setenv("INITIAL_CAPITAL", "10000")
    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()
    engine = create_async_engine(URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(repository, "SessionLocal", sessions)
    monkeypatch.setattr(state, "SessionLocal", sessions)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        portfolio = PortfolioManager()
        assert await portfolio.load()
        # Repository operations only: no broker, Redis, alerts or audit mutation.
        pos = PositionView("PGTEST", 10, 100, 95, 110, "test", 0, .5)
        portfolio.state.positions.append(pos)
        assert await portfolio.repo.add_position(portfolio.state, pos)
        pos.qty = 6
        pos.risk_pct = .3
        portfolio.state.daily_pnl = 40
        assert await portfolio.repo.close_position(portfolio.state, symbol="PGTEST", exit_price=110, exit_reason="partial", pnl=40)
        restored = PortfolioManager()
        assert await restored.load()
        assert restored.state.positions[0].qty == 6
        assert restored.state.daily_pnl == 40
        await state.set_operator_paused(True)
        assert await state.operator_paused()
    finally:
        await engine.dispose()
        get_settings.cache_clear()
