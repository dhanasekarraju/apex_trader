"""CRCE recorder — hooks all subsystems; fail-safe to SAFE_MODE on outage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from services.compliance.events import EventType
from services.compliance.store import EventStore, build_event
from services.portfolio.manager import PortfolioManager
from shared.logging import audit

_BUFFER_PATH = Path(__file__).resolve().parents[2] / "data" / "compliance" / "pending_buffer.jsonl"


def portfolio_snapshot(portfolio: PortfolioManager | None) -> dict[str, Any]:
    if portfolio is None:
        return {}
    s = portfolio.state
    return {
        "equity": round(s.equity, 2),
        "cash": round(s.cash, 2),
        "daily_pnl": round(s.daily_pnl, 2),
        "weekly_pnl": round(s.weekly_pnl, 2),
        "monthly_pnl": round(s.monthly_pnl, 2),
        "open_positions": len(s.positions),
        "positions": [
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "entry": p.entry,
                "stop_loss": p.stop_loss,
                "take_profit": p.take_profit,
                "strategy": p.strategy,
            }
            for p in s.positions
        ],
        "emergency_halt": s.emergency_halt,
        "black_swan_mode": s.black_swan_mode,
    }


class ComplianceRecorder:
    """Control Replay & Compliance Engine — central event recorder."""

    def __init__(self) -> None:
        self.store = EventStore()
        self._healthy = True
        self._buffer: list[dict] = []

    @property
    def healthy(self) -> bool:
        return self._healthy

    async def record(
        self,
        *,
        event_type: EventType,
        action: str = "",
        symbol: str = "",
        decision: str = "",
        state_snapshot: dict[str, Any] | None = None,
        reason: str = "",
        latency_ms: float = 0.0,
        portfolio: PortfolioManager | None = None,
        risk_state: dict[str, Any] | str | None = None,
        system_state: str = "",
        **extra: Any,
    ) -> dict[str, Any] | None:
        snapshot = dict(state_snapshot or {})
        if portfolio is not None:
            snapshot.setdefault("portfolio", portfolio_snapshot(portfolio))
        if risk_state is not None:
            snapshot.setdefault(
                "risk_state",
                risk_state if isinstance(risk_state, dict) else {"status": risk_state},
            )
        if system_state:
            snapshot.setdefault("system_state", system_state)

        event = build_event(
            event_type=event_type,
            action=action,
            symbol=symbol,
            decision=decision,
            state_snapshot=snapshot,
            reason=reason,
            latency_ms=latency_ms,
            **extra,
        )
        return await self._append(event)

    async def _append(self, event: dict[str, Any]) -> dict[str, Any] | None:
        try:
            self._flush_buffer_sync()
            record = self.store.append(event)
            self._healthy = True
            return record
        except Exception as exc:
            self._healthy = False
            self._buffer.append(event)
            try:
                _BUFFER_PATH.parent.mkdir(parents=True, exist_ok=True)
                with _BUFFER_PATH.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event, default=str) + "\n")
            except Exception:
                pass
            audit("crce_record_failed", error=str(exc))
            await self._engage_safe_mode(str(exc))
            return None

    def _persist_buffer(self) -> None:
        pass

    def _flush_buffer_sync(self) -> None:
        if not self._buffer and not _BUFFER_PATH.is_file():
            return
        pending: list[dict] = list(self._buffer)
        if _BUFFER_PATH.is_file():
            with _BUFFER_PATH.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        pending.append(json.loads(line))
            _BUFFER_PATH.unlink(missing_ok=True)
        self._buffer.clear()
        for ev in pending:
            self.store.append(ev)

    async def _engage_safe_mode(self, reason: str) -> None:
        from services.icb.engine import icb

        await icb.enter_safe_mode(reason)

    async def recover(self) -> dict[str, Any]:
        try:
            self._flush_buffer_sync()
            self._healthy = True
            from services.icb.engine import icb

            await icb.recover_safe_mode()
            return {"ok": True, "message": "CRCE recovered, buffer flushed"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


crce = ComplianceRecorder()
