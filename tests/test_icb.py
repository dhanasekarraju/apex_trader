"""Institutional Control Brain (ICB) tests — simplified authority layer."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from services.icb.actions import ICBAction
from services.icb.decisions import ICBDecision
from services.icb.engine import InstitutionalControlBrain
from services.icb.system_state import SystemState, persist_system_state, set_kill_switch_latched
from services.portfolio.manager import PortfolioManager
from shared.config import get_settings


@pytest_asyncio.fixture
async def brain():
    return InstitutionalControlBrain()


@pytest.mark.asyncio
async def test_icb_allows_analyze_when_active(brain, monkeypatch):
    monkeypatch.setenv("ENFORCE_MARKET_HOURS", "false")
    get_settings.cache_clear()
    result = await brain.authorize(
        ICBAction.ANALYZE_SYMBOL,
        {"portfolio": PortfolioManager(), "trading_mode": "paper", "symbol": "RELIANCE"},
    )
    assert result.allowed
    assert result.decision == ICBDecision.ALLOW


@pytest.mark.asyncio
async def test_safe_mode_blocks_trading(brain):
    await brain.enter_safe_mode("test")
    result = await brain.authorize(
        ICBAction.PLACE_ORDER,
        {"portfolio": PortfolioManager(), "trading_mode": "paper", "symbol": "TCS"},
    )
    assert not result.allowed
    assert result.system_state == SystemState.SAFE_MODE


@pytest.mark.asyncio
async def test_emergency_lock_blocks_trading(brain):
    await set_kill_switch_latched(True)
    result = await brain.authorize(
        ICBAction.PLACE_ORDER,
        {"portfolio": PortfolioManager(), "trading_mode": "paper", "symbol": "INFY"},
    )
    assert not result.allowed
    assert result.decision == ICBDecision.EMERGENCY_LOCK


@pytest.mark.asyncio
async def test_paused_blocks_autonomous(brain):
    await persist_system_state(SystemState.PAUSED, "maintenance")
    result = await brain.authorize(
        ICBAction.AUTONOMOUS_TICK,
        {"portfolio": PortfolioManager(), "trading_mode": "paper"},
    )
    assert not result.allowed
    assert result.decision == ICBDecision.PAUSE_SYSTEM


@pytest.mark.asyncio
async def test_fail_safe_engages_safe_mode(brain):
    await brain.fail_safe("unit test")
    assert not brain.healthy
    result = await brain.authorize(
        ICBAction.PLACE_ORDER,
        {"portfolio": PortfolioManager(), "trading_mode": "paper"},
    )
    assert not result.allowed


@pytest.mark.asyncio
async def test_icb_logs_audit(brain, tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("services.icb.telemetry._AUDIT_PATH", audit_path)
    await brain.authorize(
        ICBAction.STRATEGY_SCAN,
        {"portfolio": PortfolioManager(), "trading_mode": "paper", "symbol": "RELIANCE"},
    )
    assert audit_path.is_file()
    assert "strategy_scan" in audit_path.read_text()


@pytest.mark.asyncio
async def test_risk_danger_blocks_via_icb(brain):
    result = await brain.authorize(
        ICBAction.ANALYZE_SYMBOL,
        {
            "portfolio": PortfolioManager(),
            "trading_mode": "paper",
            "risk_status": "DANGER",
        },
    )
    assert not result.allowed
