"""Tests for top-5 critical production fixes."""

from services.execution.live_gate import LiveSafetyGate
from services.market_data.service import MarketDataService, RealDataRequired
from services.portfolio.manager import PortfolioManager
from services.risk.advanced import AdvancedRiskEngine, AdvancedRiskState
from services.risk.engine import TradeProposal


def test_emergency_halt_persisted_on_portfolio_state():
    pm = PortfolioManager()
    assert not pm.state.emergency_halt
    pm.emergency_shutdown()
    assert pm.state.emergency_halt
    assert pm.state.circuit_breaker
    rs = pm.state.to_risk_state()
    assert rs.emergency_halt
    assert rs.circuit_breaker


def test_black_swan_sets_all_flags():
    pm = PortfolioManager()
    pm.enter_black_swan()
    assert pm.state.black_swan_mode
    assert pm.state.emergency_halt
    assert pm.state.circuit_breaker


def test_resume_trading_clears_halt_flags():
    pm = PortfolioManager()
    pm.enter_black_swan()
    assert pm.is_trading_halted()
    pm.resume_trading()
    assert not pm.state.emergency_halt
    assert not pm.state.circuit_breaker
    assert not pm.state.black_swan_mode
    assert not pm.is_trading_halted()


def test_risk_engine_respects_persisted_halt():
    engine = AdvancedRiskEngine()
    state = AdvancedRiskState(
        equity=1_000_000, cash=1_000_000, peak_equity=1_000_000,
        emergency_halt=True,
    )
    p = TradeProposal(
        "TEST", "equity", "long", 100, 99, 104, 500, 85,
        "momentum", "trend_up", liquidity_score=0.9, volatility_pct=1.5,
    )
    d = engine.evaluate_advanced(p, state)
    assert not d.approved


def test_live_shadow_require_real_data():
    import asyncio
    import pytest

    data = MarketDataService()

    async def _fetch():
        await data.get_trading_ohlcv("RELIANCE", mode="live")

    with pytest.raises(RealDataRequired):
        asyncio.run(_fetch())


def test_live_gate_blocks_without_real_data():
    import asyncio
    from services.brokers.paper import PaperBroker

    async def _run():
        pm = PortfolioManager()
        pm.persistence_ok = True
        data = MarketDataService()
        ok, blockers = await LiveSafetyGate.check(
            market_data=data,
            broker=PaperBroker(),
            portfolio=pm,
        )
        assert not ok
        assert any("market data" in b.lower() or "Real" in b for b in blockers)

    asyncio.run(_run())


def test_live_gate_blocks_without_static_ip_confirmation():
    import asyncio
    from services.brokers.kite import KiteBroker
    from shared.config import Settings

    async def _run():
        pm = PortfolioManager()
        pm.persistence_ok = True
        data = MarketDataService()
        broker = KiteBroker()
        cfg = Settings(
            default_broker="kite",
            enable_live_execution=True,
            kite_static_ip_confirmed=False,
            market_data_source="kite",
            kite_api_key="test",
            kite_access_token="test",
        )
        ok, blockers = await LiveSafetyGate.check(
            market_data=data,
            broker=broker,
            portfolio=pm,
            settings=cfg,
        )
        assert not ok
        assert any("static IP" in b for b in blockers)

    asyncio.run(_run())
