"""Integration smoke tests."""

import pytest

from services.core.orchestrator import TradingOrchestrator


@pytest.fixture
def orch():
    return TradingOrchestrator()


@pytest.mark.asyncio
async def test_analyze_pipeline(orch):
    result = await orch.analyze_symbol("RELIANCE")
    assert "symbol" in result
    assert result["symbol"] == "RELIANCE"


def test_backtest_validation(orch):
    result = orch.run_backtest("RELIANCE", "momentum")
    assert "sharpe" in result
    assert "passed_validation" in result


@pytest.mark.asyncio
async def test_readiness_report(orch):
    report = await orch.readiness_report()
    assert "categories" in report
    assert len(report["categories"]) == 5
