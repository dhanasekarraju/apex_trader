"""Deterministic order idempotency keys."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone


def make_order_id(
    symbol: str,
    strategy: str,
    *,
    time_bucket_minutes: int = 5,
    side: str = "long",
) -> str:
    """
    Stable idempotency key: hash(symbol + strategy + time_bucket).
    Prevents duplicate orders for the same signal within a bucket.
    """
    now = datetime.now(timezone.utc)
    bucket = now.replace(
        minute=(now.minute // time_bucket_minutes) * time_bucket_minutes,
        second=0,
        microsecond=0,
    )
    raw = f"{symbol.upper()}|{strategy}|{side}|{bucket.isoformat()}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"apex-{digest}"
