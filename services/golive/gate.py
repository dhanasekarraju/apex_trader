"""Go-live gate — refuse live trading until all validations pass."""

from __future__ import annotations

from dataclasses import dataclass

from shared.config import get_settings


@dataclass
class CategoryScore:
    name: str
    score: float
    passed: bool
    details: str


@dataclass
class ReadinessReport:
    overall_passed: bool
    live_allowed: bool
    categories: list[CategoryScore]
    blockers: list[str]
    recommendation: str


class GoLiveGate:
    """Live trading requires passing ALL configurable thresholds."""

    def __init__(self) -> None:
        self.cfg = get_settings()

    def evaluate(
        self,
        *,
        backtest: dict,
        shadow: dict,
        risk_healthy: bool,
        data_quality: float,
        watchdog_ok: bool,
        strategy_scores: dict[str, float],
    ) -> ReadinessReport:
        cfg = self.cfg
        categories: list[CategoryScore] = []
        blockers: list[str] = []

        strat_score = min(strategy_scores.values()) if strategy_scores else 0
        strat_pass = (
            backtest.get("sharpe", 0) >= cfg.golive_min_sharpe
            and backtest.get("win_rate", 0) >= cfg.golive_min_win_rate
            and backtest.get("profit_factor", 0) >= cfg.golive_min_profit_factor
            and backtest.get("max_drawdown", 99) <= cfg.golive_max_drawdown
            and backtest.get("passed_validation", False)
        )
        strat_effective = strat_pass or cfg.golive_approved
        strat_details = (
            f"Sharpe {backtest.get('sharpe')} win {backtest.get('win_rate')}% "
            f"PF {backtest.get('profit_factor')}"
        )
        if strat_pass:
            pass
        elif cfg.golive_approved:
            strat_details += " · operator override (GOLIVE_APPROVED)"
        categories.append(CategoryScore(
            "strategy_quality", strat_score, strat_effective, strat_details,
        ))
        if not strat_pass and not cfg.golive_approved:
            blockers.append("Backtest/walk-forward validation failed")

        risk_pass = risk_healthy
        categories.append(CategoryScore("risk_quality", 100 if risk_pass else 0, risk_pass, "Risk engine healthy"))
        if not risk_pass:
            blockers.append("Risk engine not healthy")

        exec_pass = shadow.get("simulated_fills", 0) >= 10
        exec_effective = exec_pass or cfg.golive_approved
        exec_details = f"Shadow fills {shadow.get('simulated_fills')} win {shadow.get('win_rate')}%"
        if exec_pass:
            pass
        elif cfg.golive_approved:
            exec_details += " · operator override (GOLIVE_APPROVED)"
        categories.append(CategoryScore(
            "execution_quality", shadow.get("win_rate", 0), exec_effective, exec_details,
        ))
        if not exec_pass and not cfg.golive_approved:
            blockers.append("Insufficient shadow mode history")

        ops_pass = watchdog_ok
        categories.append(CategoryScore("operational_quality", 100 if ops_pass else 0, ops_pass, "Watchdog OK"))
        if not ops_pass:
            blockers.append("Operational watchdog issues")

        data_pass = data_quality >= cfg.min_data_quality_score
        categories.append(CategoryScore(
            "data_quality", data_quality * 100, data_pass,
            f"Data quality score {data_quality:.2f}",
        ))
        if not data_pass:
            blockers.append("Data quality below threshold")

        all_pass = all(c.passed for c in categories) and not blockers
        live = all_pass and cfg.enable_live_execution

        return ReadinessReport(
            overall_passed=all_pass,
            live_allowed=live,
            categories=categories,
            blockers=blockers,
            recommendation=(
                "APPROVED for live trading with strict monitoring"
                if live else
                "NOT READY — remain in paper/shadow mode. " + "; ".join(blockers[:3])
            ),
        )
