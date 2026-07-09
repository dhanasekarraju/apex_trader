"""Kite broker — market protection and order compliance tests."""

import os

os.environ.setdefault("KITE_MARKET_PROTECTION", "-1")
os.environ.setdefault("KITE_EXCHANGE", "NSE")
os.environ.setdefault("KITE_PRODUCT", "MIS")

from services.brokers.base import OrderRequest, OrderType
from services.brokers.kite import KiteBroker


class _FakeKite:
    VARIETY_REGULAR = "regular"
    EXCHANGE_NSE = "NSE"
    PRODUCT_MIS = "MIS"
    VALIDITY_DAY = "DAY"
    ORDER_TYPE_MARKET = "MARKET"
    ORDER_TYPE_LIMIT = "LIMIT"
    ORDER_TYPE_SLM = "SL-M"
    ORDER_TYPE_SL = "SL"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"


def test_market_order_includes_market_protection():
    broker = KiteBroker()
    broker._kite = _FakeKite()
    req = OrderRequest("RELIANCE", "long", 10, OrderType.MARKET)
    params = broker._build_params(req, 10)
    assert params["order_type"] == "MARKET"
    assert params["market_protection"] == -1
    assert params["validity"] == "DAY"
    assert params["exchange"] == "NSE"
    assert params["product"] == "MIS"


def test_stop_uses_slm_with_protection():
    broker = KiteBroker()
    broker._kite = _FakeKite()
    req = OrderRequest(
        "RELIANCE", "short", 10, OrderType.STOP, stop_price=2400.0,
    )
    params = broker._build_params(req, 10)
    assert params["order_type"] == "SL-M"
    assert params["trigger_price"] == 2400.0
    assert params["market_protection"] == -1


def test_offtick_stop_price_snapped_to_tick():
    """Kite rejects off-tick trigger prices; broker must round to a valid tick."""
    broker = KiteBroker()
    broker._kite = _FakeKite()
    req = OrderRequest(
        "ONGC", "short", 5, OrderType.STOP, stop_price=245.6789,
    )
    params = broker._build_params(req, 5)
    trigger = params["trigger_price"]
    assert abs(round(trigger / 0.05) * 0.05 - trigger) < 1e-9
    assert trigger == 245.7


def test_limit_order_has_no_market_protection():
    broker = KiteBroker()
    broker._kite = _FakeKite()
    req = OrderRequest(
        "RELIANCE", "long", 10, OrderType.LIMIT, limit_price=2500.0,
    )
    params = broker._build_params(req, 10)
    assert params["order_type"] == "LIMIT"
    assert "market_protection" not in params


def test_zero_protection_config_falls_back_to_auto():
    broker = KiteBroker()
    broker._kite = _FakeKite()
    broker.cfg.kite_market_protection = 0
    assert broker._market_protection() == -1


def test_metadata_overrides_exchange_and_product():
    broker = KiteBroker()
    broker._kite = _FakeKite()
    req = OrderRequest(
        "NIFTY24JUNFUT",
        "long",
        50,
        OrderType.MARKET,
        metadata={"exchange": "NFO", "product": "NRML"},
    )
    params = broker._build_params(req, 50)
    assert params["exchange"] == "NFO"
    assert params["product"] == "NRML"


def test_flatten_side_long_and_short():
    assert KiteBroker._flatten_side(100) == ("short", 100)
    assert KiteBroker._flatten_side(-75) == ("long", 75)
    assert KiteBroker._flatten_side(0) == ("", 0)


def test_net_positions_from_kite_response():
    resp = {
        "net": [{"tradingsymbol": "RELIANCE", "quantity": 10}],
        "day": [{"tradingsymbol": "RELIANCE", "quantity": 10}],
    }
    net = KiteBroker._net_positions(resp)
    assert len(net) == 1
    assert net[0]["tradingsymbol"] == "RELIANCE"


def test_flatten_params_for_short_position():
    broker = KiteBroker()
    broker._kite = _FakeKite()
    side, qty = broker._flatten_side(-50)
    req = OrderRequest(
        "RELIANCE",
        side,
        qty,
        OrderType.MARKET,
        metadata={"product": "MIS", "exchange": "NSE"},
    )
    params = broker._build_params(req, qty)
    assert params["transaction_type"] == "BUY"
    assert params["quantity"] == 50
    assert params["market_protection"] == -1
