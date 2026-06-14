"""Broker factory — select adapter by mode and config."""

from __future__ import annotations

from services.brokers.base import BrokerAdapter
from services.brokers.ccxt_broker import CCXTBroker
from services.brokers.ib import InteractiveBrokersAdapter
from services.brokers.kite import KiteBroker
from services.brokers.paper import PaperBroker
from shared.config import get_settings


def get_broker(mode: str | None = None) -> BrokerAdapter:
    cfg = get_settings()
    mode = mode or cfg.trading_mode

    if mode in ("paper", "shadow"):
        return PaperBroker()

    broker = cfg.default_broker.lower()
    if broker == "kite":
        return KiteBroker()
    if broker == "ib":
        return InteractiveBrokersAdapter()
    if broker == "ccxt":
        return CCXTBroker()
    return PaperBroker()
