"""Kite capital sync tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.core.orchestrator import TradingOrchestrator
from services.portfolio.manager import PortfolioManager


@pytest.mark.asyncio
async def test_sync_capital_from_kite_updates_portfolio():
    orch = TradingOrchestrator()
    orch.cfg.sync_capital_from_kite = True
    orch.cfg.trading_mode = "live"
    orch.portfolio.state.equity = 100_000
    orch.portfolio.state.cash = 100_000
    orch.portfolio.persist = AsyncMock(return_value=True)

    broker = MagicMock()
    broker.name = "kite"
    broker.connect = AsyncMock(return_value=True)
    broker.fetch_account_equity = AsyncMock(
        return_value={"ok": True, "equity": 250_000.0, "cash": 180_000.0},
    )

    with patch("services.brokers.factory.get_broker", return_value=broker):
        with patch("services.compliance.recorder.crce.record", new=AsyncMock()):
            result = await orch.sync_capital_from_kite(force=True)

    assert result["ok"] is True
    assert orch.portfolio.state.equity == 250_000.0
    assert orch.portfolio.state.cash == 180_000.0


@pytest.mark.asyncio
async def test_sync_skipped_when_disabled():
    orch = TradingOrchestrator()
    orch.cfg.sync_capital_from_kite = False
    result = await orch.sync_capital_from_kite(force=True)
    assert result["skipped"] == "disabled"
