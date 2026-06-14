"""Input validation — symbols and trading inputs."""

from __future__ import annotations

import re

_SYMBOL_RE = re.compile(r"^[A-Z0-9&.-]{1,32}$")


def normalize_symbol(symbol: str) -> str:
    """Sanitize and validate NSE-style trading symbols."""
    cleaned = symbol.strip().upper()
    if not cleaned or not _SYMBOL_RE.match(cleaned):
        raise ValueError(
            "Invalid symbol — use 1-32 alphanumeric characters (e.g. RELIANCE, TCS)"
        )
    return cleaned
