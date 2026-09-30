"""Fail-closed reconciliation; an absent position is not an execution receipt."""
from __future__ import annotations
from services.control.reconciliation_state import clear_reconciliation_degraded, set_reconciliation_degraded
from shared.logging import audit

async def reconcile_on_startup(*, broker, portfolio, trades, trading_mode: str) -> dict:
    rows = await trades.open_trades()
    report = dict(open_trades_db=len(rows), positions_added=0, positions_removed=0, positions_updated=0, orphaned_closed=0)
    if trading_mode in ("paper", "shadow"):
        if hasattr(broker, "_open_positions"):
            broker._open_positions = [dict(symbol=p.symbol, qty=p.qty, entry=p.entry) for p in portfolio.state.positions]
        return {**report, "reconciliation_status": "OK", "broker_positions": len(portfolio.state.positions)}
    try:
        positions = await broker.fetch_open_positions()
        report["broker_positions"] = len(positions)
        issues = []
        if any(r.status in ("pending", "submitted", "unknown") for r in rows):
            issues.append("Unresolved orders require broker order-history review; no automatic resubmission")
        internal = {p.symbol: p for p in portfolio.state.positions}
        seen = set()
        for b in positions:
            symbol = b["symbol"]
            if symbol in seen or b.get("side", "long") != "long":
                issues.append(f"Unsupported or ambiguous broker exposure: {symbol}")
            seen.add(symbol)
            p = internal.get(symbol)
            if p is None or abs(p.qty - float(b["qty"])) > 0.0001:
                issues.append(f"Broker quantity mismatch: {symbol}; retain ledger for investigation")
            elif not p.stop_order_id:
                issues.append(f"Missing protective stop: {symbol}")
            else:
                stop = await broker.fetch_order_status(p.stop_order_id)
                if stop.get("status") not in ("OPEN", "TRIGGER PENDING", "COMPLETE"):
                    issues.append(f"Stop not confirmed: {symbol}")
        for symbol in internal.keys() - seen:
            issues.append(f"Position absent at broker: {symbol}; confirm exit executions before accounting")
        if issues:
            raise RuntimeError("; ".join(issues))
        await clear_reconciliation_degraded()
        return {**report, "reconciliation_status": "OK"}
    except Exception as exc:
        await set_reconciliation_degraded(str(exc))
        audit("reconciliation_degraded", reason=str(exc))
        return {**report, "broker_positions": report.get("broker_positions", 0), "reconciliation_status": "DEGRADED", "reason": str(exc)}
