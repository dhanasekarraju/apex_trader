"""Go-live gate tests."""

from services.golive.gate import GoLiveGate


def test_blocks_live_without_validation():
    gate = GoLiveGate()
    report = gate.evaluate(
        backtest={
            "sharpe": 0.5, "win_rate": 30, "profit_factor": 0.8,
            "max_drawdown": 15, "passed_validation": False,
        },
        shadow={"simulated_fills": 0, "win_rate": 0},
        risk_healthy=True,
        data_quality=0.9,
        watchdog_ok=True,
        strategy_scores={"momentum": 40},
    )
    assert not report.overall_passed
    assert not report.live_allowed
    assert len(report.blockers) > 0


def test_passes_with_strong_metrics():
    gate = GoLiveGate()
    report = gate.evaluate(
        backtest={
            "sharpe": 1.5, "win_rate": 55, "profit_factor": 1.8,
            "max_drawdown": 5, "passed_validation": True,
        },
        shadow={"simulated_fills": 50, "win_rate": 52},
        risk_healthy=True,
        data_quality=0.95,
        watchdog_ok=True,
        strategy_scores={"momentum": 55},
    )
    assert report.overall_passed
    assert all(c.passed for c in report.categories)
