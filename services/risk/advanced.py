"""Advanced risk engine — extends base with sector, correlation, strategy disable."""

from __future__ import annotations

from dataclasses import dataclass, field

from services.risk.engine import RiskCheck, RiskDecision, RiskEngine, RiskState, RiskVerdict, TradeProposal
from shared.config import get_settings


@dataclass
class AdvancedRiskState(RiskState):
    monthly_pnl: float = 0.0
    sector_exposure: dict[str, float] = field(default_factory=dict)
    correlation_exposure: dict[str, float] = field(default_factory=dict)
    disabled_strategies: set[str] = field(default_factory=set)
    black_swan_mode: bool = False
    safe_mode: bool = False
    data_quality_score: float = 1.0


class AdvancedRiskEngine(RiskEngine):
    """Institutional protections — AI cannot bypass."""

    def evaluate_advanced(
        self,
        proposal: TradeProposal,
        state: AdvancedRiskState,
    ) -> RiskDecision:
        if state.black_swan_mode or state.safe_mode:
            return RiskDecision(
                RiskVerdict.HALTED, 0, [],
                reason="Safe/black swan mode — no new trades",
            )

        if proposal.strategy in state.disabled_strategies:
            return RiskDecision(
                RiskVerdict.REJECTED, 0,
                [RiskCheck("strategy_disabled", False, f"{proposal.strategy} auto-disabled")],
                reason=f"Strategy {proposal.strategy} disabled due to underperformance",
            )

        base = self.evaluate(proposal, state)
        if not base.approved:
            return base

        checks = list(base.checks)
        cfg = get_settings()

        if state.data_quality_score < cfg.min_data_quality_score:
            return RiskDecision(
                RiskVerdict.REJECTED, 0, checks,
                reason=f"Data quality {state.data_quality_score:.2f} below threshold",
            )

        sector_exp = state.sector_exposure.get(proposal.correlation_bucket, 0)
        sector_ok = sector_exp < cfg.max_sector_concentration_pct
        checks.append(RiskCheck(
            "sector_concentration", bool(sector_ok),
            f"Sector {proposal.correlation_bucket} exposure {sector_exp:.1f}%",
        ))

        corr_exp = state.correlation_exposure.get(proposal.correlation_bucket, 0)
        corr_ok = corr_exp < cfg.max_correlated_exposure_pct
        checks.append(RiskCheck(
            "correlation", bool(corr_ok),
            f"Correlated exposure {corr_exp:.1f}%",
        ))

        monthly_limit = state.equity * cfg.max_monthly_loss_pct / 100
        monthly_ok = state.monthly_pnl >= -monthly_limit
        checks.append(RiskCheck(
            "monthly_loss", bool(monthly_ok),
            f"Monthly PnL {state.monthly_pnl:.0f} within limit",
        ))

        if proposal.volatility_pct > cfg.vol_threshold_reduce:
            base.size_multiplier *= cfg.high_vol_size_multiplier
            checks.append(RiskCheck(
                "high_vol_reduce", True,
                f"Size reduced {cfg.high_vol_size_multiplier}x — vol {proposal.volatility_pct:.1f}%",
            ))

        if not all(c.passed for c in checks):
            failed = next(c for c in checks if not c.passed)
            return RiskDecision(RiskVerdict.REJECTED, 0, checks, reason=failed.detail)

        qty = base.approved_qty * base.size_multiplier
        verdict = RiskVerdict.REDUCED if base.size_multiplier < 1 else base.verdict
        return RiskDecision(verdict, qty, checks, base.reason, base.size_multiplier)

    def enter_black_swan(self, state: AdvancedRiskState) -> None:
        state.black_swan_mode = True
        state.emergency_halt = True
        state.circuit_breaker = True

    def enter_safe_mode(self, state: AdvancedRiskState) -> None:
        state.safe_mode = True
        state.circuit_breaker = True
