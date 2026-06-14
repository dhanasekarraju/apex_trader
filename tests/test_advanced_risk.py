"""Advanced risk engine tests."""

from services.risk.advanced import AdvancedRiskEngine, AdvancedRiskState
from services.risk.engine import TradeProposal


def test_rejects_disabled_strategy():
    engine = AdvancedRiskEngine()
    state = AdvancedRiskState(
        equity=1_000_000, cash=1_000_000, peak_equity=1_000_000,
        disabled_strategies={"momentum"},
    )
    p = TradeProposal(
        "TEST", "equity", "long", 100, 99, 104, 500, 85,
        "momentum", "trend_up", liquidity_score=0.9, volatility_pct=1.5,
    )
    d = engine.evaluate_advanced(p, state)
    assert not d.approved


def test_safe_mode_halts():
    engine = AdvancedRiskEngine()
    state = AdvancedRiskState(
        equity=1_000_000, cash=1_000_000, peak_equity=1_000_000,
        safe_mode=True,
    )
    p = TradeProposal(
        "TEST", "equity", "long", 100, 99, 104, 500, 85,
        "momentum", "trend_up", liquidity_score=0.9,
    )
    d = engine.evaluate_advanced(p, state)
    assert not d.approved
