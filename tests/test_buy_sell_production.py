"""Production buy/sell safety tests."""

import pytest

from services.execution.pre_trade import PreTradeValidator
from services.portfolio.manager import PortfolioManager
from services.portfolio.models import PositionView


def test_normalize_equity_qty():
    v = PreTradeValidator()
    assert v.normalize_equity_qty(10.9) == 10
    assert v.normalize_equity_qty(0.4) == 0


def test_duplicate_symbol_blocked():
    v = PreTradeValidator()
    portfolio = PortfolioManager()
    portfolio.state.positions.append(
        PositionView(
            symbol="RELIANCE",
            qty=10,
            entry=2500,
            stop_loss=2400,
            take_profit=2600,
            strategy="momentum",
            unrealized_pnl=0,
            risk_pct=0.5,
        )
    )
    assert v.has_open_position(portfolio, "RELIANCE")


@pytest.mark.asyncio
async def test_validate_buy_rejects_duplicate():
    v = PreTradeValidator()
    portfolio = PortfolioManager()
    portfolio.state.positions.append(
        PositionView(
            symbol="TCS",
            qty=5,
            entry=4000,
            stop_loss=3900,
            take_profit=4200,
            strategy="momentum",
            unrealized_pnl=0,
            risk_pct=0.3,
        )
    )
    from services.market_data.service import MarketDataService

    ok, msg, qty = await v.validate_buy(
        symbol="TCS",
        qty=10,
        entry=4000,
        stop_loss=3900,
        portfolio=portfolio,
        market_data=MarketDataService(),
        trading_mode="paper",
    )
    assert not ok
    assert "Duplicate" in msg
    assert qty == 0


@pytest.mark.asyncio
async def test_record_exit_updates_pnl():
    portfolio = PortfolioManager()
    portfolio.state.positions.append(
        PositionView(
            symbol="INFY",
            qty=10,
            entry=1500,
            stop_loss=1450,
            take_profit=1600,
            strategy="momentum",
            unrealized_pnl=0,
            risk_pct=0.4,
        )
    )
    start_equity = portfolio.state.equity
    await portfolio.record_exit(
        symbol="INFY",
        exit_price=1480,
        exit_reason="stop_loss",
        pnl=-200,
    )
    assert len(portfolio.state.positions) == 0
    assert portfolio.state.daily_pnl == -200
    assert portfolio.state.equity == start_equity - 200
    assert portfolio.state.consecutive_losses == 1
