"""Position sizing tests."""

from services.sizing.engine import PositionSizingEngine, SizeInput, SizingMethod


def test_fixed_risk_never_exceeds_limit():
    engine = PositionSizingEngine()
    inp = SizeInput(
        equity=1_000_000, entry=100, stop_loss=98,
        volatility_pct=2.0, portfolio_heat_pct=0, risk_multiplier=1.0,
    )
    result = engine.compute(inp, SizingMethod.FIXED_RISK)
    risk_rs = abs(inp.entry - inp.stop_loss) * result.qty
    assert risk_rs / inp.equity * 100 <= 0.51


def test_small_capital_allows_one_midcap_share():
    engine = PositionSizingEngine()
    inp = SizeInput(
        equity=5_000,
        entry=1_200,
        stop_loss=1_180,
        volatility_pct=2.0,
        portfolio_heat_pct=0,
        risk_multiplier=1.0,
    )
    result = engine.compute(inp, SizingMethod.FIXED_RISK)
    assert result.qty >= 1


def test_kelly_capped():
    engine = PositionSizingEngine()
    inp = SizeInput(
        equity=1_000_000, entry=100, stop_loss=95,
        volatility_pct=2.0, portfolio_heat_pct=0, risk_multiplier=1.0,
        win_rate=70, avg_win_loss_ratio=2.0,
    )
    result = engine.compute(inp, SizingMethod.KELLY)
    assert result.qty > 0
    assert result.risk_pct <= 1.0
