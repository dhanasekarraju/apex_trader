"""Backtesting framework — walk-forward ready, slippage + commission."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from services.regime.detector import RegimeDetector
from services.strategies.engine import StrategyEngine


@dataclass
class BacktestResult:
    strategy: str
    symbol: str
    total_trades: int
    win_rate: float
    net_return_pct: float
    sharpe: float
    sortino: float
    max_drawdown: float
    passed_stress: bool
    trades: list[dict]


class BacktestEngine:
    COMMISSION_PCT = 0.03
    SLIPPAGE_BPS = 2.0

    def __init__(self) -> None:
        self.regime = RegimeDetector()
        self.strategies = StrategyEngine()

    def run(
        self,
        symbol: str,
        df: pd.DataFrame,
        strategy_name: str | None = None,
        initial_capital: float = 1_000_000,
    ) -> BacktestResult:
        trades: list[dict] = []
        equity = initial_capital
        peak = initial_capital
        max_dd = 0.0
        returns: list[float] = []

        window = 60
        for i in range(window, len(df) - 1, 5):
            slice_df = df.iloc[: i + 1].copy()
            regime = self.regime.analyze(slice_df)
            if not regime.trade_allowed:
                continue

            allowed = [strategy_name] if strategy_name else regime.recommended_strategies
            signals = self.strategies.scan(
                symbol, slice_df, regime.regime.value, allowed=allowed or None
            )
            if not signals:
                continue

            sig = signals[0]
            entry = sig.entry * (1 + self.SLIPPAGE_BPS / 10000)
            exit_px = float(df["close"].iloc[min(i + 5, len(df) - 1)])

            if exit_px <= sig.stop_loss:
                exit_px = sig.stop_loss
                win = False
            elif exit_px >= sig.take_profit:
                exit_px = sig.take_profit
                win = True
            else:
                win = exit_px > entry

            pnl_pct = (exit_px - entry) / entry * 100 - self.COMMISSION_PCT * 2
            equity *= 1 + pnl_pct / 100
            peak = max(peak, equity)
            dd = (peak - equity) / peak * 100
            max_dd = max(max_dd, dd)
            returns.append(pnl_pct)

            trades.append({
                "entry": entry,
                "exit": exit_px,
                "pnl_pct": round(pnl_pct, 3),
                "win": win,
                "strategy": sig.strategy,
            })

        wins = sum(1 for t in trades if t["win"])
        total = len(trades)
        win_rate = wins / total * 100 if total else 0
        net_ret = (equity / initial_capital - 1) * 100

        ret_s = pd.Series(returns)
        sharpe = self._sharpe(ret_s)
        sortino = self._sortino(ret_s)
        passed = max_dd < 8 and win_rate >= 45 and net_ret > 0

        return BacktestResult(
            strategy=strategy_name or "dynamic",
            symbol=symbol,
            total_trades=total,
            win_rate=round(win_rate, 2),
            net_return_pct=round(net_ret, 2),
            sharpe=round(sharpe, 2),
            sortino=round(sortino, 2),
            max_drawdown=round(max_dd, 2),
            passed_stress=passed,
            trades=trades[-20:],
        )

    @staticmethod
    def _sharpe(rets: pd.Series) -> float:
        if rets.empty or rets.std() == 0:
            return 0.0
        return float(rets.mean() / rets.std() * np.sqrt(252))

    @staticmethod
    def _sortino(rets: pd.Series) -> float:
        downside = rets[rets < 0]
        if downside.empty or downside.std() == 0:
            return 0.0
        return float(rets.mean() / downside.std() * np.sqrt(252))
