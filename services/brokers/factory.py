"""Broker factory — select adapter by mode and config."""

from __future__ import annotations

from services.brokers.base import BrokerAdapter
from services.brokers.ccxt_broker import CCXTBroker
from services.brokers.ib import InteractiveBrokersAdapter
from services.brokers.kite import KiteBroker
from services.brokers.paper import PaperBroker
from shared.config import get_settings

_cached: BrokerAdapter | None = None
_cached_key: str | None = None


def get_broker(mode: str | None = None) -> BrokerAdapter:
    """Return a process-wide broker instance for the active mode.

    Creating a fresh KiteBroker on every call dropped the post-login session
    (UI showed connected via kite_auth, while order/lifecycle paths reconnected
    with a new empty client and spammed kite_connect_failed).
    """
    global _cached, _cached_key
    cfg = get_settings()
    mode = mode or cfg.trading_mode
    key = f"{mode}:{cfg.default_broker.lower()}"

    if _cached is not None and _cached_key == key:
        return _cached

    if mode in ("paper", "shadow"):
        broker: BrokerAdapter = PaperBroker()
    else:
        name = cfg.default_broker.lower()
        if name == "kite":
            broker = KiteBroker()
        elif name == "ib":
            broker = InteractiveBrokersAdapter()
        elif name == "ccxt":
            broker = CCXTBroker()
        else:
            broker = PaperBroker()

    _cached = broker
    _cached_key = key
    return broker


def reset_broker_cache() -> None:
    """Drop cached adapter (after mode switch or kite disconnect)."""
    global _cached, _cached_key
    _cached = None
    _cached_key = None
