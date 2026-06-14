"""Autonomous engine safety and wiring tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services.autonomous.engine import AutonomousEngine
from services.autonomous.watchlist import WatchlistProvider
from services.core.orchestrator import TradingOrchestrator


@pytest.fixture
def orch():
    return TradingOrchestrator()


@pytest.fixture
def engine(orch):
    return orch.autonomous


def test_watchlist_loads_symbols():
    wl = WatchlistProvider()
    symbols = wl.resolve()
    assert len(symbols) >= 4
    assert "RELIANCE" in symbols
    assert len(symbols) == len(set(symbols))


def test_start_blockers_when_disabled(engine, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_ENABLED", "false")
    from shared.config import get_settings

    get_settings.cache_clear()
    engine.cfg = get_settings()
    blockers = engine._start_blockers()
    assert any("AUTONOMOUS_ENABLED" in b for b in blockers)


def test_start_blockers_when_halted(engine):
    engine.orch.portfolio.emergency_shutdown()
    blockers = engine._start_blockers()
    assert "Kill switch active" in blockers


@pytest.mark.asyncio
async def test_start_returns_blockers_when_halted(engine):
    engine.orch.portfolio.emergency_shutdown()
    result = await engine.start()
    assert result["ok"] is False
    assert result["blockers"]


@pytest.mark.asyncio
async def test_tick_skipped_when_not_running(engine):
    with patch("services.autonomous.engine.is_autonomous_running", new=AsyncMock(return_value=False)):
        result = await engine.tick()
    assert result.get("skipped") == "not_running"


@pytest.mark.asyncio
async def test_tick_routes_through_orchestrator(engine, monkeypatch):
    monkeypatch.setenv("ENFORCE_MARKET_HOURS", "false")
    from shared.config import get_settings

    get_settings.cache_clear()
    engine.cfg = get_settings()

    calls: list[str] = []

    async def fake_analyze(symbol: str):
        calls.append(symbol)
        return {"action": "NO_TRADE", "symbol": symbol, "reason": "test"}

    engine.orch.analyze_symbol = fake_analyze  # type: ignore[method-assign]
    engine.watchlist.resolve = lambda: ["RELIANCE", "TCS"]  # type: ignore[method-assign]
    engine._in_session = lambda: True  # type: ignore[method-assign]

    with patch("services.autonomous.engine.is_autonomous_running", new=AsyncMock(return_value=True)):
        with patch("services.autonomous.engine.set_autonomous_status", new=AsyncMock()):
            result = await engine.tick()

    assert calls
    assert result.get("stats", {}).get("scanned", 0) >= 1


@pytest.mark.asyncio
async def test_stop_clears_running(engine):
    with patch("services.autonomous.state.set_autonomous_running", new=AsyncMock()) as mock_set:
        result = await engine.stop()
    assert result["ok"] is True
    assert result["running"] is False
    mock_set.assert_awaited_once_with(False)


@pytest.mark.asyncio
async def test_status_includes_watchlist(engine):
    with patch("services.autonomous.engine.is_autonomous_running", new=AsyncMock(return_value=False)):
        with patch("services.autonomous.engine.get_autonomous_status", new=AsyncMock(return_value=None)):
            status = await engine.status()
    assert status["watchlist_count"] >= 1
    assert "session" in status
