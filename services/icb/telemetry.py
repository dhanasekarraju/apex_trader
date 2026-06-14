"""ICB audit telemetry — local ledger + CRCE (audit only)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.icb.actions import ICBAction
from services.icb.decisions import ICBResult

_AUDIT_PATH = Path(__file__).resolve().parents[2] / "data" / "icb" / "audit.jsonl"


def log_icb_decision(action: ICBAction, result: ICBResult, *, extra: dict[str, Any] | None = None) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action.value,
        "decision": result.decision,
        "system_state": result.system_state.value,
        "reason": result.reason,
        "crce_integrity": result.crce_integrity,
        "latency_ms": round(result.latency_ms, 2),
        "layer_denies": result.layer_denies,
        **(extra or {}),
    }
    _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


async def log_icb_crce(action: ICBAction, result: ICBResult) -> None:
    from services.compliance.events import EventType
    from services.compliance.recorder import crce

    await crce.record(
        event_type=EventType.ICB_DECISION,
        action=action.value,
        decision=result.decision,
        reason=result.reason,
        latency_ms=result.latency_ms,
        metadata={
            "system_state": result.system_state.value,
            "crce_integrity": result.crce_integrity,
        },
    )
