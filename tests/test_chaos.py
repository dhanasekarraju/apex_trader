"""Chaos Testing + Live Market Hardening Layer tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.brokers.base import OrderRequest, OrderType
from services.chaos.broker_mock import ChaosBroker
from services.chaos.chaos_engine import ChaosEngine
from services.chaos.latency_simulator import LatencySimulator, PROFILE_RANGES_MS
from services.chaos.resilience_reporter import ResilienceReporter
from services.chaos.scenario_runner import ScenarioResult, ScenarioRunner
from services.chaos.scenarios import CHAOS_SCENARIOS, LatencyProfile, SCENARIO_BY_ID
from shared.config import get_settings
from services.compliance.events import EventType
from services.compliance.store import EventStore
from services.compliance.recorder import crce
from services.icb.system_state import SystemState, get_system_state


@pytest.fixture
def fast_chaos(monkeypatch):
    async def noop_apply(self, override_ms=None):
        return 0.0

    monkeypatch.setattr(
        "services.chaos.latency_simulator.LatencySimulator.apply",
        noop_apply,
    )


@pytest.fixture
def chaos_crce(tmp_path, monkeypatch):
    store = EventStore(path=tmp_path / "chaos_events.jsonl")
    crce.store = store
    monkeypatch.setattr(
        "services.compliance.recorder._BUFFER_PATH",
        tmp_path / "pending_buffer.jsonl",
    )
    return store


@pytest.mark.asyncio
async def test_chaos_scenarios_catalog():
    assert len(CHAOS_SCENARIOS) >= 25
    categories = {s.category.value for s in CHAOS_SCENARIOS}
    assert "broker" in categories
    assert "network" in categories
    assert "system" in categories


def test_latency_profiles_cover_institutional_ranges():
    assert PROFILE_RANGES_MS[LatencyProfile.NORMAL] == (10, 50)
    assert PROFILE_RANGES_MS[LatencyProfile.CRITICAL][1] >= 5000


@pytest.mark.asyncio
async def test_chaos_broker_rejection_spike_deterministic():
    broker = ChaosBroker(seed=101, mode="rejection_spike", fault_config={"reject_rate": 1.0})
    await broker.connect()
    req = OrderRequest(symbol="RELIANCE", side="BUY", qty=1, order_type=OrderType.MARKET)
    r1 = await broker.place_order(req, 2500.0)
    r2 = await broker.place_order(req, 2500.0)
    assert r1.status.value == "rejected"
    assert r2.status.value == "rejected"


@pytest.mark.asyncio
async def test_chaos_broker_partial_fill():
    broker = ChaosBroker(seed=103, mode="partial_fill", fault_config={"fill_ratio": 0.5})
    await broker.connect()
    req = OrderRequest(symbol="RELIANCE", side="BUY", qty=10, order_type=OrderType.MARKET)
    result = await broker.place_order(req, 2500.0)
    assert result.status.value == "partial"
    assert 0 < result.filled_qty < req.qty


@pytest.mark.asyncio
async def test_scenario_runner_logs_crce_events(fast_chaos, chaos_crce, monkeypatch):
    monkeypatch.setenv("ENFORCE_MARKET_HOURS", "false")
    from shared.config import get_settings
    get_settings.cache_clear()

    runner = ScenarioRunner()
    scenario = SCENARIO_BY_ID["broker_rejection_spike"]
    result = await runner.run(scenario)

    assert result.scenario_id == "broker_rejection_spike"
    events = chaos_crce.load_all()
    types = {e["event_type"] for e in events}
    assert EventType.CHAOS_SCENARIO_STARTED.value in types
    assert EventType.FAULT_INJECTED.value in types
    assert EventType.CHAOS_SCENARIO_COMPLETED.value in types


@pytest.mark.asyncio
async def test_crce_failure_triggers_safe_mode(fast_chaos, monkeypatch):
    monkeypatch.setenv("ENFORCE_MARKET_HOURS", "false")
    from shared.config import get_settings
    get_settings.cache_clear()

    runner = ScenarioRunner()
    result = await runner.run(SCENARIO_BY_ID["system_crce_failure"])
    state = await get_system_state()
    assert state == SystemState.SAFE_MODE or result.safe_mode_triggered


@pytest.mark.asyncio
async def test_risk_timeout_scenario(fast_chaos, monkeypatch):
    monkeypatch.setenv("ENFORCE_MARKET_HOURS", "false")
    from shared.config import get_settings
    get_settings.cache_clear()

    runner = ScenarioRunner()
    result = await runner.run(SCENARIO_BY_ID["system_risk_timeout"])
    assert result.execution_status != "filled" or result.icb_decision == "DENY"


@pytest.mark.asyncio
async def test_chaos_engine_quick_suite(fast_chaos, chaos_crce, tmp_path, monkeypatch):
    monkeypatch.setenv("ENFORCE_MARKET_HOURS", "false")
    from shared.config import get_settings
    get_settings.cache_clear()

    engine = ChaosEngine()
    report_path = tmp_path / "chaos_report.json"
    fast_scenarios = [
        SCENARIO_BY_ID["broker_rejection_spike"],
        SCENARIO_BY_ID["market_illiquidity"],
        SCENARIO_BY_ID["system_crce_failure"],
        SCENARIO_BY_ID["system_risk_timeout"],
    ]
    report = await engine.run_suite(fast_scenarios)
    reporter = ResilienceReporter(engine.last_results)
    reporter.generate(path=report_path)

    assert report_path.is_file()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert "resilience_score" in payload
    assert payload["stability_classification"] in (
        "UNSAFE", "DEGRADED", "STABLE", "INSTITUTIONAL-GRADE",
    )
    assert "full_suite" in payload
    assert len(report["scenario_results"]) == 4


def test_safe_for_live_capital_strict_rules():
    results = [
        ScenarioResult(
            "a", passed=True, safe=True, duration_ms=100, icb_decision="DENY",
            kill_switch_triggered=True, reconciliation_ok=True,
        ),
    ] * 25
    reporter = ResilienceReporter(results)
    assert reporter.classify() == "INSTITUTIONAL-GRADE"
    assert reporter.resilience_score() >= 90
    assert reporter.safe_for_live_capital() is True

    bad = [ScenarioResult("x", passed=False, safe=False, duration_ms=100, failures=["fail"])]
    reporter_bad = ResilienceReporter(bad)
    assert reporter_bad.safe_for_live_capital() is False


def test_chaos_live_gate_blocks_without_report(monkeypatch):
    monkeypatch.setenv("CHAOS_GATE_ENFORCE", "false")
    get_settings.cache_clear()
    from services.chaos.live_gate import ChaosLiveGate

    monkeypatch.setattr(ChaosLiveGate, "load_report", lambda path=None: None)
    approved, blockers = ChaosLiveGate.check_for_live()
    assert not approved
    assert any("chaos" in b.lower() and "report" in b.lower() for b in blockers)


def test_check_for_live_never_bypasses_dev_flag(monkeypatch):
    monkeypatch.setenv("CHAOS_GATE_ENFORCE", "false")
    get_settings.cache_clear()
    from services.chaos.live_gate import ChaosLiveGate

    monkeypatch.setattr(ChaosLiveGate, "load_report", lambda path=None: None)
    approved, _ = ChaosLiveGate.check()
    assert approved
    approved_live, blockers = ChaosLiveGate.check_for_live()
    assert not approved_live
    assert blockers


def test_chaos_live_gate_approves_institutional_report(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAOS_GATE_ENFORCE", "true")
    get_settings.cache_clear()
    from datetime import datetime, timezone

    from services.chaos.live_gate import ChaosLiveGate

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "full_suite": True,
        "scenario_count": 25,
        "resilience_score": 95,
        "stability_classification": "INSTITUTIONAL-GRADE",
        "safe_for_live_capital": True,
    }
    path = tmp_path / "chaos_report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    approved, blockers = ChaosLiveGate.validate(report)
    assert approved
    assert blockers == []


def test_resilience_reporter_score_and_classification(tmp_path):
    results = [
        ScenarioResult("a", passed=True, safe=True, duration_ms=100),
        ScenarioResult("b", passed=True, safe=True, duration_ms=200),
        ScenarioResult("c", passed=False, safe=False, duration_ms=300, failures=["x"]),
    ]
    reporter = ResilienceReporter(results)
    assert 0 <= reporter.resilience_score() <= 100
    assert reporter.classify() in ("UNSAFE", "DEGRADED", "STABLE", "INSTITUTIONAL-GRADE")
    out = tmp_path / "chaos_report.json"
    report = reporter.generate(path=out)
    assert report["trade_integrity"]["total_scenarios"] == 3
    assert out.is_file()


@pytest.mark.asyncio
async def test_broker_illiquidity_no_fill(fast_chaos, monkeypatch):
    monkeypatch.setenv("ENFORCE_MARKET_HOURS", "false")
    from shared.config import get_settings
    get_settings.cache_clear()

    runner = ScenarioRunner()
    result = await runner.run(SCENARIO_BY_ID["market_illiquidity"])
    assert result.execution_status.upper() in ("REJECTED", "NO_TRADE", "")
