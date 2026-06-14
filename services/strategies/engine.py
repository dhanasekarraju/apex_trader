"""Pluggable strategy framework — multiple independent strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Signal:
    symbol: str
    strategy: str
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    confidence: float
    qty_suggestion: float
    reasons: list[str]
    timeframe: str = "15m"


class BaseStrategy(ABC):
    name: str = "base"
    asset_classes: list[str] = ["equity"]

    @abstractmethod
    def generate(self, symbol: str, df: pd.DataFrame, regime: str) -> Signal | None:
        ...


class TrendFollowingStrategy(BaseStrategy):
    name = "trend_following"

    def generate(self, symbol: str, df: pd.DataFrame, regime: str) -> Signal | None:
        if regime not in ("trend_up", "high_volatility"):
            return None
        close = df["close"].astype(float)
        if len(close) < 50:
            return None
        ema9 = close.ewm(span=9).mean().iloc[-1]
        ema21 = close.ewm(span=21).mean().iloc[-1]
        price = close.iloc[-1]
        if ema9 <= ema21 or price <= ema9:
            return None
        atr = self._atr(df)
        sl = price - atr * 1.5
        tp = price + atr * 3
        return Signal(
            symbol, self.name, "long", round(price, 4), round(sl, 4), round(tp, 4),
            78, 0, ["EMA9>EMA21", "Trend aligned", f"Regime {regime}"],
        )

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> float:
        h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
        prev = c.shift(1)
        tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
        return float(tr.ewm(span=period).mean().iloc[-1])


class MomentumStrategy(BaseStrategy):
    name = "momentum"

    def generate(self, symbol: str, df: pd.DataFrame, regime: str) -> Signal | None:
        if regime not in ("trend_up", "high_volatility", "low_volatility"):
            return None
        close = df["close"].astype(float)
        if len(close) < 30:
            return None
        roc = (close.iloc[-1] / close.iloc[-10] - 1) * 100
        if roc < 1.5:
            return None
        price = close.iloc[-1]
        sl = price * 0.985
        tp = price * (1 + roc / 100 * 0.6)
        return Signal(
            symbol, self.name, "long", round(price, 4), round(sl, 4), round(tp, 4),
            min(90, 70 + roc), 0,
            [f"ROC10 +{roc:.1f}%", "Momentum burst"],
        )


class MeanReversionStrategy(BaseStrategy):
    name = "mean_reversion"

    def generate(self, symbol: str, df: pd.DataFrame, regime: str) -> Signal | None:
        if regime not in ("range", "trend_down", "low_volatility"):
            return None
        close = df["close"].astype(float)
        if len(close) < 30:
            return None
        mid = close.rolling(20).mean().iloc[-1]
        std = close.rolling(20).std().iloc[-1]
        price = close.iloc[-1]
        z = (price - mid) / std if std > 0 else 0
        if z > -1.5:
            return None
        sl = price * 0.992
        tp = mid
        return Signal(
            symbol, self.name, "long", round(price, 4), round(sl, 4), round(tp, 4),
            min(85, 72 + abs(z) * 5), 0,
            [f"Z-score {z:.2f}", "Mean reversion setup"],
        )


class BreakoutStrategy(BaseStrategy):
    name = "breakout"

    def generate(self, symbol: str, df: pd.DataFrame, regime: str) -> Signal | None:
        if regime not in ("trend_up", "high_volatility"):
            return None
        high = df["high"].astype(float)
        close = df["close"].astype(float)
        vol = df["volume"].astype(float) if "volume" in df.columns else pd.Series([1] * len(df))
        if len(close) < 25:
            return None
        range_high = high.iloc[-21:-1].max()
        price = close.iloc[-1]
        avg_vol = vol.iloc[-21:-1].mean()
        if price <= range_high or vol.iloc[-1] < avg_vol * 1.3:
            return None
        sl = range_high * 0.995
        tp = price + (price - sl) * 2.5
        return Signal(
            symbol, self.name, "long", round(price, 4), round(sl, 4), round(tp, 4),
            80, 0, ["20-bar breakout", "Volume confirmation"],
        )


class VolatilityExpansionStrategy(BaseStrategy):
    name = "volatility_expansion"

    def generate(self, symbol: str, df: pd.DataFrame, regime: str) -> Signal | None:
        if regime != "high_volatility":
            return None
        close = df["close"].astype(float)
        if len(close) < 30:
            return None
        rets = close.pct_change()
        short_v = rets.tail(5).std()
        long_v = rets.tail(20).std()
        if long_v <= 0 or short_v / long_v < 1.4:
            return None
        price = close.iloc[-1]
        sl = price * 0.98
        tp = price * 1.025
        return Signal(
            symbol, self.name, "long", round(price, 4), round(sl, 4), round(tp, 4),
            76, 0, ["Vol expansion", f"Ratio {short_v/long_v:.2f}"],
        )


STRATEGY_REGISTRY: dict[str, BaseStrategy] = {
    s.name: s
    for s in [
        TrendFollowingStrategy(),
        MomentumStrategy(),
        MeanReversionStrategy(),
        BreakoutStrategy(),
        VolatilityExpansionStrategy(),
    ]
}


class StrategyEngine:
    """Run all regime-appropriate strategies and rank signals."""

    def scan(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime: str,
        allowed: list[str] | None = None,
    ) -> list[Signal]:
        out: list[Signal] = []
        for name, strat in STRATEGY_REGISTRY.items():
            if allowed and name not in allowed:
                continue
            sig = strat.generate(symbol, df, regime)
            if sig:
                out.append(sig)
        out.sort(key=lambda s: s.confidence, reverse=True)
        return out
