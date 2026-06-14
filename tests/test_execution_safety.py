"""Execution engine production safety tests."""

import pytest

from services.brokers.base import OrderRequest, OrderStatus, OrderType
from services.core.orchestrator import TradingOrchestrator
from services.execution.circuit_breaker import ApiCircuitBreaker
from services.execution.execution_engine import ExecutionEngine
from services.execution.idempotency import make_order_id
from services.portfolio.manager import PortfolioManager


def test_idempotency_key_stable_within_bucket():
    a = make_order_id("RELIANCE", "momentum", time_bucket_minutes=5)
    b = make_order_id("RELIANCE", "momentum", time_bucket_minutes=5)
    assert a == b
    assert a.startswith("apex-")


def test_idempotency_key_differs_by_symbol():
    a = make_order_id("RELIANCE", "momentum")
    b = make_order_id("TCS", "momentum")
    assert a != b


def test_circuit_breaker_opens_after_threshold():
    breaker = ApiCircuitBreaker()
    breaker.failure_threshold = 3
    breaker.pause_minutes = 10
    assert not breaker.is_open()
    breaker.record_failure("e1")
    breaker.record_failure("e2")
    opened = breaker.record_failure("e3")
    assert opened
    assert breaker.is_open()


@pytest.mark.asyncio
async def test_execution_blocks_when_halted():
    portfolio = PortfolioManager()
    portfolio.emergency_shutdown()
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
    assert "blocked" in result.message.lower()


@pytest.mark.asyncio
async def test_orchestrator_blocks_analyze_when_halted():
    orch = TradingOrchestrator()
    orch.portfolio.emergency_shutdown()
    decision = await orch.analyze_symbol("RELIANCE")
    assert decision["action"] == "NO_TRADE"
    assert "blocked" in decision["reason"].lower() or "halt" in decision["reason"].lower()


@pytest.mark.asyncio
async def test_live_gate_requires_golive_approved(monkeypatch):
    from services.brokers.paper import PaperBroker
    from services.execution.live_gate import LiveSafetyGate
    from services.market_data.service import MarketDataService
    from shared.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("GOLIVE_APPROVED", "false")
    monkeypatch.setenv("ENABLE_LIVE_EXECUTION", "true")
    get_settings.cache_clear()

    cfg = get_settings()
    portfolio = PortfolioManager()
    portfolio.persistence_ok = True
    broker = PaperBroker()
    await broker.connect()
    ok, blockers = await LiveSafetyGate.check(
        market_data=MarketDataService(),
        broker=broker,
        portfolio=portfolio,
        settings=cfg,
    )
    assert not ok
    assert any("GOLIVE_APPROVED" in b for b in blockers)
