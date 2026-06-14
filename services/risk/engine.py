"""Institutional risk engine — capital preservation overrides everything."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from shared.config import Settings, get_settings


class RiskVerdict(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    REDUCED = "reduced_size"
    HALTED = "halted"


@dataclass
class RiskState:
    equity: float
    cash: float
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    peak_equity: float = 0.0
    open_positions: int = 0
    portfolio_heat_pct: float = 0.0
    consecutive_losses: int = 0
    emergency_halt: bool = False
    circuit_breaker: bool = False


@dataclass
class TradeProposal:
    symbol: str
    asset_class: str
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    qty: float
    confidence: float
    strategy: str
    regime: str
    liquidity_score: float = 1.0
    volatility_pct: float = 1.0
    correlation_bucket: str = "general"


@dataclass
class RiskCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class RiskDecision:
    verdict: RiskVerdict
    approved_qty: float
    checks: list[RiskCheck] = field(default_factory=list)
    reason: str = ""
    size_multiplier: float = 1.0

    @property
    def approved(self) -> bool:
        return self.verdict in (RiskVerdict.APPROVED, RiskVerdict.REDUCED)

    def to_json(self) -> str:
        return json.dumps({
            "verdict": self.verdict.value,
            "approved_qty": self.approved_qty,
            "reason": self.reason,
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks],
        })


class RiskEngine:
    """
    Institutional-grade risk gate.
    AI/signals NEVER bypass this layer.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.cfg = settings or get_settings()

    def evaluate(self, proposal: TradeProposal, state: RiskState) -> RiskDecision:
        checks: list[RiskCheck] = []

        def chk(name: str, ok: bool, detail: str) -> bool:
            checks.append(RiskCheck(name, bool(ok), detail))
            return bool(ok)

        if state.emergency_halt:
            return RiskDecision(
                RiskVerdict.HALTED, 0, checks,
                reason="Emergency shutdown active",
            )

        if state.circuit_breaker:
            return RiskDecision(
                RiskVerdict.HALTED, 0, checks,
                reason="Circuit breaker triggered",
            )

        chk(
            "confidence",
            proposal.confidence >= self.cfg.min_confidence_score,
            f"Confidence {proposal.confidence:.1f} vs min {self.cfg.min_confidence_score}",
        )

        chk(
            "daily_loss",
            self._daily_loss_ok(state),
            f"Daily PnL {state.daily_pnl:.2f} within -{self.cfg.max_daily_loss_pct}% limit",
        )

        chk(
            "weekly_loss",
            self._weekly_loss_ok(state),
            f"Weekly PnL within -{self.cfg.max_weekly_loss_pct}% limit",
        )

        chk(
            "drawdown",
            self._drawdown_ok(state),
            f"Drawdown within {self.cfg.max_monthly_drawdown_pct}% cap",
        )

        chk(
            "position_count",
            state.open_positions < self.cfg.max_open_positions,
            f"Open {state.open_positions}/{self.cfg.max_open_positions}",
        )

        per_trade_risk = self._per_trade_risk_pct(proposal, state)
        chk(
            "per_trade_risk",
            per_trade_risk <= self.cfg.max_risk_per_trade_pct,
            f"Trade risk {per_trade_risk:.2f}% ≤ {self.cfg.max_risk_per_trade_pct}%",
        )

        projected_heat = state.portfolio_heat_pct + per_trade_risk
        chk(
            "portfolio_heat",
            projected_heat <= self.cfg.max_portfolio_heat_pct,
            f"Heat {projected_heat:.2f}% ≤ {self.cfg.max_portfolio_heat_pct}%",
        )

        chk(
            "liquidity",
            proposal.liquidity_score >= 0.6,
            f"Liquidity score {proposal.liquidity_score:.2f}",
        )

        chk(
            "volatility",
            proposal.volatility_pct <= 50.0,
            f"Vol {proposal.volatility_pct:.2f}% within crisis threshold",
        )

        chk(
            "regime_clarity",
            proposal.regime not in ("unclear", "crisis", "black_swan"),
            f"Regime: {proposal.regime}",
        )

        if not all(c.passed for c in checks):
            failed = next(c for c in checks if not c.passed)
            return RiskDecision(
                RiskVerdict.REJECTED, 0, checks,
                reason=f"Risk rejected: {failed.name} — {failed.detail}",
            )

        multiplier = 1.0
        if state.consecutive_losses >= self.cfg.consecutive_loss_halt:
            return RiskDecision(
                RiskVerdict.HALTED, 0, checks,
                reason=f"{state.consecutive_losses} consecutive losses — trading halted",
            )
        if state.consecutive_losses >= self.cfg.consecutive_loss_reduce:
            multiplier = 0.5
            checks.append(RiskCheck(
                "loss_streak_reduce",
                True,
                f"Reduced size 50% after {state.consecutive_losses} losses",
            ))

        approved_qty = proposal.qty * multiplier
        verdict = RiskVerdict.REDUCED if multiplier < 1 else RiskVerdict.APPROVED
        return RiskDecision(
            verdict, approved_qty, checks,
            reason="All risk checks passed",
            size_multiplier=multiplier,
        )

    def _daily_loss_ok(self, state: RiskState) -> bool:
        limit = state.equity * self.cfg.max_daily_loss_pct / 100
        return state.daily_pnl >= -limit

    def _weekly_loss_ok(self, state: RiskState) -> bool:
        limit = state.equity * self.cfg.max_weekly_loss_pct / 100
        return state.weekly_pnl >= -limit

    def _drawdown_ok(self, state: RiskState) -> bool:
        if state.peak_equity <= 0:
            return True
        dd = (state.peak_equity - state.equity) / state.peak_equity * 100
        return dd <= self.cfg.max_monthly_drawdown_pct

    def _per_trade_risk_pct(self, p: TradeProposal, state: RiskState) -> float:
        if p.entry <= p.stop_loss or state.equity <= 0:
            return 999.0
        risk_rs = abs(p.entry - p.stop_loss) * p.qty
        return risk_rs / state.equity * 100

    def trigger_circuit_breaker(self, state: RiskState) -> None:
        state.circuit_breaker = True

    def emergency_shutdown(self, state: RiskState) -> None:
        state.emergency_halt = True
        state.circuit_breaker = True
