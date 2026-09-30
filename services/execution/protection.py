"""Validate a broker stop against the exposure it is meant to protect."""
import math


def stop_problem(status: dict, *, symbol: str, qty: float, product: str, exchange: str) -> str:
    if status.get("status") not in ("OPEN", "TRIGGER PENDING"):
        return "Stop is not working"
    try:
        pending = float(status["pending_quantity"])
        filled = float(status["filled_quantity"])
        trigger = float(status["trigger_price"])
        if not all(math.isfinite(x) for x in (pending, filled, trigger, qty)):
            return "Invalid stop quantities"
        if filled != 0 or abs(pending - qty) > 0.0001 or qty <= 0:
            return "Stop quantity differs or has partial executions; reconcile fills"
        if trigger <= 0:
            return "Invalid stop trigger"
    except (KeyError, TypeError, ValueError):
        return "Stop details unavailable"
    return stop_identity_problem(status, symbol=symbol, product=product, exchange=exchange)


def stop_identity_problem(status: dict, *, symbol: str, product: str, exchange: str) -> str:
    for key, expected in (("tradingsymbol", symbol), ("transaction_type", "SELL"),
                          ("product", product), ("exchange", exchange)):
        if str(status.get(key, "")).upper() != str(expected).upper():
            return f"Stop {key} does not match exposure"
    if status.get("order_type") not in ("SL", "SL-M"):
        return "Protective order is not a stop"
    return ""
