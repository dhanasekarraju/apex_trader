"""System health signals for ICB — authority checks only."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from services.control.reconciliation_state import is_reconciliation_degraded
from shared.config import get_settings

_SIGNALS_CACHE_TTL_SEC = 60.0
_signals_cache: tuple[float, "SystemSignals"] | None = None


@dataclass
class SystemSignals:
    crce_integrity: str = "ok"
    drift_count: int = 0
    reconciliation_degraded: bool = False
    issues: list[str] = field(default_factory=list)


def invalidate_signals_cache() -> None:
    global _signals_cache
    _signals_cache = None


async def collect_signals(context: dict[str, Any]) -> SystemSignals:
    global _signals_cache
    now = time.monotonic()
    if _signals_cache and (now - _signals_cache[0]) < _SIGNALS_CACHE_TTL_SEC:
        return _signals_cache[1]

    signals = await _collect_signals_inner(context)
    _signals_cache = (now, signals)
    return signals


async def _collect_signals_inner(context: dict[str, Any]) -> SystemSignals:
    cfg = get_settings()
    signals = SystemSignals()
    signals.reconciliation_degraded = await is_reconciliation_degraded()
    if signals.reconciliation_degraded:
        signals.issues.append("Broker reconciliation degraded")

    try:
        from services.compliance.store import EventStore

        integrity = await asyncio.to_thread(EventStore().verify_chain)
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

        drifts = await asyncio.to_thread(DriftDetector().scan_recent, 800)
        signals.drift_count = len(drifts)
        if drifts:
            signals.issues.append(f"{len(drifts)} drift(s) detected")
    except Exception:
        pass

    return signals
