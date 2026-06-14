"""Risk dashboard — institutional status layer."""

from __future__ import annotations

from enum import Enum

from services.portfolio.manager import PortfolioManager
from shared.config import get_settings


class RiskStatus(str, Enum):
    SAFE = "SAFE"
    WARNING = "WARNING"
    DANGER = "DANGER"
    HALTED = "HALTED"


class RiskDashboard:
    """Compute desk-style risk status from portfolio + live PnL snapshot."""

    def __init__(self, portfolio: PortfolioManager) -> None:
        self.portfolio = portfolio
        self.cfg = get_settings()

    def compute(self, pnl_snapshot: dict | None = None) -> dict:
        cfg = self.cfg
        metrics = self.portfolio.metrics()
        equity = max(metrics.get("equity") or cfg.initial_capital, 1.0)

        drawdown = metrics.get("drawdown_pct") or 0.0
        heat = metrics.get("portfolio_heat_pct") or 0.0
        open_positions = metrics.get("open_positions") or 0
        daily_pnl = metrics.get("daily_pnl") or 0.0

        if pnl_snapshot:
            daily_pnl = pnl_snapshot.get("daily_pnl", daily_pnl)
            heat = max(heat, self._heat_from_exposure(pnl_snapshot, equity))

        daily_loss_pct = abs(min(daily_pnl, 0)) / equity * 100

        utilizations = {
            "daily_loss": self._pct_of_limit(daily_loss_pct, cfg.max_daily_loss_pct),
            "drawdown": self._pct_of_limit(drawdown, cfg.max_monthly_drawdown_pct),
            "portfolio_heat": self._pct_of_limit(heat, cfg.max_portfolio_heat_pct),
            "open_positions": self._pct_of_limit(
                open_positions, cfg.max_open_positions
            ),
        }

        kill_switch = self.portfolio.is_trading_halted()
        status = self._resolve_status(utilizations, kill_switch)

        return {
            "status": status.value,
            "drawdown": round(drawdown, 2),
            "daily_loss": round(daily_loss_pct, 2),
            "portfolio_heat": round(heat, 2),
            "open_positions": open_positions,
            "max_positions": cfg.max_open_positions,
            "kill_switch": kill_switch,
            "trading_allowed": status == RiskStatus.SAFE,
            "utilizations": {k: round(v, 1) for k, v in utilizations.items()},
            "limits": {
                "max_daily_loss_pct": cfg.max_daily_loss_pct,
                "max_drawdown_pct": cfg.max_monthly_drawdown_pct,
                "max_portfolio_heat_pct": cfg.max_portfolio_heat_pct,
                "max_open_positions": cfg.max_open_positions,
            },
            "flags": {
                "emergency_halt": metrics.get("emergency_halt", False),
                "circuit_breaker": metrics.get("circuit_breaker", False),
                "black_swan_mode": metrics.get("black_swan_mode", False),
            },
        }

    @staticmethod
    def _pct_of_limit(value: float, limit: float) -> float:
        if limit <= 0:
            return 0.0
        return min(value / limit * 100, 999.0)

    @staticmethod
    def _heat_from_exposure(pnl_snapshot: dict, equity: float) -> float:
        exposure = pnl_snapshot.get("open_exposure") or 0.0
        return min(exposure * 0.2, 100.0)

    @staticmethod
    def _resolve_status(utilizations: dict[str, float], halted: bool) -> RiskStatus:
        if halted:
            return RiskStatus.HALTED
        peak = max(utilizations.values()) if utilizations else 0.0
        if peak >= 90:
            return RiskStatus.DANGER
        if peak >= 70:
            return RiskStatus.WARNING
        return RiskStatus.SAFE
