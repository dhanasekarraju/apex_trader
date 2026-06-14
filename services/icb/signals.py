"""System health signals for ICB — authority checks only."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.control.reconciliation_state import is_reconciliation_degraded
from shared.config import get_settings


@dataclass
class SystemSignals:
    crce_integrity: str = "ok"
    drift_count: int = 0
    reconciliation_degraded: bool = False
    issues: list[str] = field(default_factory=list)


async def collect_signals(context: dict[str, Any]) -> SystemSignals:
    cfg = get_settings()
    signals = SystemSignals()
    signals.reconciliation_degraded = await is_reconciliation_degraded()
    if signals.reconciliation_degraded:
        signals.issues.append("Broker reconciliation degraded")

    try:
        from services.compliance.store import EventStore

        integrity = EventStore().verify_chain()
        signals.crce_integrity = "ok" if integrity.get("valid") else "drift"
        if not integrity.get("valid"):
            signals.issues.append("CRCE hash chain invalid")
    except Exception as exc:
        signals.crce_integrity = "failed"
        signals.issues.append(f"CRCE check error: {exc}")

    if cfg.trading_mode == "live" and signals.crce_integrity != "ok":
        signals.issues.append("LIVE requires valid CRCE chain")

    try:
        from services.compliance.drift import DriftDetector

        drifts = DriftDetector().scan()
        signals.drift_count = len(drifts)
        if drifts:
            signals.issues.append(f"{len(drifts)} drift(s) detected")
    except Exception:
        pass

    return signals
