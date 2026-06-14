"""API circuit breaker — pause trading after repeated broker failures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shared.config import get_settings
from shared.logging import audit


class ApiCircuitBreaker:
    """Opens after N consecutive API failures; pauses trading for M minutes."""

    def __init__(self) -> None:
        cfg = get_settings()
        self.failure_threshold = cfg.api_failure_threshold
        self.pause_minutes = cfg.api_circuit_pause_minutes
        self._failures = 0
        self._paused_until: datetime | None = None

    def is_open(self) -> bool:
        if self._paused_until is None:
            return False
        if datetime.now(timezone.utc) >= self._paused_until:
            self._paused_until = None
            self._failures = 0
            audit("api_circuit_closed")
            return False
        return True

    def pause_remaining_sec(self) -> int:
        if not self.is_open() or self._paused_until is None:
            return 0
        delta = self._paused_until - datetime.now(timezone.utc)
        return max(0, int(delta.total_seconds()))

    def record_success(self) -> None:
        if self._failures:
            self._failures = 0

    def record_failure(self, reason: str) -> bool:
        """Record failure. Returns True if circuit just opened."""
        self._failures += 1
        audit("api_failure", count=self._failures, reason=reason)
        if self._failures >= self.failure_threshold:
            self._paused_until = datetime.now(timezone.utc) + timedelta(
                minutes=self.pause_minutes
            )
            audit(
                "api_circuit_open",
                failures=self._failures,
                pause_minutes=self.pause_minutes,
            )
            self._failures = 0
            return True
        return False

    def status(self) -> dict:
        return {
            "open": self.is_open(),
            "failures": self._failures,
            "pause_remaining_sec": self.pause_remaining_sec(),
        }
