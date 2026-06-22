"""Peak equity reset when syncing small real capital after large INITIAL_CAPITAL seed."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

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
