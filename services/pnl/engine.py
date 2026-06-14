"""Live PnL engine — broker positions are primary truth."""

from __future__ import annotations

from dataclasses import dataclass

from services.brokers.factory import get_broker
from services.market_data.service import MarketDataService
from services.portfolio.manager import PortfolioManager
from shared.config import get_settings
from shared.logging import audit


@dataclass
class PositionPnL:
    symbol: str
    qty: float
    side: str
    avg_price: float
    ltp: float
    unrealized_pnl: float
    realized_pnl: float
    total_pnl: float
    strategy: str = ""
    source: str = "broker"


class LivePnLEngine:
    """Compute live PnL from broker positions, validated against internal state."""

    def __init__(
        self,
        *,
        portfolio: PortfolioManager,
        market_data: MarketDataService | None = None,
    ) -> None:
        self.portfolio = portfolio
        self.market_data = market_data or MarketDataService()
        self.cfg = get_settings()

    async def compute(self) -> dict:
        broker = get_broker()
        broker_positions: list[dict] = []
        if self.cfg.trading_mode != "shadow":
            try:
                if await broker.connect():
                    broker_positions = await broker.fetch_open_positions()
            except Exception as e:
                audit("pnl_broker_fetch_failed", error=str(e))

        if not broker_positions:
            broker_positions = self._positions_from_portfolio()

        symbols = [p["symbol"] for p in broker_positions if p.get("symbol")]
        ltps = await self._fetch_ltps(symbols)

        positions: list[dict] = []
        total_unrealized = 0.0
        total_exposure_rs = 0.0

        internal_by_symbol = {p.symbol: p for p in self.portfolio.state.positions}

        for bpos in broker_positions:
            symbol = bpos.get("symbol", "")
            if not symbol:
                continue
            qty = float(bpos.get("qty", 0))
            if qty <= 0:
                continue
            side = bpos.get("side", "long")
            avg_price = float(bpos.get("entry") or bpos.get("avg_price") or 0)
            ltp = ltps.get(symbol) or avg_price
            if side == "short":
                unrealized = (avg_price - ltp) * qty
            else:
                unrealized = (ltp - avg_price) * qty

            internal = internal_by_symbol.get(symbol)
            strategy = internal.strategy if internal else bpos.get("strategy", "")
            source = "broker" if bpos.get("from_broker", True) else "internal"

            if internal and abs(internal.qty - qty) > 0.01:
                audit(
                    "pnl_qty_mismatch",
                    symbol=symbol,
                    broker_qty=qty,
                    internal_qty=internal.qty,
                )

            positions.append(
                PositionPnL(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    avg_price=round(avg_price, 4),
                    ltp=round(ltp, 4),
                    unrealized_pnl=round(unrealized, 2),
                    realized_pnl=0.0,
                    total_pnl=round(unrealized, 2),
                    strategy=strategy,
                    source=source,
                ).__dict__
            )
            total_unrealized += unrealized
            total_exposure_rs += ltp * qty

        equity = max(self.portfolio.state.equity, 1.0)
        open_exposure = total_exposure_rs / equity * 100
        daily_pnl = self.portfolio.state.daily_pnl + total_unrealized
        portfolio_pnl = total_unrealized

        return {
            "portfolio_pnl": round(portfolio_pnl, 2),
            "daily_pnl": round(daily_pnl, 2),
            "unrealized_pnl": round(total_unrealized, 2),
            "realized_pnl": round(self.portfolio.state.daily_pnl, 2),
            "positions": positions,
            "open_exposure": round(open_exposure, 2),
            "margin_used": round(min(open_exposure * 0.25, 100.0), 2),
            "equity": round(equity, 2),
            "position_count": len(positions),
            "source": "broker" if broker_positions else "internal",
            "updated_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
        }

    def _positions_from_portfolio(self) -> list[dict]:
        return [
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "entry": p.entry,
                "side": "long",
                "strategy": p.strategy,
                "from_broker": False,
            }
            for p in self.portfolio.state.positions
        ]

    async def _fetch_ltps(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        if self.market_data.has_real_data_configured():
            return await self._kite_ltps(symbols)
        out: dict[str, float] = {}
        for sym in symbols:
            df = self.market_data.synthetic_ohlcv(sym, bars=5)
            out[sym] = float(df["close"].iloc[-1])
        return out

    async def _kite_ltps(self, symbols: list[str]) -> dict[str, float]:
        token = self.market_data.cfg.kite_api_key
        access = __import__(
            "services.brokers.kite_auth", fromlist=["kite_auth"]
        ).kite_auth.get_access_token_sync()
        if not token or not access:
            return {}
        try:
            from kiteconnect import KiteConnect
            import asyncio
            from functools import partial

            kite = KiteConnect(api_key=token)
            kite.set_access_token(access)
            keys = [f"NSE:{s}" for s in symbols]
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, partial(kite.ltp, keys))
            out: dict[str, float] = {}
            for sym in symbols:
                key = f"NSE:{sym}"
                if key in resp:
                    out[sym] = float(resp[key].get("last_price", 0))
            return out
        except Exception as e:
            audit("pnl_ltp_fetch_failed", error=str(e))
            return {}
