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

        from services.sizing.engine import PositionSizingEngine, SizeInput
        from shared.config import get_settings
        cfg = get_settings()
        sizing = PositionSizingEngine()
        next_entry = 60
        daily_returns = {}
        for i in range(60, len(df) - 1):
            if i < next_entry:
                continue
            regime = self.regime.analyze(df.iloc[:i + 1])
            if not regime.trade_allowed:
                continue
            allowed = [strategy_name] if strategy_name else regime.recommended_strategies
            signals = self.strategies.scan(symbol, df.iloc[:i + 1], regime.regime.value, allowed=allowed or None)
            if not signals:
                continue
            sig = signals[0]
            # Signals use a completed candle; enter on the next bar, with adverse slip.
            entry = float(df["open"].iloc[i + 1]) * (1 + self.SLIPPAGE_BPS / 10000)
            if not 0 < sig.stop_loss < entry < sig.take_profit:
                continue
            cost = entry * (cfg.estimated_round_trip_cost_bps + cfg.estimated_exit_slippage_bps) / 10000
            if (sig.take_profit - entry - cost) / (entry - sig.stop_loss + cost) < cfg.min_net_reward_risk:
                continue
            sized = sizing.compute(SizeInput(equity=equity, cash=equity, entry=entry, stop_loss=sig.stop_loss))
            if sized.qty <= 0:
                continue
            end = min(i + 5, len(df) - 1)
            exit_px = float(df['close'].iloc[end])
            reason = 'time_exit'
            for j in range(i + 1, end + 1):
                bar = df.iloc[j]
                if float(bar['open']) <= sig.stop_loss:
                    exit_px, end, reason = float(bar['open']), j, 'gap_stop'
                    break
                if float(bar['low']) <= sig.stop_loss:
                    # Stop first when both boundaries occur in one candle.
                    exit_px, end, reason = sig.stop_loss, j, 'stop'
                    break
                if float(bar['high']) >= sig.take_profit:
                    exit_px, end, reason = sig.take_profit, j, 'target'
                    break
            exit_px *= 1 - cfg.estimated_exit_slippage_bps / 10000
            costs = entry * sized.qty * cfg.estimated_round_trip_cost_bps / 10000
            pnl = (exit_px - entry) * sized.qty - costs
            pnl_pct = pnl / equity * 100
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak * 100)
            key = str(df.index[end])[:10]
            daily_returns[key] = daily_returns.get(key, 0) + pnl_pct
            trades.append(dict(entry=entry, exit=exit_px, qty=sized.qty, pnl=pnl,
                               pnl_pct=pnl_pct, win=pnl > 0, strategy=sig.strategy, exit_reason=reason))
            next_entry = end + 1
        returns = list(daily_returns.values())

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
            trades=trades,
        )

    @staticmethod
    def _sharpe(rets: pd.Series) -> float:
        if len(rets) < 2 or rets.std() == 0:
            return 0.0
        return float(rets.mean() / rets.std() * np.sqrt(252))

    @staticmethod
    def _sortino(rets: pd.Series) -> float:
        downside = rets[rets < 0]
        if len(downside) < 2 or downside.std() == 0:
            return 0.0
        return float(rets.mean() / downside.std() * np.sqrt(252))
