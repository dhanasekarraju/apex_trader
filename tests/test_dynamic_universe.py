"""Dynamic trending watchlist tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.autonomous.dynamic_universe import DynamicUniverseSelector, trending_score


def test_trending_score_weights_volume_and_move():
    low = trending_score({"volume": 100_000, "last_price": 100, "ohlc": {"close": 100}})
    high = trending_score({"volume": 1_000_000, "last_price": 105, "ohlc": {"close": 100}})
    assert high > low


@pytest.mark.asyncio
async def test_dynamic_universe_fallback_without_kite():
    data = MagicMock()
    data.has_real_data_configured.return_value = False
    selector = DynamicUniverseSelector(market_data=data)
    with patch("services.autonomous.dynamic_universe.cache_set", new=AsyncMock()):
        snap = await selector.refresh()
    assert snap.pool_size == 50
    assert snap.scan_size == 15
    assert len(snap.scan) == 15
    assert snap.source == "fallback_liquid"


@pytest.mark.asyncio
async def test_dynamic_universe_ranks_kite_quotes():
    data = MagicMock()
    data.has_real_data_configured.return_value = True
    data.list_nse_eq_symbols = AsyncMock(return_value=["AAA", "BBB", "CCC"])
    data.fetch_market_quotes = AsyncMock(
        return_value={
        "AAA": {"volume": 10_000, "last_price": 60, "ohlc": {"close": 60}},
        "BBB": {"volume": 5_000_000, "last_price": 110, "ohlc": {"close": 100}},
        "CCC": {"volume": 200_000, "last_price": 80, "ohlc": {"close": 78}},
    })
    selector = DynamicUniverseSelector(market_data=data)
    with patch("services.autonomous.dynamic_universe.cache_set", new=AsyncMock()):
        snap = await selector.refresh()
    assert snap.scan[0] == "BBB"
    assert snap.source == "kite_trending"


@pytest.mark.asyncio
async def test_watchlist_dynamic_mode(monkeypatch):
    from services.autonomous.watchlist import WatchlistProvider
    from shared.config import get_settings

    monkeypatch.setenv("WATCHLIST_MODE", "dynamic")
    get_settings.cache_clear()
    provider = WatchlistProvider()
    fake_snap = type("S", (), {"scan": ["RELIANCE"], "pool": ["RELIANCE", "TCS"]})()
    fake_snap.to_dict = lambda: {"mode": "dynamic", "scan": fake_snap.scan, "pool": fake_snap.pool}
    with patch(
        "services.autonomous.dynamic_universe.DynamicUniverseSelector.get_snapshot",
        new=AsyncMock(return_value=fake_snap),
    ):
        symbols = await provider.resolve_scan_symbols()
    assert symbols == ["RELIANCE"]
