"""Kite capital sync tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.core.orchestrator import TradingOrchestrator
from services.portfolio.manager import PortfolioManager


@pytest.mark.asyncio
async def test_sync_resets_stale_peak_equity():
    pf = PortfolioManager()
    pf.state.equity = 1_000_000
    pf.state.peak_equity = 1_000_000
    pf.state.cash = 1_000_000
    pf.persist = AsyncMock(return_value=True)

    with patch("services.compliance.recorder.crce.record", new=AsyncMock()):
        result = await pf.sync_capital_from_kite(4022.8, 4022.8)

    assert result["ok"] is True
    assert result["peak_reset"] is True
    assert pf.state.equity == 4022.8
    assert pf.state.peak_equity == 4022.8


@pytest.mark.asyncio
async def test_orchestrator_sync_uses_kite_directly():
    orch = TradingOrchestrator()
    orch.cfg.sync_capital_from_kite = True
    orch.cfg.trading_mode = "live"
    orch.cfg.kite_api_key = "key"
    orch.cfg.kite_api_secret = "secret"
    orch.portfolio.sync_capital_from_kite = AsyncMock(
        return_value={"ok": True, "equity": 4022.8},
    )

    broker = MagicMock()
    broker.connect = AsyncMock(return_value=True)
    broker.fetch_account_equity = AsyncMock(
        return_value={"ok": True, "equity": 4022.8, "cash": 4022.8},
    )

    with patch("services.brokers.kite_auth.kite_auth.get_access_token_sync", return_value="tok"):
        with patch("services.brokers.kite.KiteBroker", return_value=broker):
            result = await orch.sync_capital_from_kite(force=True)

    assert result["ok"] is True
    broker.fetch_account_equity.assert_awaited_once()
