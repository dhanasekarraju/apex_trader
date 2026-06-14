"""Production remediation integration tests — auth, execution, reconciliation, flatten."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from services.brokers.base import OrderRequest, OrderStatus, OrderType
from services.brokers.paper import PaperBroker
from services.control.reconciliation_state import (
    clear_reconciliation_degraded,
    is_reconciliation_degraded,
    set_reconciliation_degraded,
)
from services.execution.execution_engine import ExecutionEngine
from services.execution.idempotency import make_order_id
from services.execution.reconciliation import reconcile_on_startup
from services.gateway.auth import resolve_api_access_key, verify_api_token
from services.market_data.service import MarketDataService
from services.portfolio.manager import PortfolioManager
from services.portfolio.models import PositionView
from services.trades.repository import TradeRepository
from shared.config import get_settings


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-api-key-for-ci"}


@pytest.fixture
def api_client(api_headers, monkeypatch):
    monkeypatch.setenv("API_ACCESS_KEY", "test-api-key-for-ci")
    get_settings.cache_clear()

    with patch("services.gateway.main.init_db", new=AsyncMock()), patch(
        "services.gateway.main.orch.startup",
        new=AsyncMock(),
    ), patch(
        "services.gateway.main.orch.shutdown",
        new=AsyncMock(),
    ):
        from services.gateway.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


def test_verify_api_token_rejects_missing():
    with pytest.raises(HTTPException) as exc:
        verify_api_token(None)
    assert exc.value.status_code == 401


def test_verify_api_token_accepts_valid():
    verify_api_token("test-api-key-for-ci")


def test_resolve_api_key_prefers_env():
    cfg = get_settings()
    assert resolve_api_access_key(cfg) == "test-api-key-for-ci"


def test_analyze_requires_auth(api_client):
    r = api_client.post("/api/analyze", json={"symbol": "RELIANCE"})
    assert r.status_code == 401


def test_analyze_rejects_bad_key(api_client):
    r = api_client.post(
        "/api/analyze",
        json={"symbol": "RELIANCE"},
        headers={"X-API-Key": "wrong"},
    )
    assert r.status_code == 401


def test_autonomous_status_requires_auth(api_client):
    r = api_client.get("/api/autonomous/status")
    assert r.status_code == 401


def test_autonomous_routes_with_auth(api_client, api_headers, monkeypatch):
    from services.gateway import main as gateway_main

    async def fake_status():
        return {"running": False, "watchlist_count": 5}

    async def fake_start():
        return {"ok": True, "running": True}

    async def fake_stop():
        return {"ok": True, "running": False}

    gateway_main.orch.autonomous.status = fake_status  # type: ignore[method-assign]
    gateway_main.orch.autonomous.start = fake_start  # type: ignore[method-assign]
    gateway_main.orch.autonomous.stop = fake_stop  # type: ignore[method-assign]

    st = api_client.get("/api/autonomous/status", headers=api_headers)
    assert st.status_code == 200
    body = st.json()
    assert body.get("success") is True

    start = api_client.post("/api/autonomous/start", headers=api_headers)
    assert start.status_code == 200

    stop = api_client.post("/api/autonomous/stop", headers=api_headers)
    assert stop.status_code == 200


@pytest.mark.asyncio
async def test_paper_buy_sl_attach(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("ENFORCE_MARKET_HOURS", "false")
    get_settings.cache_clear()

    portfolio = PortfolioManager()
    engine = ExecutionEngine(portfolio=portfolio, market_data=MarketDataService())
    engine._trades.create_pending = AsyncMock()  # type: ignore[method-assign]
    engine._trades.update_status = AsyncMock()  # type: ignore[method-assign]
    engine._trades.get_by_client_id = AsyncMock(return_value=None)  # type: ignore[method-assign]
    await engine.connect()

    req = OrderRequest(
        symbol="RELIANCE",
        side="long",
        qty=10,
        order_type=OrderType.MARKET,
        stop_price=2400.0,
        take_profit=2600.0,
        strategy="momentum",
        client_order_id=make_order_id("RELIANCE", "momentum"),
    )
    result = await engine.place_order(req, 2500.0)
    assert result.status in (OrderStatus.FILLED, OrderStatus.PARTIAL)
    assert result.raw.get("stop_order_id")


@pytest.mark.asyncio
async def test_paper_exit_after_buy(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()

    portfolio = PortfolioManager()
    engine = ExecutionEngine(portfolio=portfolio, market_data=MarketDataService())
    await engine.connect()

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

    exit_result = await engine.place_exit(
        symbol="RELIANCE",
        qty=10,
        reason="take_profit",
        market_price=2600,
    )
    assert exit_result.status in (OrderStatus.FILLED, OrderStatus.PARTIAL)


@pytest.mark.asyncio
async def test_reconciliation_does_not_wipe_on_broker_failure(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()
    await clear_reconciliation_degraded()

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

    broker = MagicMock()
    broker.fetch_open_positions = AsyncMock(side_effect=RuntimeError("broker down"))

    trades = MagicMock()
    trades.open_trades = AsyncMock(return_value=[])

    report = await reconcile_on_startup(
        broker=broker,
        portfolio=portfolio,
        trades=trades,
        trading_mode="paper",
    )

    assert report["reconciliation_status"] == "DEGRADED"
    assert len(portfolio.state.positions) == 1
    assert await is_reconciliation_degraded()


@pytest.mark.asyncio
async def test_flatten_updates_equity_and_cash(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()

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
    portfolio.state.cash = portfolio.state.equity - 25000

    engine = ExecutionEngine(portfolio=portfolio, market_data=MarketDataService())
    engine._broker = PaperBroker()
    await engine._broker.connect()

    equity_before = portfolio.state.equity
    cash_before = portfolio.state.cash
    await engine.activate_kill_switch()

    assert not portfolio.state.positions
    assert portfolio.state.cash >= cash_before
    assert portfolio.is_trading_halted()


@pytest.mark.asyncio
async def test_circuit_breaker_does_not_emergency_shutdown():
    portfolio = PortfolioManager()
    engine = ExecutionEngine(portfolio=portfolio)
    engine._circuit.failure_threshold = 2
    engine._circuit.pause_minutes = 10

    engine._circuit.record_failure("e1")
    engine._circuit.record_failure("e2")
    assert engine._circuit.is_open()
    assert not portfolio.is_trading_halted()


@pytest.mark.asyncio
async def test_execution_blocks_when_reconciliation_degraded(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    get_settings.cache_clear()
    await set_reconciliation_degraded("broker down")

    portfolio = PortfolioManager()
    engine = ExecutionEngine(portfolio=portfolio)
    req = OrderRequest(
        symbol="RELIANCE",
        side="long",
        qty=1,
        order_type=OrderType.MARKET,
        stop_price=2400,
        strategy="momentum",
        client_order_id=make_order_id("RELIANCE", "momentum"),
    )
    result = await engine.place_order(req, 2500)
    assert result.status == OrderStatus.REJECTED
    assert (
        "Reconciliation degraded" in result.message
        or "ICB blocked" in result.message
        or "DEGRADED" in result.message
        or "SAFE_MODE" in result.message
    )

    await clear_reconciliation_degraded()


@pytest.mark.asyncio
async def test_orchestrator_blocks_on_risk_danger(monkeypatch):
    monkeypatch.setenv("ENFORCE_MARKET_HOURS", "false")
    get_settings.cache_clear()

    from services.core.orchestrator import TradingOrchestrator
    from services.risk.dashboard import RiskStatus

    orch = TradingOrchestrator()
    orch.portfolio.state.equity = 1_000_000
    orch.portfolio.state.daily_pnl = -20_000

    with patch.object(orch.risk_dashboard, "compute") as mock_compute:
        mock_compute.return_value = {"status": RiskStatus.DANGER.value}
        decision = await orch.analyze_symbol("RELIANCE")

    assert decision["action"] == "NO_TRADE"
    assert "DANGER" in decision["reason"] or "ICB" in decision["reason"]


@pytest.mark.asyncio
async def test_pnl_daily_reset(monkeypatch):
    from services.control.pnl_reset import maybe_reset_pnl_periods, _memory_reset

    _memory_reset.clear()
    portfolio = PortfolioManager()
    portfolio.state.daily_pnl = -5000
    portfolio.persist = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await maybe_reset_pnl_periods(portfolio)
    assert "daily" in result["reset"]
    assert portfolio.state.daily_pnl == 0
