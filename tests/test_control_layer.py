"""ICB compatibility shim tests (formerly ICL)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from services.control.actions import ControlAction
from services.control.layer import ControlLayer, control_layer
from services.control.system_state import SystemState, get_kill_switch_latched, set_kill_switch_latched
from services.core.orchestrator import TradingOrchestrator
from services.portfolio.manager import PortfolioManager
from services.risk.dashboard import RiskStatus
from shared.config import get_settings


@pytest.fixture
def layer():
    return ControlLayer()


@pytest_asyncio.fixture(autouse=True)
async def reset_state_between_tests():
    from services.control.system_state import clear_system_state
    from services.icb.engine import icb

    await set_kill_switch_latched(False)
    await clear_system_state()
    await icb.recover_safe_mode()
    from services.control.halt import set_emergency_halt

    await set_emergency_halt(False)
    yield


@pytest_asyncio.fixture
async def clear_kill_latch():
    await set_kill_switch_latched(False)
    yield
    await set_kill_switch_latched(False)


@pytest.mark.asyncio
async def test_safe_mode_blocks_all_trading(layer):
    await layer.enter_safe_mode("test")
    decision = await layer.allow(
        ControlAction.PLACE_ORDER,
        {"portfolio": PortfolioManager(), "trading_mode": "paper"},
    )
    assert not decision.allowed
    assert decision.system_state == SystemState.SAFE_MODE
    await layer.recover_safe_mode()


@pytest.mark.asyncio
async def test_emergency_lock_blocks_everything(clear_kill_latch, layer):
    await layer.activate_kill_switch()
    assert await get_kill_switch_latched()

    decision = await layer.allow(
        ControlAction.ANALYZE_SYMBOL,
        {"portfolio": PortfolioManager(), "trading_mode": "paper"},
    )
    assert not decision.allowed
    assert decision.system_state == SystemState.EMERGENCY_LOCK


@pytest.mark.asyncio
async def test_emergency_lock_only_admin_reset(clear_kill_latch, layer):
    await layer.activate_kill_switch()
    reset = await layer.allow(
        ControlAction.ADMIN_RESET_KILL_SWITCH,
        {"portfolio": PortfolioManager(), "trading_mode": "paper"},
    )
    assert reset.allowed


@pytest.mark.asyncio
async def test_reconciliation_degraded_blocks_trading(layer):
    with patch(
        "services.icb.signals.is_reconciliation_degraded",
        new=AsyncMock(return_value=True),
    ):
        decision = await layer.allow(
            ControlAction.PLACE_ORDER,
            {"portfolio": PortfolioManager(), "trading_mode": "paper"},
        )
    assert not decision.allowed


@pytest.mark.asyncio
async def test_active_allows_analyze(layer, monkeypatch):
    monkeypatch.setenv("ENFORCE_MARKET_HOURS", "false")
    get_settings.cache_clear()

    decision = await layer.allow(
        ControlAction.ANALYZE_SYMBOL,
        {
            "portfolio": PortfolioManager(),
            "trading_mode": "paper",
            "risk_status": RiskStatus.SAFE.value,
        },
    )
    assert decision.allowed
    assert decision.system_state == SystemState.ACTIVE


@pytest.mark.asyncio
async def test_autonomous_blocked_in_safe_mode(layer):
    await layer.enter_safe_mode("infra")
    decision = await layer.allow(
        ControlAction.START_AUTONOMOUS,
        {"portfolio": PortfolioManager(), "trading_mode": "paper"},
    )
    assert not decision.allowed
    await layer.recover_safe_mode()


@pytest.mark.asyncio
async def test_orchestrator_analyze_blocked_when_emergency_lock(clear_kill_latch):
    orch = TradingOrchestrator()
    await control_layer.activate_kill_switch()
    orch.portfolio.emergency_shutdown()

    decision = await orch.analyze_symbol("RELIANCE")
    assert decision["action"] == "NO_TRADE"


@pytest.mark.asyncio
async def test_resume_clears_emergency_lock_after_kill_switch(clear_kill_latch):
    orch = TradingOrchestrator()
    await control_layer.activate_kill_switch()
    orch.portfolio.emergency_shutdown()

    result = await orch.resume_trading()
    assert result["ok"] is True
    assert not await get_kill_switch_latched()


@pytest.mark.asyncio
async def test_admin_reset_clears_emergency_lock(clear_kill_latch):
    orch = TradingOrchestrator()
    orch.portfolio.emergency_shutdown()
    await control_layer.activate_kill_switch()

    result = await orch.admin_reset_kill_switch()
    assert result["ok"] is True
    assert not await get_kill_switch_latched()
