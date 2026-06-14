"""Recent trade event stream for UI."""

from __future__ import annotations

import json
from pathlib import Path


def recent_trade_events(limit: int = 30) -> list[dict]:
    path = Path(__file__).resolve().parents[2] / "data" / "logs" / "trades.jsonl"
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    events: list[dict] = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(events))
