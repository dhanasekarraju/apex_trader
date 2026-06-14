"""Append-only compliance event store with SHA-256 hash chaining."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.compliance.events import EventType

_ROOT = Path(__file__).resolve().parents[2]
EVENT_LOG = _ROOT / "data" / "compliance" / "event_log.jsonl"
HASH_FILE = _ROOT / "data" / "compliance" / "last_hash.txt"
GENESIS_HASH = "0" * 64


def _ensure_dirs() -> None:
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)


def _read_last_hash() -> str:
    if HASH_FILE.is_file():
        return HASH_FILE.read_text(encoding="utf-8").strip() or GENESIS_HASH
    if EVENT_LOG.is_file():
        last_line = ""
        with EVENT_LOG.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last_line = line
        if last_line:
            try:
                return json.loads(last_line).get("event_hash", GENESIS_HASH)
            except json.JSONDecodeError:
                pass
    return GENESIS_HASH


def _write_last_hash(value: str) -> None:
    _ensure_dirs()
    HASH_FILE.write_text(value, encoding="utf-8")


def build_event(
    *,
    event_type: EventType | str,
    action: str = "",
    symbol: str = "",
    decision: str = "",
    state_snapshot: dict[str, Any] | None = None,
    reason: str = "",
    latency_ms: float = 0.0,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type.value if isinstance(event_type, EventType) else str(event_type),
        "action": action,
        "symbol": symbol,
        "decision": decision,
        "state_snapshot": state_snapshot or {},
        "reason": reason,
        "latency_ms": round(latency_ms, 2),
        **extra,
    }


class EventStore:
    """Immutable append-only ledger with tamper-evident hash chain."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or EVENT_LOG
        self._hash_file = HASH_FILE if path is None else self.path.with_suffix(self.path.suffix + ".hash")

    def _read_last_hash(self) -> str:
        if self._hash_file.is_file():
            value = self._hash_file.read_text(encoding="utf-8").strip()
            if value:
                return value
        if self.path.is_file():
            last_line = ""
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last_line = line
            if last_line:
                try:
                    return json.loads(last_line).get("event_hash", GENESIS_HASH)
                except json.JSONDecodeError:
                    pass
        return GENESIS_HASH

    def _write_last_hash(self, value: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._hash_file.write_text(value, encoding="utf-8")

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        prev_hash = self._read_last_hash()
        canonical = json.dumps(event, sort_keys=True, default=str)
        event_hash = hashlib.sha256(f"{prev_hash}{canonical}".encode()).hexdigest()
        record = {
            "prev_hash": prev_hash,
            "event_hash": event_hash,
            **event,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        self._write_last_hash(event_hash)
        return record

    def load_all(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def load_range(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        events = self.load_all()
        if not start_time and not end_time:
            return events
        out: list[dict] = []
        for ev in events:
            ts = datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00"))
            if start_time and ts < start_time:
                continue
            if end_time and ts > end_time:
                continue
            out.append(ev)
        return out

    def verify_chain(self) -> dict[str, Any]:
        events = self.load_all()
        if not events:
            return {"valid": True, "events": 0, "message": "empty ledger"}

        prev = GENESIS_HASH
        for i, record in enumerate(events):
            stored_prev = record.get("prev_hash", GENESIS_HASH)
            if stored_prev != prev:
                return {
                    "valid": False,
                    "broken_at_index": i,
                    "expected_prev_hash": prev,
                    "stored_prev_hash": stored_prev,
                }
            body = {k: v for k, v in record.items() if k not in ("prev_hash", "event_hash")}
            canonical = json.dumps(body, sort_keys=True, default=str)
            expected = hashlib.sha256(f"{prev}{canonical}".encode()).hexdigest()
            if record.get("event_hash") != expected:
                return {
                    "valid": False,
                    "broken_at_index": i,
                    "expected_hash": expected,
                    "stored_hash": record.get("event_hash"),
                }
            prev = record["event_hash"]
        return {"valid": True, "events": len(events), "head_hash": prev}
