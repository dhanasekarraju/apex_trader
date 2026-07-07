"""Self-healing chaos report.

The live gate requires a fresh institutional chaos report. When it goes stale
(default 7 days) live trading would block and autonomous would never start.
This module auto-re-runs the suite in the background so the operator never has
to babysit it. Genuine resilience failures (low score / unsafe) are NOT masked
— only staleness/absence is auto-healed.
"""

from __future__ import annotations

import asyncio

from shared.logging import audit

_lock = asyncio.Lock()
_task: asyncio.Task | None = None


def is_running() -> bool:
    return _task is not None and not _task.done()


async def _run(quick: bool) -> None:
    try:
        from services.chaos.chaos_engine import chaos_engine

        report = await chaos_engine.run_suite(quick=quick)
        audit(
            "chaos_auto_refresh_complete",
            score=report.get("resilience_score"),
            classification=report.get("stability_classification"),
        )
    except Exception as exc:  # noqa: BLE001 - background best-effort
        audit("chaos_auto_refresh_failed", error=str(exc))


async def ensure_fresh_report(*, quick: bool = False) -> bool:
    """Start a background chaos suite if one isn't already running.

    Returns True if a run was started, False if one was already in progress.
    """
    global _task
    if is_running():
        return False
    async with _lock:
        if is_running():
            return False
        _task = asyncio.create_task(_run(quick))
        audit("chaos_auto_refresh_started", quick=quick)
        return True
