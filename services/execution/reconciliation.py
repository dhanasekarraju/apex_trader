"""Broker ↔ Postgres reconciliation — broker is source of truth."""

from __future__ import annotations

from services.brokers.base import BrokerAdapter
from services.control.reconciliation_state import (
    clear_reconciliation_degraded,
    set_reconciliation_degraded,
)
from services.portfolio.manager import PortfolioManager
from services.portfolio.models import PositionView
from services.trades.repository import TradeRepository
from shared.logging import audit


async def reconcile_on_startup(
    *,
    broker: BrokerAdapter,
    portfolio: PortfolioManager,
    trades: TradeRepository,
    trading_mode: str,
) -> dict:
    """
    1. Load open trades from Postgres (idempotency seed)
    2. Fetch broker open positions (source of truth)
    3. Repair portfolio mismatches — NEVER delete positions if broker fetch fails
    """
    open_rows = await trades.open_trades()
    broker_positions: list[dict] = []
    broker_fetch_ok = True

    if trading_mode != "shadow":
        try:
            broker_positions = await broker.fetch_open_positions()
        except Exception as e:
            broker_fetch_ok = False
            audit("broker_positions_fetch_failed", error=str(e))
            await set_reconciliation_degraded(str(e))
            return {
                "reconciliation_status": "DEGRADED",
                "open_trades_db": len(open_rows),
                "broker_positions": 0,
                "positions_added": 0,
                "positions_removed": 0,
                "positions_updated": 0,
                "reason": str(e),
            }

    await clear_reconciliation_degraded()

    db_by_symbol = {p.symbol: p for p in portfolio.state.positions}
    broker_by_symbol = {
        p["symbol"]: p for p in broker_positions if p.get("qty", 0) > 0
    }

    added = 0
    removed = 0
    updated = 0

    for symbol, bpos in broker_by_symbol.items():
        qty = float(bpos.get("qty", 0))
        entry = float(bpos.get("entry", 0) or bpos.get("avg_price", 0))
        if symbol in db_by_symbol:
            db_pos = db_by_symbol[symbol]
            if abs(db_pos.qty - qty) > 0.001:
                db_pos.qty = qty
                db_pos.entry = entry or db_pos.entry
                updated += 1
                audit("reconcile_qty_updated", symbol=symbol, qty=qty)
        else:
            trade_row = next((r for r in open_rows if r.symbol == symbol), None)
            portfolio.state.positions.append(
                PositionView(
                    symbol=symbol,
                    qty=qty,
                    entry=entry,
                    stop_loss=float(trade_row.stop_loss or 0) if trade_row else 0,
                    take_profit=float(trade_row.take_profit or 0) if trade_row else 0,
                    strategy=trade_row.strategy if trade_row else "recovered",
                    unrealized_pnl=0,
                    risk_pct=0,
                    broker_order_id=trade_row.broker_order_id or "" if trade_row else "",
                    stop_order_id=trade_row.stop_order_id or "" if trade_row else "",
                )
            )
            added += 1
            audit("reconcile_position_added", symbol=symbol, qty=qty)

    if broker_fetch_ok:
        for symbol in list(db_by_symbol.keys()):
            if symbol not in broker_by_symbol and trading_mode in ("live", "paper"):
                portfolio.state.positions = [
                    p for p in portfolio.state.positions if p.symbol != symbol
                ]
                removed += 1
                audit("reconcile_position_removed", symbol=symbol)

    if added or removed or updated:
        await portfolio.persist()

    return {
        "reconciliation_status": "OK",
        "open_trades_db": len(open_rows),
        "broker_positions": len(broker_by_symbol),
        "positions_added": added,
        "positions_removed": removed,
        "positions_updated": updated,
    }
