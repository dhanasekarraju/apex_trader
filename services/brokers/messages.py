"""Broker message helpers."""

from __future__ import annotations


def is_insufficient_balance(message: str) -> bool:
    """True when the broker rejected an order for lack of funds/margin."""
    text = (message or "").lower()
    needles = (
        "insufficient",
        "margin shortfall",
        "required margin",
        "not enough",
        "fund",
        "balance",
        "exceeds available",
        "margin available",
    )
    return any(n in text for n in needles)
