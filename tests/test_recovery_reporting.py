"""Recovery warnings must be recorded without crashing or bypassing entry gates."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.brokers.base import OrderRequest, OrderStatus, OrderType
from services.core.orchestrator import TradingOrchestrator
from services.control.reconciliation_state import is_reconciliation_degraded
from shared.config import get_settings


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["manual_position", "broker_unavailable", "flat"])
async def test_startup_records_recovery_and_preserves_entry_gate(monkeypatch, scenario):
    import services.compliance.recorder as recorder
    import services.compliance.store as store
    from services.brokers.kite_auth import kite_auth

    cfg = get_settings()
    cfg.trading_mode = "live"
    cfg.default_broker = "kite"
    cfg.autonomous_auto_start = False
    orch = TradingOrchestrator()
    positions = [{"symbol": "INOXGREEN", "qty": 10, "side": "long"}]
    broker = SimpleNamespace(
        connect=AsyncMock(return_value=True),
        fetch_open_positions=AsyncMock(return_value=positions if scenario == "manual_position" else []),
        place_order=AsyncMock(),
        cancel_order=AsyncMock(),
    )
    if scenario == "broker_unavailable":
        broker.fetch_open_positions.side_effect = ConnectionError("broker unavailable")
    orch.execution._broker = broker
    monkeypatch.setattr(kite_auth, "startup", AsyncMock())
    monkeypatch.setattr(orch, "sync_capital_from_kite", AsyncMock())
    monkeypatch.setattr(orch, "refresh_control_cache", AsyncMock())
    monkeypatch.setattr(recorder, "crce", recorder.ComplianceRecorder())

    await orch.startup()

    events = [json.loads(line) for line in store.EVENT_LOG.read_text(encoding="utf-8").splitlines()]
    event = next(event for event in events if event["action"] == "RECONCILE_PORTFOLIO")
    degraded = scenario != "flat"
    assert event["reconciliation_status"] == ("DEGRADED" if degraded else "OK")
    if scenario == "manual_position":
        assert "INOXGREEN" in event["reason"]
    elif scenario == "broker_unavailable":
        assert "broker unavailable" in event["reason"]
    else:
        assert event["reason"] == "OK"
    assert await is_reconciliation_degraded() is degraded
    assert orch.portfolio.state.positions == []
    assert await orch.execution._trades.open_trades() == []
    if degraded:
        result = await orch.execution.place_order(OrderRequest(
            symbol="OTHER", side="long", qty=1, order_type=OrderType.MARKET,
            stop_price=95,
        ), market_price=100)
        assert result.status == OrderStatus.REJECTED
        assert "Reconciliation degraded" in result.message
    broker.place_order.assert_not_awaited()
    broker.cancel_order.assert_not_awaited()
