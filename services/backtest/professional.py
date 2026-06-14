"""Professional backtesting — walk-forward, Monte Carlo, validation gate."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from services.backtest.engine import BacktestEngine
from services.regime.detector import RegimeDetector
from services.strategies.engine import StrategyEngine
from shared.config import get_settings


@dataclass
class ProBacktestResult:
    strategy: str
    symbol: str
    total_trades: int
    win_rate: float
    net_return_pct: float
    sharpe: float
    sortino: float
    calmar: float
    profit_factor: float
    max_drawdown: float
    expectancy: float
    passed_validation: bool
    walk_forward_passed: bool
    monte_carlo_passed: bool
    rejection_reasons: list[str]


class ProfessionalBacktester:
    COMMISSION_PCT = 0.03
    SLIPPAGE_BPS = 3.0

    def __init__(self) -> None:
        self.base = BacktestEngine()
        self.regime = RegimeDetector()
        self.strategies = StrategyEngine()
        self.cfg = get_settings()

    def run_full_validation(
        self,
        symbol: str,
        df: pd.DataFrame,
        strategy: str | None = None,
    ) -> ProBacktestResult:
        base = self.base.run(symbol, df, strategy_name=strategy)
        reasons: list[str] = []

        gross_profit = sum(t["pnl_pct"] for t in base.trades if t["pnl_pct"] > 0)
        gross_loss = abs(sum(t["pnl_pct"] for t in base.trades if t["pnl_pct"] < 0))
        pf = gross_profit / gross_loss if gross_loss else 0

        wins = [t["pnl_pct"] for t in base.trades if t["win"]]
        losses = [t["pnl_pct"] for t in base.trades if not t["win"]]
        expectancy = (
            (len(wins) / len(base.trades) * np.mean(wins) if wins else 0)
            - (len(losses) / len(base.trades) * abs(np.mean(losses)) if losses else 0)
        ) if base.trades else 0

        calmar = base.net_return_pct / base.max_drawdown if base.max_drawdown else 0

        wf_pass = self._walk_forward(symbol, df, strategy)
        mc_pass = self._monte_carlo(base.trades)

        if base.sharpe < self.cfg.golive_min_sharpe:
            reasons.append(f"Sharpe {base.sharpe} < {self.cfg.golive_min_sharpe}")
        if base.win_rate < self.cfg.golive_min_win_rate:
            reasons.append(f"Win rate {base.win_rate}% too low")
        if base.max_drawdown > self.cfg.golive_max_drawdown:
            reasons.append(f"Drawdown {base.max_drawdown}% too high")
        if pf < self.cfg.golive_min_profit_factor:
            reasons.append(f"Profit factor {pf:.2f} too low")
        if not wf_pass:
            reasons.append("Walk-forward failed")
        if not mc_pass:
            reasons.append("Monte Carlo stress failed")

        passed = len(reasons) == 0

        return ProBacktestResult(
            strategy=base.strategy,
            symbol=symbol,
            total_trades=base.total_trades,
            win_rate=base.win_rate,
            net_return_pct=base.net_return_pct,
            sharpe=base.sharpe,
            sortino=base.sortino,
            calmar=round(calmar, 2),
            profit_factor=round(pf, 2),
            max_drawdown=base.max_drawdown,
            expectancy=round(float(expectancy), 4),
            passed_validation=bool(passed),
            walk_forward_passed=bool(wf_pass),
            monte_carlo_passed=bool(mc_pass),
            rejection_reasons=reasons,
        )

    def _walk_forward(self, symbol: str, df: pd.DataFrame, strategy: str | None) -> bool:
        if len(df) < 200:
            return False
        mid = len(df) // 2
        in_sample = self.base.run(symbol, df.iloc[:mid], strategy)
        out_sample = self.base.run(symbol, df.iloc[mid:], strategy)
        return bool(in_sample.net_return_pct > 0 and out_sample.max_drawdown < 12)

    def _monte_carlo(self, trades: list[dict], sims: int = 200) -> bool:
        if len(trades) < 5:
            return False
        pnls = [t["pnl_pct"] for t in trades]
        max_dds = []
        for _ in range(sims):
            shuffled = np.random.choice(pnls, size=len(pnls), replace=True)
            equity = 100.0
            peak = 100.0
            max_dd = 0.0
            for p in shuffled:
                equity *= 1 + p / 100
                peak = max(peak, equity)
                max_dd = max(max_dd, (peak - equity) / peak * 100)
            max_dds.append(max_dd)
        return bool(float(np.percentile(max_dds, 95)) < 15)
