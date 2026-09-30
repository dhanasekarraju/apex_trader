"""Passive dashboard observations. Never call trading services from a page refresh."""
from datetime import datetime, timezone
from time import monotonic

_observations: dict = {}


def observe(channel: str, payload: dict) -> None:
    # Existing background work publishes observations; no extra polling or I/O.
    _observations[channel] = (dict(payload), monotonic(), datetime.now(timezone.utc).isoformat())


def read(channel: str) -> dict:
    item = _observations.get(channel)
    if not item:
        return {"data": {}, "age_seconds": None, "observed_at": None}
    payload, tick, stamp = item
    return {"data": payload, "age_seconds": round(monotonic() - tick, 1), "observed_at": stamp}


def build_snapshot(orch, loops: dict) -> dict:
    from services.brokers.kite_auth import kite_auth
    from services.control.reconciliation_state import peek_reconciliation_status

    cfg = orch.cfg
    policy_keys = ("max_position_value_pct", "cash_reserve_pct", "max_risk_per_trade_pct",
                   "min_net_reward_risk", "max_daily_loss_pct", "max_monthly_drawdown_pct",
                   "max_portfolio_heat_pct", "max_open_positions")
    position_keys = ("symbol", "qty", "entry", "stop_loss", "take_profit", "strategy", "risk_pct", "stop_order_id")
    decision_keys = ("symbol", "action", "strategy", "risk_reason", "risk_verdict")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": cfg.trading_mode,
        "portfolio": orch.portfolio.metrics(),
        "positions": [{k: getattr(p, k) for k in position_keys} for p in orch.portfolio.state.positions],
        "sizing_policy": {k: getattr(cfg, k) for k in policy_keys},
        "pnl": read("pnl"),
        "autonomous": read("autonomous"),
        "running": read("running"),
        "reconciliation": peek_reconciliation_status(),
        "loops": loops,
        # A token's presence is not proof of broker connectivity. Never expose it.
        "kite": {"configured": bool(cfg.kite_api_key and cfg.kite_api_secret),
                 "session_saved": bool(kite_auth.get_access_token_sync())},
        "session": f"{cfg.autonomous_session_start}–{cfg.autonomous_session_end} IST",
        "scan_interval_seconds": cfg.autonomous_scan_interval_sec,
        "recent_decisions": [{k: str(d.get(k, ""))[:800] for k in decision_keys} for d in orch.decisions[:5]],
    }
