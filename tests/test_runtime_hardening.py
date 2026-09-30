"""Production safeguards must fail closed without touching a broker."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import pytest
from services.control.execution_owner import ExecutionOwner
from services.execution.protection import stop_problem
from shared.config import get_settings


def connection(held=True):
    return NS(execute=AsyncMock(return_value=NS(scalar=lambda: held)), commit=AsyncMock(),
              invalidate=AsyncMock(), close=AsyncMock())


@pytest.mark.asyncio
async def test_owner_rejects_second_process_and_closes_physical_session():
    conn = connection(False)
    owner = ExecutionOwner()
    with pytest.raises(RuntimeError, match="Another trading process"):
        await owner.acquire(NS(dialect=NS(name="postgresql"), connect=AsyncMock(return_value=conn)))
    assert not owner.active
    conn.invalidate.assert_awaited_once()
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_owner_loss_is_latched_and_does_not_reacquire():
    conn = connection()
    owner = ExecutionOwner()
    await owner.acquire(NS(dialect=NS(name="postgresql"), connect=AsyncMock(return_value=conn)))
    await owner.check()
    conn.execute.side_effect = ConnectionError("Disconnected")
    with pytest.raises(ConnectionError):
        await owner.check()
    assert not owner.active
    conn.execute.side_effect = None
    with pytest.raises(RuntimeError, match="absent or lost"):
        await owner.check()
    await owner.close()


@pytest.mark.asyncio
async def test_broker_writes_blocked_without_owner(monkeypatch):
    import services.control.execution_owner as ownership
    from services.brokers.kite import KiteBroker
    get_settings().trading_mode = "live"
    get_settings().default_broker = "kite"
    monkeypatch.setattr(ownership, "execution_owner", ExecutionOwner())
    broker = KiteBroker()
    broker._kite = NS(place_order=Mock(), cancel_order=Mock())
    with pytest.raises(RuntimeError):
        await broker._submit_order({"quantity": 1})
    with pytest.raises(RuntimeError):
        await broker.cancel_order("STOP")
    broker._kite.place_order.assert_not_called()
    broker._kite.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_operator_stop_survives_auto_start_and_cache_loss(monkeypatch):
    from services.core.orchestrator import TradingOrchestrator
    from services.autonomous.state import operator_paused, set_operator_paused
    import shared.events as events
    orch = TradingOrchestrator()
    orch.cfg.autonomous_auto_start = True
    orch.cfg.autonomous_enabled = True
    orch.autonomous._start_blockers = AsyncMock(return_value=[])
    await orch.autonomous.stop()
    await (await events.get_redis()).flushdb()
    assert await operator_paused()
    result = await orch.autonomous.start(automatic=True)
    assert result["ok"] is False
    orch.autonomous._start_blockers.assert_not_awaited()
    # Shutdown/EOD stopping cannot erase an operator pause.
    await orch.autonomous.stop(operator_pause=False)
    assert await operator_paused()
    await set_operator_paused(False)
    assert not await operator_paused()


@pytest.mark.asyncio
async def test_startup_db_failure_never_launches_trading_loops(monkeypatch):
    import services.gateway.main as gateway
    monkeypatch.setattr(gateway, "init_db", AsyncMock(side_effect=RuntimeError("Database unavailable")))
    loops = Mock()
    monkeypatch.setattr(gateway, "ensure_background_loops", loops)
    monkeypatch.setattr(gateway.orch, "startup", AsyncMock())
    with pytest.raises(RuntimeError, match="Database unavailable"):
        async with gateway.lifespan(gateway.app):
            pytest.fail("Broken startup yielded a running service")
    loops.assert_not_called()
    gateway.orch.startup.assert_not_awaited()
    assert not gateway.app.state.initialized


@pytest.mark.asyncio
async def test_portfolio_load_failure_stops_recovery(monkeypatch):
    from services.core.orchestrator import TradingOrchestrator
    orch = TradingOrchestrator()
    monkeypatch.setattr(orch.portfolio, "load", AsyncMock(return_value=False))
    monkeypatch.setattr(orch.execution, "recover", AsyncMock())
    with pytest.raises(RuntimeError, match="Portfolio recovery failed"):
        await orch.startup()
    orch.execution.recover.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_chaos_never_starts(monkeypatch):
    from services.chaos.auto import ensure_fresh_report
    from services.chaos.chaos_engine import ChaosEngine
    get_settings().trading_mode = "live"
    get_settings().default_broker = "kite"
    assert not await ensure_fresh_report()
    engine = ChaosEngine()
    engine.runner.run = AsyncMock()
    with pytest.raises(RuntimeError, match="separate paper"):
        await engine.run_suite()
    engine.runner.run.assert_not_awaited()


def working_stop():
    return dict(status="TRIGGER PENDING", pending_quantity=10, filled_quantity=0,
                trigger_price=95, tradingsymbol="TEST", transaction_type="SELL",
                exchange="NSE", product="MIS", order_type="SL-M")


@pytest.mark.parametrize("change", [dict(status="COMPLETE"), dict(pending_quantity=4),
    dict(filled_quantity=2), dict(transaction_type="BUY"), dict(product="CNC"),
    dict(tradingsymbol="OTHER"), dict(order_type="MARKET"), dict(trigger_price=0)])
def test_wrong_or_partial_stop_never_counts_as_protection(change):
    stop = {**working_stop(), **change}
    assert stop_problem(stop, symbol="TEST", qty=10, product="MIS", exchange="NSE")


def test_matching_working_stop():
    assert not stop_problem(working_stop(), symbol="TEST", qty=10, product="MIS", exchange="NSE")


@pytest.mark.asyncio
async def test_terminal_partial_stop_never_rebooks_cumulative_quantity():
    from services.portfolio.manager import PortfolioManager, PositionView
    from services.execution.lifecycle import PositionLifecycleService
    from services.execution.execution_engine import ExecutionEngine
    portfolio = PortfolioManager()
    position = PositionView("TEST", 10, 100, 95, 110, "test", 0, .5, stop_order_id="STOP")
    portfolio.state.positions = [position]
    lifecycle = PositionLifecycleService(portfolio=portfolio, execution=ExecutionEngine(portfolio=portfolio), market_data=Mock())
    lifecycle._close_position = AsyncMock()
    broker = NS(fetch_order_status=AsyncMock(return_value={**working_stop(), "status":"COMPLETE", "filled_quantity":4, "average_price":95}))
    await lifecycle._check_position(position, broker, "live")
    await lifecycle._check_position(position, broker, "live")
    lifecycle._close_position.assert_not_awaited()
    assert position.qty == 10


@pytest.mark.asyncio
async def test_cross_origin_control_is_rejected_before_mutation(monkeypatch):
    from services.gateway.main import app, orch
    from httpx import ASGITransport, AsyncClient
    stop = AsyncMock()
    monkeypatch.setattr(orch.autonomous, "stop", stop)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.post("/api/autonomous/stop", headers={"X-API-Key":get_settings().api_access_key, "Origin":"https://evil.example"})
    assert response.status_code == 403
    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_oauth_callback_requires_matching_browser_state(monkeypatch):
    from urllib.parse import urlsplit, parse_qs
    from services.gateway.main import app, orch
    from services.brokers.kite_auth import kite_auth
    from httpx import ASGITransport, AsyncClient
    monkeypatch.setattr(kite_auth, "login_url", lambda: "https://kite.zerodha.com/connect/login?v=3")
    complete = AsyncMock()
    monkeypatch.setattr(kite_auth, "complete_login", complete)
    monkeypatch.setattr(orch.execution, "connect", AsyncMock(return_value=True))
    monkeypatch.setattr(orch.execution, "refresh_broker", Mock())
    monkeypatch.setattr(orch, "sync_capital_from_kite", AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test", headers={"X-API-Key":get_settings().api_access_key}) as client:
        response = await client.get("/api/kite/callback?request_token=bogus")
        assert response.status_code == 403
        complete.assert_not_awaited()
        login = await client.get("/api/kite/login")
        state = parse_qs(parse_qs(urlsplit(login.headers["location"]).query)["redirect_params"][0])["apex_state"][0]
        assert "HttpOnly" in login.headers["set-cookie"] and "Secure" in login.headers["set-cookie"]
        response = await client.get("/api/kite/callback", params={"request_token":"test-request-token", "apex_state":state, "status":"success"})
        assert response.status_code in (302,307)
        complete.assert_awaited_once_with("test-request-token")
        assert "Max-Age=0" in response.headers["set-cookie"]
