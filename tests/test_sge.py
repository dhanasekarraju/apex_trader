"""Strategy Governance Engine (SGE) tests."""

from __future__ import annotations

import pytest
import pytest_asyncio

from services.governance.engine import StrategyGovernanceEngine
from services.governance.policy import evaluate_strategy
from services.governance.models import StrategyRecord
from services.governance.states import StrategyState
from services.governance.store import clear_governance_state


@pytest_asyncio.fixture
async def sge():
    await clear_governance_state()
    engine = StrategyGovernanceEngine()
    await engine.ensure_loaded()
    yield engine
    await clear_governance_state()


@pytest.mark.asyncio
async def test_default_strategies_active(sge):
    status = sge.status()
    assert len(status["strategies"]) == 5
    assert status["active"] == [
        "breakout",
        "mean_reversion",
        "momentum",
        "trend_following",
        "volatility_expansion",
    ]


@pytest.mark.asyncio
async def test_paused_strategy_blocked(sge):
    await sge.set_state("momentum", StrategyState.PAUSED, reason="manual pause")
    decision = await sge.check_trade(
        "momentum",
        regime="trend_up",
        regime_recommended=["momentum", "trend_following"],
    )
    assert not decision.allowed
    assert decision.state == StrategyState.PAUSED


@pytest.mark.asyncio
async def test_throttled_strategy_allowed_with_multiplier(sge):
    await sge.set_state("momentum", StrategyState.THROTTLED, reason="high vol", throttle_factor=0.4)
    decision = await sge.check_trade(
        "momentum",
        regime="trend_up",
        regime_recommended=["momentum"],
    )
    assert decision.allowed
    assert decision.size_multiplier == 0.4
    assert sge.size_multiplier("momentum") == 0.4


@pytest.mark.asyncio
async def test_killed_strategy_irreversible(sge):
    await sge.set_state("breakout", StrategyState.KILLED, reason="catastrophic loss")
    result = await sge.set_state("breakout", StrategyState.ACTIVE, reason="try restore")
    assert not result["ok"]
    assert "KILLED" in result["message"]


@pytest.mark.asyncio
async def test_regime_filter_blocks_mismatch(sge):
    decision = await sge.check_trade(
        "mean_reversion",
        regime="trend_up",
        regime_recommended=["momentum", "trend_following"],
    )
    assert not decision.allowed
    assert "not in recommended" in decision.reason


@pytest.mark.asyncio
async def test_resolve_allowed_merges_lab_and_governance(sge):
    await sge.set_state("momentum", StrategyState.DISABLED, reason="test")
    allowed, decisions = await sge.resolve_allowed(
        symbol="RELIANCE",
        regime="trend_up",
        regime_recommended=["momentum", "trend_following", "breakout"],
        lab_enabled=["momentum", "trend_following", "breakout"],
    )
    assert "momentum" not in allowed
    assert "trend_following" in allowed
    assert not decisions["momentum"].allowed


@pytest.mark.asyncio
async def test_auto_disable_on_poor_performance(sge, monkeypatch):
    monkeypatch.setattr(sge.cfg, "strategy_disable_min_trades", 3)
    monkeypatch.setattr(sge.cfg, "strategy_disable_win_rate", 35.0)

    for _ in range(3):
        await sge.record_outcome("momentum", -100.0)

    record = sge._records["momentum"]
    assert record.state == StrategyState.DISABLED
    assert "Auto-disabled" in record.reason


@pytest.mark.asyncio
async def test_disabled_for_risk_excludes_non_tradeable(sge):
    await sge.set_state("momentum", StrategyState.THROTTLED)
    await sge.set_state("breakout", StrategyState.PAUSED)
    disabled = sge.disabled_for_risk()
    assert "breakout" in disabled
    assert "momentum" not in disabled


def test_evaluate_active_regime_aligned():
    record = StrategyRecord(name="momentum", state=StrategyState.ACTIVE)
    decision = evaluate_strategy(
        record,
        regime="trend_up",
        regime_recommended=["momentum"],
        lab_enabled=True,
    )
    assert decision.allowed
    assert decision.size_multiplier == 1.0
