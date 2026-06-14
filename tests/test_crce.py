"""Control Replay & Compliance Engine tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.compliance.drift import DriftDetector
from services.compliance.events import EventType
from services.compliance.recorder import ComplianceRecorder, portfolio_snapshot
from services.compliance.replay import ReplayEngine
from services.compliance.reports import ComplianceReportGenerator
from services.compliance.store import EventStore, build_event
from services.portfolio.manager import PortfolioManager
from services.portfolio.models import PositionView


@pytest.fixture
def store(tmp_path):
    return EventStore(path=tmp_path / "event_log.jsonl")


@pytest.fixture
def recorder(store):
    r = ComplianceRecorder()
    r.store = store
    return r


def test_event_hash_chain_integrity(store):
    e1 = store.append(build_event(event_type=EventType.CONTROL_DECISION, action="TEST", decision="ALLOW"))
    e2 = store.append(build_event(event_type=EventType.ORDER_PLACED, symbol="RELIANCE", decision="EXECUTED"))
    assert e1["event_hash"] != e2["event_hash"]
    assert e2["prev_hash"] == e1["event_hash"]
    verify = store.verify_chain()
    assert verify["valid"] is True
    assert verify["events"] == 2


def test_tampered_chain_detected(store):
    store.append(build_event(event_type=EventType.CONTROL_DECISION, decision="ALLOW"))
    path = store.path
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    import json

    record = json.loads(lines[0])
    record["reason"] = "TAMPERED"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    verify = store.verify_chain()
    assert verify["valid"] is False


@pytest.mark.asyncio
async def test_recorder_append_only(recorder):
    await recorder.record(
        event_type=EventType.CONTROL_DECISION,
        action="ANALYZE_SYMBOL",
        symbol="TCS",
        decision="ALLOW",
        reason="ok",
    )
    events = recorder.store.load_all()
    assert len(events) == 1
    assert events[0]["event_type"] == "CONTROL_DECISION"


@pytest.mark.asyncio
async def test_recorder_fail_safe_engages_safe_mode(recorder, monkeypatch):
    from services.control.layer import control_layer

    await control_layer.recover_safe_mode()

    def boom(_event):
        raise OSError("disk full")

    monkeypatch.setattr(recorder.store, "append", boom)
    result = await recorder.record(
        event_type=EventType.ORDER_PLACED,
        symbol="INFY",
        decision="EXECUTED",
    )
    assert result is None
    assert not recorder.healthy
    assert control_layer.healthy is False


def test_replay_reconstructs_portfolio(store):
    pf = {
        "equity": 1_010_000,
        "cash": 760_000,
        "daily_pnl": 10_000,
        "positions": [{"symbol": "RELIANCE", "qty": 10, "entry": 2500}],
    }
    store.append(
        build_event(
            event_type=EventType.PORTFOLIO_UPDATE,
            symbol="RELIANCE",
            decision="EXECUTED",
            state_snapshot={"portfolio": pf},
        )
    )
    result = ReplayEngine(store).replay()
    assert result["reconstructed"]["equity"] == 1_010_000
    assert result["reconstructed"]["open_positions"] == 1


def test_replay_divergence_detection(store):
    pf = {"equity": 1_000_000, "cash": 1_000_000, "daily_pnl": 0, "positions": []}
    store.append(
        build_event(
            event_type=EventType.PORTFOLIO_UPDATE,
            decision="EXECUTED",
            state_snapshot={"portfolio": pf},
        )
    )
    reference = {"portfolio": {"equity": 999_000, "cash": 1_000_000, "daily_pnl": 0, "positions": []}}
    result = ReplayEngine(store).replay(reference_snapshot=reference)
    assert result["divergence"]["checked"] is True
    assert result["divergence"]["matches"] is False


def test_drift_risk_vs_execution(store):
    store.append(
        build_event(
            event_type=EventType.RISK_EVALUATION,
            symbol="RELIANCE",
            decision="DENY",
            timestamp="2026-06-14T10:00:00+00:00",
        )
    )
    store.append(
        build_event(
            event_type=EventType.ORDER_FILLED,
            symbol="RELIANCE",
            decision="EXECUTED",
            timestamp="2026-06-14T10:00:01+00:00",
        )
    )
    drifts = DriftDetector(store).scan()
    assert any(d["drift_type"] == "risk_vs_execution" for d in drifts)


def test_drift_broker_vs_internal(store):
    store.append(
        build_event(
            event_type=EventType.ORDER_FILLED,
            symbol="TCS",
            decision="EXECUTED",
            metadata={"broker_filled_qty": 8, "internal_qty": 10},
        )
    )
    drifts = DriftDetector(store).scan()
    assert any(d["drift_type"] == "broker_vs_internal_fill" for d in drifts)


def test_kill_switch_in_replay_timeline(store):
    store.append(
        build_event(
            event_type=EventType.KILL_SWITCH_TRIGGERED,
            decision="EXECUTED",
            reason="admin kill",
            state_snapshot={"system_state": "KILL_SWITCHED"},
        )
    )
    result = ReplayEngine(store).replay()
    assert result["reconstructed"]["system_state"] == "KILL_SWITCHED"


def test_compliance_report_generated(store, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "services.compliance.reports._REPORT_DIR",
        tmp_path / "reports",
    )
    store.append(build_event(event_type=EventType.CONTROL_DECISION, decision="ALLOW", symbol="RELIANCE"))
    report = ComplianceReportGenerator(store).generate()
    assert report["integrity"]["valid"] is True
    assert Path(report["paths"]["json"]).is_file()
    assert Path(report["paths"]["markdown"]).is_file()


def test_portfolio_snapshot_helper():
    pm = PortfolioManager()
    pm.state.positions.append(
        PositionView(
            symbol="INFY",
            qty=5,
            entry=1500,
            stop_loss=1450,
            take_profit=1600,
            strategy="momentum",
            unrealized_pnl=0,
            risk_pct=0.5,
        )
    )
    snap = portfolio_snapshot(pm)
    assert snap["open_positions"] == 1
    assert snap["positions"][0]["symbol"] == "INFY"


def test_missing_event_id_detection(store):
    bad = build_event(event_type=EventType.ORDER_PLACED, decision="EXECUTED")
    del bad["event_id"]
    store.append(bad)
    result = ReplayEngine(store).replay()
    assert result["missing_events"]
