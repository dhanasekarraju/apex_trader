"""Market data layer — synthetic for paper/backtest; Kite for live/shadow."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from functools import partial

import numpy as np
import pandas as pd

from services.brokers.kite_auth import kite_auth
from shared.config import get_settings
from shared.logging import audit


class RealDataRequired(Exception):
    """Raised when live/shadow trading requires real feeds."""


class MarketDataService:
    def __init__(self) -> None:
        self.cfg = get_settings()
        self._real_data_ok: bool | None = None
        self._instrument_cache: dict[str, int] | None = None

    def has_real_data_configured(self) -> bool:
        return (
            self.cfg.market_data_source == "kite"
            and bool(self.cfg.kite_api_key and kite_auth.get_access_token_sync())
        )

    async def verify_real_data(self) -> bool:
        if not self.has_real_data_configured():
            self._real_data_ok = False
            return False
        try:
            df = await self._fetch_kite_ohlcv("RELIANCE", bars=20)
            self._real_data_ok = df is not None and not df.empty
        except Exception as e:
            audit("real_data_verify_failed", error=str(e))
            self._real_data_ok = False
        return bool(self._real_data_ok)

    async def get_trading_ohlcv(
        self, symbol: str, *, mode: str, bars: int = 500
    ) -> tuple[pd.DataFrame, str]:
        if mode in ("live", "shadow"):
            if not self.has_real_data_configured():
                raise RealDataRequired("Real market data not configured (MARKET_DATA_SOURCE=kite)")
            df = await self._fetch_kite_ohlcv(symbol.upper(), bars=bars)
            if df is None or df.empty:
                raise RealDataRequired(f"Unable to fetch real OHLCV for {symbol}")
            return df, "kite"
        return self.synthetic_ohlcv(symbol, bars=bars), "synthetic"

    def synthetic_ohlcv(self, symbol: str, bars: int = 500, seed: int = 42) -> pd.DataFrame:
        rng = np.random.default_rng(seed + hash(symbol) % 1000)
        price = 100.0
        rows = []
        for i in range(bars):
            ret = rng.normal(0.0003, 0.012)
            price *= 1 + ret
            h = price * (1 + abs(rng.normal(0, 0.004)))
            l = price * (1 - abs(rng.normal(0, 0.004)))
            o = price * (1 + rng.normal(0, 0.002))
            vol = int(rng.integers(100_000, 2_000_000))
            rows.append({"open": o, "high": h, "low": l, "close": price, "volume": vol})
        df = pd.DataFrame(rows)
        df.index = pd.date_range(end=pd.Timestamp.now(), periods=bars, freq="15min")
        return df

    async def _fetch_kite_ohlcv(self, symbol: str, bars: int = 500) -> pd.DataFrame | None:
        if not self.has_real_data_configured():
            return None
        try:
            from kiteconnect import KiteConnect
        except ImportError:
            audit("kiteconnect_missing")
            return None

        kite = KiteConnect(api_key=self.cfg.kite_api_key)
        kite.set_access_token(kite_auth.get_access_token_sync())
        token = await self._resolve_instrument_token(kite, symbol)
        if token is None:
            return None

        to_date = datetime.now()
        from_date = to_date - timedelta(days=max(30, bars // 26))
        loop = asyncio.get_event_loop()
        candles = await loop.run_in_executor(
            None,
            partial(
                kite.historical_data,
                token,
                from_date.strftime("%Y-%m-%d"),
                to_date.strftime("%Y-%m-%d"),
                "15minute",
            ),
        )
        if not candles:
            return None
        df = pd.DataFrame(candles)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").rename(columns={"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"})
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        return df.tail(bars)

    async def _resolve_instrument_token(self, kite, symbol: str) -> int | None:
        if self._instrument_cache is None:
            loop = asyncio.get_event_loop()
            instruments = await loop.run_in_executor(None, partial(kite.instruments, "NSE"))
            self._instrument_cache = {
                i["tradingsymbol"]: i["instrument_token"] for i in instruments
            }
        return self._instrument_cache.get(symbol.upper())

    def volume_profile(self, df: pd.DataFrame, bins: int = 20) -> dict:
        if df.empty:
            return {"poc": 0, "bins": []}
        prices = df["close"].astype(float)
        vols = df["volume"].astype(float) if "volume" in df.columns else pd.Series([1] * len(df))
        hist, edges = np.histogram(prices, bins=bins, weights=vols)
        poc_idx = int(hist.argmax())
        poc = float((edges[poc_idx] + edges[poc_idx + 1]) / 2)
        return {"poc": round(poc, 4), "bins": len(hist)}
