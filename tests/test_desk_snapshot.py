"""Dashboard reads must not add trading, broker, database or Redis work."""
from unittest.mock import AsyncMock, Mock
import pytest
from httpx import ASGITransport, AsyncClient
from services.gateway import desk_snapshot as desk
from services.gateway.main import app, orch
from shared.config import get_settings


@pytest.mark.asyncio
async def test_snapshot_is_authenticated_passive_and_does_not_mutate_ledger(monkeypatch):
    from services.brokers.kite_auth import kite_auth
    import shared.events as events
    monkeypatch.setattr(desk, "_observations", {})
    bomb = Mock(side_effect=AssertionError("Dashboard initiated expensive work"))
    async_bomb = AsyncMock(side_effect=AssertionError("Dashboard initiated I/O"))
    for name in ("dashboard", "live_pnl", "refresh_control_cache", "run_backtest"):
        monkeypatch.setattr(orch, name, bomb)
    monkeypatch.setattr(orch.autonomous, "status", async_bomb)
    monkeypatch.setattr(orch.execution, "live_blockers", async_bomb)
    monkeypatch.setattr(kite_auth, "verify_connection", async_bomb)
    monkeypatch.setattr(events, "cache_get", async_bomb)
    monkeypatch.setattr(events, "cache_set", async_bomb)
    monkeypatch.setattr(kite_auth, "get_access_token_sync", lambda: "never-expose-this-secret")
    original_curve = list(orch.equity_curve)
    original_metrics = orch.portfolio.metrics()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get("/api/desk/snapshot")
        assert denied.status_code == 401
        assert "www-authenticate" in denied.headers
        for _ in range(10):
            result = await client.get("/api/desk/snapshot", headers={"X-API-Key": get_settings().api_access_key})
            assert result.status_code == 200
            assert result.headers["cache-control"] == "private, no-store"
            data = result.json()["data"]
            assert data["pnl"]["age_seconds"] is None
            assert data["reconciliation"]["status"] == "UNKNOWN"
            assert data["kite"]["session_saved"] is True
            assert "never-expose-this-secret" not in result.text
    bomb.assert_not_called()
    async_bomb.assert_not_called()
    assert orch.equity_curve == original_curve
    assert orch.portfolio.metrics() == original_metrics


def test_observation_age_and_missing_values(monkeypatch):
    monkeypatch.setattr(desk, "_observations", {})
    monkeypatch.setattr(desk, "monotonic", lambda: 100.0)
    assert desk.read("pnl")["age_seconds"] is None
    source = {"daily_pnl": -25, "stale": True}
    desk.observe("pnl", source)
    source["daily_pnl"] = 999
    monkeypatch.setattr(desk, "monotonic", lambda: 230.5)
    result = desk.read("pnl")
    assert result["age_seconds"] == 130.5
    assert result["data"]["daily_pnl"] == -25
    assert result["data"]["stale"] is True


@pytest.mark.asyncio
async def test_existing_background_writes_publish_observations(monkeypatch):
    from services.control.halt import cache_pnl_snapshot
    from services.autonomous.state import set_autonomous_status, set_autonomous_running, is_autonomous_running
    monkeypatch.setattr(desk, "_observations", {})
    await cache_pnl_snapshot({"daily_pnl": 42, "stale": False})
    await set_autonomous_running(True)
    await set_autonomous_status({"stats": {"scanned": 8}})
    assert await is_autonomous_running()
    assert desk.read("pnl")["data"]["daily_pnl"] == 42
    assert desk.read("running")["data"]["running"] is True
    assert desk.read("autonomous")["data"]["stats"]["scanned"] == 8
    await set_autonomous_running(False)
    assert desk.read("running")["data"]["running"] is False


def test_snapshot_caps_decisions_and_allowlists_fields(monkeypatch):
    monkeypatch.setattr(orch, "decisions", [{"symbol": "TEST", "action": "REJECTED", "risk_reason": "x" * 900, "secret": "hidden"}] * 50)
    result = desk.build_snapshot(orch, {"control": False})
    assert len(result["recent_decisions"]) == 5
    assert len(result["recent_decisions"][0]["risk_reason"]) == 800
    assert "secret" not in result["recent_decisions"][0]
