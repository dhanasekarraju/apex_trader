"""Position lifecycle — detect exits and close the loop on PnL/state."""

from __future__ import annotations

from services.brokers.base import OrderRequest, OrderStatus, OrderType
from services.brokers.factory import get_broker
from services.execution.execution_engine import ExecutionEngine
from services.journal.service import TradeJournal
from services.market_data.service import MarketDataService
from services.portfolio.manager import PortfolioManager
from services.trades.repository import TradeRepository
from shared.config import get_settings
from shared.logging import audit, trade_log


class PositionLifecycleService:
    """
    Monitors open positions for SL/TP fills and broker drift.
    Broker is source of truth; internal state is updated on confirmed exit.
    """

    def __init__(
        self,
        *,
        portfolio: PortfolioManager,
        execution: ExecutionEngine,
        market_data: MarketDataService,
        journal: TradeJournal | None = None,
    ) -> None:
        self.portfolio = portfolio
        self.execution = execution
        self.market_data = market_data
        self.journal = journal
        self.trades = TradeRepository()
        self.cfg = get_settings()

    async def tick(self) -> dict:
        from services.icb.actions import ICBAction
        from services.icb.engine import icb

        icb_result = await icb.authorize(
            ICBAction.LIFECYCLE_TICK,
            {
                "portfolio": self.portfolio,
                "trading_mode": self.cfg.trading_mode,
            },
        )
        if not icb_result.allowed and not self.portfolio.state.positions:
            return {"checked": 0, "exits": 0, "skipped": icb_result.reason}

        positions = list(self.portfolio.state.positions)
        if not positions:
            return {"checked": 0, "exits": 0}

        mode = self.cfg.trading_mode
        broker = get_broker()
        if mode != "shadow":
            await broker.connect()

        exits = 0
        checked = 0

        for pos in positions:
            checked += 1
            async with self.execution.order_lock:
                if pos not in self.portfolio.state.positions:
                    continue
                closed = await self._check_position(pos, broker, mode)
            if closed:
                exits += 1

        await self._sync_missing_broker_positions(broker, mode)
        return {"checked": checked, "exits": exits}

    async def _check_position(self, pos, broker, mode: str) -> bool:
        symbol = pos.symbol.upper()

        if pos.stop_order_id and mode == "live" and hasattr(broker, "fetch_order_status"):
            status = await broker.fetch_order_status(pos.stop_order_id)
            if status.get("status") == "COMPLETE":
                exit_price = float(status.get("average_price") or 0)
                await self._close_position(
                    symbol=symbol,
                    exit_price=exit_price,
                    exit_reason="stop_loss",
                    qty=min(pos.qty, float(status.get("filled_quantity") or 0)),
                )
                return True

        ltps = await self._ltps([symbol])
        ltp = ltps.get(symbol, 0.0)
        if ltp <= 0:
            return False

        if pos.stop_loss > 0 and ltp <= pos.stop_loss and mode in ("paper", "shadow"):
            result = await self.execution.place_exit(
                symbol=symbol,
                qty=pos.qty,
                reason="stop_loss",
                market_price=ltp,
            )
            if result.status.value in ("filled", "partial"):
                await self._close_position(
                    symbol=symbol,
                    exit_price=result.avg_price,
                    exit_reason="stop_loss",
                    qty=result.filled_qty,
                )
                return True
            return False

        if pos.take_profit > 0 and ltp >= pos.take_profit:
            result = await self.execution.place_exit(
                symbol=symbol,
                qty=pos.qty,
                reason="take_profit",
                market_price=ltp,
            )
            if result.status.value in ("filled", "partial"):
                await self._close_position(
                    symbol=symbol,
                    exit_price=result.avg_price,
                    exit_reason="take_profit",
                    qty=result.filled_qty,
                )
                return True
            return False

        return False

    async def _sync_missing_broker_positions(self, broker, mode: str) -> None:
        if mode not in ("live", "paper"):
            return
        from services.control.reconciliation_state import set_reconciliation_degraded
        try:
            broker_positions = await broker.fetch_open_positions()
        except Exception as exc:
            await set_reconciliation_degraded(str(exc))
            return
        broker_symbols = {p["symbol"].upper() for p in broker_positions if p.get("qty", 0) > 0}
        for pos in self.portfolio.state.positions:
            if pos.symbol.upper() not in broker_symbols and mode == "live":
                await set_reconciliation_degraded(f"Missing broker position {pos.symbol}; exit fill reconciliation required")

    async def _broker_has_symbol(self, broker, symbol: str) -> bool:
        try:
            positions = await broker.fetch_open_positions()
        except Exception:
            return True
        return any(p.get("symbol", "").upper() == symbol.upper() for p in positions)

    async def _ltps(self, symbols: list[str]) -> dict[str, float]:
        if self.market_data.has_real_data_configured():
            return await self.market_data.fetch_ltps(symbols)
        if get_settings().trading_mode in ("live", "shadow"):
            return {}
        out: dict[str, float] = {}
        for sym in symbols:
            df = self.market_data.synthetic_ohlcv(sym, bars=5)
            out[sym] = float(df["close"].iloc[-1])
        return out

    async def _close_position(
        self,
        *,
        symbol: str,
        exit_price: float,
        exit_reason: str,
        qty: float,
    ) -> None:
        pos = next((p for p in self.portfolio.state.positions if p.symbol.upper() == symbol.upper()), None)
        if pos is None:
            return

        if qty <= 0 or exit_price <= 0:
            return
        qty = min(qty, pos.qty)
        pnl = (exit_price - pos.entry) * qty
        pnl -= pos.entry * qty * get_settings().estimated_round_trip_cost_bps / 10000
        await self.portfolio.record_exit(
            symbol=symbol,
            exit_price=exit_price,
            exit_reason=exit_reason,
            pnl=pnl,
            qty=qty,
        )

        open_trade = await self.trades.get_open_by_symbol(symbol)
        if open_trade and pos.qty <= 0:
            await self.trades.update_status(
                open_trade.client_order_id,
                status="closed",
                exit_price=exit_price,
                exit_reason=exit_reason,
                message=f"Closed: {exit_reason}",
            )

        if self.journal and pos.qty <= 0:
            outcome = "win" if pnl >= 0 else "loss"
            self.journal.close_trade(symbol, exit_reason, pnl, outcome)

        trade_log(
            symbol=symbol,
            strategy=pos.strategy,
            action="EXIT",
            result=exit_reason,
            exit_price=exit_price,
            pnl=pnl,
        )
        from services.risk.unified import UnifiedRiskEngine

        if pos.qty <= 0:
            await UnifiedRiskEngine().record_strategy_outcome(pos.strategy, pnl)
        audit("position_closed", symbol=symbol, reason=exit_reason, pnl=pnl)
