"""Control Layer telemetry — append-only audit trail."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.control.actions import ControlAction
from services.control.system_state import SystemState

_LOG_DIR = Path(__file__).resolve().parents[2] / "data" / "control"
_AUDIT_PATH = _LOG_DIR / "audit.jsonl"


def log_control_event(
    *,
    action: ControlAction | str,
    decision: str,
    reason: str,
    system_state: SystemState | str,
    risk_state: str = "",
    latency_ms: float = 0.0,
    **extra: object,
) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action.value if isinstance(action, ControlAction) else str(action),
        "decision": decision,
        "reason": reason,
        "system_state": system_state.value if isinstance(system_state, SystemState) else str(system_state),
        "risk_state": risk_state,
        "latency_ms": round(latency_ms, 2),
        **extra,
    }
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")
