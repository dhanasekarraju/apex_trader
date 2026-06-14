"""Pre-trade validation — institutional caution before any buy/sell."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from services.market_data.service import MarketDataService
from services.portfolio.manager import PortfolioManager
from shared.config import Settings, get_settings
from shared.logging import audit

_IST = ZoneInfo("Asia/Kolkata")


class PreTradeValidator:
    """Hard gates before order submission — market hours, qty, duplicates, price sanity."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.cfg = settings or get_settings()

    def normalize_equity_qty(self, qty: float) -> int:
        """NSE equity — whole shares only."""
        return max(0, int(qty))

    def has_open_position(self, portfolio: PortfolioManager, symbol: str) -> bool:
        sym = symbol.upper()
        return any(p.symbol.upper() == sym for p in portfolio.state.positions)

    def is_market_open(self, *, now: datetime | None = None) -> tuple[bool, str]:
        if not self.cfg.enforce_market_hours:
            return True, "Market hours check disabled"
        ts = now or datetime.now(_IST)
        if ts.weekday() >= 5:
            return False, "Market closed — weekend"
        session_open = time(9, 15)
        session_close = time(15, 30)
        t = ts.time()
        if session_open <= t <= session_close:
            return True, "NSE regular session open"
        return False, f"Market closed — NSE hours 09:15–15:30 IST (now {t.strftime('%H:%M')})"

    async def validate_buy(
        self,
        *,
        symbol: str,
        qty: float,
        entry: float,
        stop_loss: float,
        portfolio: PortfolioManager,
        market_data: MarketDataService,
        trading_mode: str,
    ) -> tuple[bool, str, int]:
        sym = symbol.upper()
        approved_qty = self.normalize_equity_qty(qty)
        if approved_qty < 1:
            return False, "Quantity below 1 share after lot rounding", 0

        if stop_loss <= 0 or stop_loss >= entry:
            return False, "Stop-loss must be below entry", 0

        if self.has_open_position(portfolio, sym):
            return False, f"Duplicate blocked — already holding {sym}", 0

        if trading_mode in ("live", "paper") and self.cfg.enforce_market_hours:
            open_ok, msg = self.is_market_open()
            if not open_ok:
                return False, msg, 0

        ltp = await self._reference_price(sym, entry, market_data)
        if ltp > 0 and entry > 0:
            deviation = abs(ltp - entry) / entry * 100
            if deviation > self.cfg.max_entry_deviation_pct:
                audit(
                    "pre_trade_price_deviation",
                    symbol=sym,
                    entry=entry,
                    ltp=ltp,
                    deviation=deviation,
                )
                return (
                    False,
                    f"Entry {entry:.2f} deviates {deviation:.1f}% from LTP {ltp:.2f} "
                    f"(max {self.cfg.max_entry_deviation_pct}%)",
                    0,
                )

        return True, "Pre-trade checks passed", approved_qty

    async def validate_sell(
        self,
        *,
        symbol: str,
        qty: float,
        portfolio: PortfolioManager,
    ) -> tuple[bool, str, int]:
        sym = symbol.upper()
        pos = next((p for p in portfolio.state.positions if p.symbol.upper() == sym), None)
        if pos is None:
            return False, f"No open position for {sym}", 0
        sell_qty = min(self.normalize_equity_qty(qty), self.normalize_equity_qty(pos.qty))
        if sell_qty < 1:
            return False, "Sell quantity invalid", 0
        return True, "Sell validation passed", sell_qty

    async def _reference_price(
        self,
        symbol: str,
        fallback: float,
        market_data: MarketDataService,
    ) -> float:
        if market_data.has_real_data_configured():
            ltps = await market_data.fetch_ltps([symbol])
            if symbol in ltps and ltps[symbol] > 0:
                return ltps[symbol]
        try:
            df = market_data.synthetic_ohlcv(symbol, bars=5)
            return float(df["close"].iloc[-1])
        except Exception:
            return fallback
