"""Persistent strategy governance state."""

from __future__ import annotations

import json
from pathlib import Path

from services.governance.models import StrategyRecord
from shared.events import cache_get, cache_set
from shared.logging import audit

_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = _ROOT / "data" / "governance" / "strategy_states.json"
REDIS_KEY = "apex:sge:strategy_states"

_memory: dict[str, dict] = {}


class GovernanceStore:
    async def load(self) -> dict[str, StrategyRecord]:
        raw = await self._load_raw()
        return {name: StrategyRecord.from_dict(data) for name, data in raw.items()}

    async def save(self, records: dict[str, StrategyRecord]) -> None:
        payload = {name: rec.to_dict() for name, rec in records.items()}
        await self._save_raw(payload)

    async def _load_raw(self) -> dict[str, dict]:
        global _memory
        try:
            val = await cache_get(REDIS_KEY)
            if val:
                data = json.loads(val)
                _memory = data
                return data
        except Exception:
            pass
        if _memory:
            return _memory
        if STATE_FILE.is_file():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                _memory = data
                return data
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    async def _save_raw(self, payload: dict[str, dict]) -> None:
        global _memory
        _memory = payload
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            await cache_set(REDIS_KEY, json.dumps(payload), ttl=86400 * 30)
        except Exception:
            pass
        audit("sge_state_persisted", strategies=len(payload))


async def clear_governance_state() -> None:
    """Test helper — reset in-memory and file state."""
    global _memory
    _memory = {}
    if STATE_FILE.is_file():
        STATE_FILE.unlink(missing_ok=True)
    try:
        await cache_set(REDIS_KEY, "{}", ttl=60)
    except Exception:
        pass
