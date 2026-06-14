"""Drift detection — expected vs actual execution outcomes."""

from __future__ import annotations

from typing import Any

from services.compliance.events import EventType
from services.compliance.store import EventStore


class DriftDetector:
    """Scan event log for mismatches between expected and actual behavior."""

    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or EventStore()

    def scan(self, events: list[dict] | None = None) -> list[dict[str, Any]]:
        events = events or self.store.load_all()
        drifts: list[dict[str, Any]] = []

        risk_by_symbol: dict[str, dict] = {}
        order_context: dict[str, dict] = {}

        for ev in events:
            et = ev.get("event_type", "")
            sym = (ev.get("symbol") or "").upper()

            if et == EventType.RISK_EVALUATION.value and sym:
                risk_by_symbol[sym] = ev

            if et == EventType.ORDER_PLACED.value:
                cid = ev.get("client_order_id") or ev.get("event_id", "")
                order_context[cid] = ev

            if et == EventType.RISK_EVALUATION.value:
                decision = ev.get("decision", "")
                if decision in ("REJECTED", "DENY", "HALTED"):
                    later = self._find_later_fill(events, ev["timestamp"], sym)
                    if later:
                        drifts.append(
                            self._drift(
                                "risk_vs_execution",
                                "CRITICAL",
                                sym,
                                ev["timestamp"],
                                f"risk {decision}",
                                later.get("event_type"),
                            )
                        )

            if et == EventType.ORDER_FILLED.value:
                meta = ev.get("metadata") or {}
                broker_qty = meta.get("broker_filled_qty")
                internal_qty = meta.get("internal_qty") or ev.get("qty")
                if broker_qty is not None and internal_qty is not None:
                    if abs(float(broker_qty) - float(internal_qty)) > 0.001:
                        drifts.append(
                            self._drift(
                                "broker_vs_internal_fill",
                                "HIGH",
                                sym,
                                ev["timestamp"],
                                str(internal_qty),
                                str(broker_qty),
                            )
                        )

            if et == EventType.ORDER_EXITED.value:
                expected_sl = ev.get("expected_stop_loss")
                actual = ev.get("exit_price")
                reason = ev.get("exit_reason", "")
                if expected_sl and reason == "stop_loss" and actual:
                    if float(actual) > float(expected_sl) * 1.02:
                        drifts.append(
                            self._drift(
                                "sl_exit_price",
                                "MEDIUM",
                                sym,
                                ev["timestamp"],
                                str(expected_sl),
                                str(actual),
                            )
                        )

            if et == EventType.AUTONOMOUS_TICK.value:
                action = ev.get("autonomous_action")
                if action == "BUY" and not self._find_later_order(events, ev["timestamp"], sym):
                    drifts.append(
                        self._drift(
                            "autonomous_vs_execution",
                            "HIGH",
                            sym,
                            ev["timestamp"],
                            "BUY signal",
                            "no ORDER_PLACED/FILLED",
                        )
                    )

        return drifts

    @staticmethod
    def _drift(
        drift_type: str,
        severity: str,
        symbol: str,
        timestamp: str,
        expected: str,
        actual: str,
    ) -> dict:
        return {
            "drift_type": drift_type,
            "severity": severity,
            "symbol": symbol,
            "timestamp": timestamp,
            "expected": expected,
            "actual": actual,
        }

    @staticmethod
    def _find_later_fill(events: list[dict], after_ts: str, symbol: str) -> dict | None:
        for ev in events:
            if ev.get("timestamp", "") <= after_ts:
                continue
            if ev.get("symbol", "").upper() != symbol.upper():
                continue
            if ev.get("event_type") in (EventType.ORDER_FILLED.value, EventType.ORDER_PLACED.value):
                return ev
        return None

    @staticmethod
    def _find_later_order(events: list[dict], after_ts: str, symbol: str) -> bool:
        for ev in events:
            if ev.get("timestamp", "") <= after_ts:
                continue
            if ev.get("symbol", "").upper() != symbol.upper():
                continue
            if ev.get("event_type") in (
                EventType.ORDER_PLACED.value,
                EventType.ORDER_FILLED.value,
            ):
                return True
        return False
