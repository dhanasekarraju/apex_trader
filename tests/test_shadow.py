"""Shadow mode tests."""

import os
os.environ.setdefault("TRADING_MODE", "shadow")

from services.brokers.base import OrderRequest, OrderType
from services.shadow.engine import ShadowEngine


def test_shadow_simulates_fill():
    engine = ShadowEngine()
    req = OrderRequest("RELIANCE", "long", 10, OrderType.MARKET, strategy="momentum")
    result = engine.simulate(req, 2500.0)
    assert result.filled_qty == 10
    assert result.avg_price > 2500.0
    report = engine.weekly_report()
    assert report["simulated_fills"] == 1


def test_shadow_records_missed():
    engine = ShadowEngine()
    engine.record_missed("TCS", "Risk rejected", 3500.0)
    report = engine.weekly_report()
    assert report["missed_opportunities"] == 1
