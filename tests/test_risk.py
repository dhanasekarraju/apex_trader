"""Risk engine tests — capital preservation rules."""

import os
os.environ.setdefault("KITE_API_KEY", "test")
os.environ.setdefault("KITE_API_SECRET", "test")

from services.risk.engine import RiskEngine, RiskState, TradeProposal


def test_rejects_low_confidence():
    engine = RiskEngine()
    state = RiskState(equity=1_000_000, cash=1_000_000, peak_equity=1_000_000)
    p = TradeProposal(
        "TEST", "equity", "long", 100, 98, 105, 100, 60,
        "momentum", "trend_up",
    )
    d = engine.evaluate(p, state)
    assert not d.approved


def test_approves_valid_trade():
    engine = RiskEngine()
    state = RiskState(equity=1_000_000, cash=1_000_000, peak_equity=1_000_000)
    p = TradeProposal(
        "TEST", "equity", "long", 100, 99, 104, 500, 80,
        "momentum", "trend_up", liquidity_score=0.9, volatility_pct=1.5,
    )
    d = engine.evaluate(p, state)
    assert d.approved


def test_halts_after_consecutive_losses():
    engine = RiskEngine()
    state = RiskState(
        equity=980_000, cash=500_000, peak_equity=1_000_000,
        consecutive_losses=5,
    )
    p = TradeProposal(
        "TEST", "equity", "long", 100, 99, 104, 500, 85,
        "momentum", "trend_up", liquidity_score=0.9,
    )
    d = engine.evaluate(p, state)
    assert d.verdict.value == "halted"
