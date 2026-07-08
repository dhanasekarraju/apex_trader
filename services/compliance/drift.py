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
        return self._scan_events(events)

    def scan_recent(self, limit: int = 800) -> list[dict[str, Any]]:
        """Scan only recent events — fast path for ICB on large ledgers."""
        events = self.store.load_all()
        if len(events) > limit:
            events = events[-limit:]
        return self._scan_events(events)

    def _scan_events(self, events: list[dict]) -> list[dict[str, Any]]:
        """Detect GENUINE execution drift only.

        The engine re-scans the same symbols every cycle, so a symbol being
        risk-rejected in one cycle and legitimately traded in a later cycle is
        NORMAL — not drift. We therefore track only the *latest* risk decision
        per symbol and flag an order that contradicts it. Speculative rules
        (e.g. "BUY signal had no order") are intentionally excluded: a gated
        signal is expected behaviour, not a compliance violation.
        """
        drifts: list[dict[str, Any]] = []

        # Most-recent risk decision per symbol; an APPROVE clears any prior reject.
        latest_risk: dict[str, str] = {}

        for ev in events:
            et = ev.get("event_type", "")
            sym = (ev.get("symbol") or "").upper()
            ts = ev.get("timestamp", "")

            if et == EventType.RISK_EVALUATION.value and sym:
                latest_risk[sym] = ev.get("decision", "")

            # Genuine drift: an order reached the broker while this symbol's most
            # recent risk decision was a rejection (should be impossible).
            if et in (EventType.ORDER_PLACED.value, EventType.ORDER_FILLED.value) and sym:
                if latest_risk.get(sym) in ("REJECTED", "DENY", "HALTED"):
                    drifts.append(
                        self._drift(
                            "risk_vs_execution",
                            "CRITICAL",
                            sym,
                            ts,
                            f"risk {latest_risk[sym]}",
                            et,
                        )
                    )
                # An order implies a fresh approval path; clear the stale state.
                latest_risk[sym] = "APPROVED"

            # Genuine drift: broker filled a different quantity than we intended.
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
                                ts,
                                str(internal_qty),
                                str(broker_qty),
                            )
                        )

            # Genuine drift: stop-loss exit filled materially worse than planned.
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
                                ts,
                                str(expected_sl),
                                str(actual),
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
