"""Market regime detection — drives dynamic strategy selection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


class Regime(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    HIGH_VOL = "high_volatility"
    LOW_VOL = "low_volatility"
    CRISIS = "crisis"
    UNCLEAR = "unclear"


@dataclass
class RegimeAnalysis:
    regime: Regime
    confidence: float
    volatility_pct: float
    trend_strength: float
    recommended_strategies: list[str]
    trade_allowed: bool
    explanation: str


class RegimeDetector:
    """Classify market state from OHLCV — no ML required for v1; ML hook ready."""

    STRATEGY_MAP: dict[Regime, list[str]] = {
        Regime.TREND_UP: ["trend_following", "momentum", "breakout", "swing"],
        Regime.TREND_DOWN: ["mean_reversion"],
        Regime.RANGE: ["mean_reversion", "market_making"],
        Regime.HIGH_VOL: ["volatility_expansion", "breakout"],
        Regime.LOW_VOL: ["mean_reversion", "stat_arb"],
        Regime.CRISIS: [],
        Regime.UNCLEAR: [],
    }

    def analyze(self, df: pd.DataFrame) -> RegimeAnalysis:
        if df.empty or len(df) < 50:
            return RegimeAnalysis(
                Regime.UNCLEAR, 0, 0, 0, [], False,
                "Insufficient data for regime classification",
            )

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)

        returns = close.pct_change().dropna()
        vol = float(returns.tail(20).std() * np.sqrt(252) * 100)

        ema20 = close.ewm(span=20).mean()
        ema50 = close.ewm(span=50).mean()
        trend = float((ema20.iloc[-1] - ema50.iloc[-1]) / ema50.iloc[-1] * 100)

        atr = self._atr(high, low, close)
        atr_pct = float(atr.iloc[-1] / close.iloc[-1] * 100)

        if vol > 45 or atr_pct > 5:
            regime = Regime.CRISIS if vol > 60 else Regime.HIGH_VOL
        elif abs(trend) < 0.3 and vol < 15:
            regime = Regime.RANGE
        elif trend > 0.8:
            regime = Regime.TREND_UP
        elif trend < -0.8:
            regime = Regime.TREND_DOWN
        elif vol < 12:
            regime = Regime.LOW_VOL
        else:
            regime = Regime.UNCLEAR

        strategies = self.STRATEGY_MAP.get(regime, [])
        trade_allowed = regime not in (Regime.CRISIS, Regime.UNCLEAR)
        conf = min(95, 50 + abs(trend) * 10 + (20 if vol < 30 else 0))

        return RegimeAnalysis(
            regime=regime,
            confidence=round(conf, 1),
            volatility_pct=round(vol, 2),
            trend_strength=round(trend, 2),
            recommended_strategies=strategies,
            trade_allowed=trade_allowed,
            explanation=(
                f"Regime={regime.value} trend={trend:+.2f}% vol={vol:.1f}% "
                f"ATR={atr_pct:.2f}%"
            ),
        )

    @staticmethod
    def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        prev = close.shift(1)
        tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()
