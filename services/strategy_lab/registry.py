"""Strategy lab — registration, ranking, auto-disable."""

from __future__ import annotations

from dataclasses import dataclass, field

from services.strategies.engine import STRATEGY_REGISTRY, BaseStrategy, StrategyEngine
from shared.config import get_settings


@dataclass
class StrategyPerformance:
    name: str
    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    enabled: bool = True
    disabled_reason: str = ""


class StrategyLab:
    """Plug-and-play strategy management with performance tracking."""

    def __init__(self) -> None:
        self.cfg = get_settings()
        self.engine = StrategyEngine()
        self.performance: dict[str, StrategyPerformance] = {
            name: StrategyPerformance(name=name)
            for name in STRATEGY_REGISTRY
        }

    def register(self, strategy: BaseStrategy) -> None:
        STRATEGY_REGISTRY[strategy.name] = strategy
        if strategy.name not in self.performance:
            self.performance[strategy.name] = StrategyPerformance(name=strategy.name)

    def record_outcome(self, strategy: str, pnl: float) -> None:
        perf = self.performance.setdefault(strategy, StrategyPerformance(name=strategy))
        perf.trades += 1
        if pnl > 0:
            perf.wins += 1
        perf.total_pnl += pnl
        perf.win_rate = perf.wins / perf.trades * 100 if perf.trades else 0
        self._auto_disable(perf)

    def _auto_disable(self, perf: StrategyPerformance) -> None:
        if perf.trades < self.cfg.strategy_disable_min_trades:
            return
        if perf.win_rate < self.cfg.strategy_disable_win_rate:
            perf.enabled = False
            perf.disabled_reason = f"Win rate {perf.win_rate:.1f}% below {self.cfg.strategy_disable_win_rate}%"

    def enabled_strategies(self) -> list[str]:
        return [n for n, p in self.performance.items() if p.enabled]

    def ranking(self) -> list[dict]:
        rows = sorted(
            self.performance.values(),
            key=lambda p: (p.win_rate, p.total_pnl),
            reverse=True,
        )
        return [
            {
                "name": p.name,
                "trades": p.trades,
                "win_rate": round(p.win_rate, 1),
                "total_pnl": round(p.total_pnl, 2),
                "enabled": p.enabled,
                "disabled_reason": p.disabled_reason,
            }
            for p in rows
        ]
