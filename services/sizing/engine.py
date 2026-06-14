"""Position sizing engine — fixed risk, ATR, vol, Kelly (capped), portfolio risk."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from shared.config import get_settings


class SizingMethod(str, Enum):
    FIXED_RISK = "fixed_risk"
    ATR = "atr"
    VOLATILITY = "volatility"
    KELLY = "kelly"
    PORTFOLIO = "portfolio"


@dataclass
class SizeInput:
    equity: float
    entry: float
    stop_loss: float
    atr: float = 0.0
    volatility_pct: float = 1.0
    win_rate: float = 0.5
    avg_win_loss_ratio: float = 1.5
    portfolio_heat_pct: float = 0.0
    risk_multiplier: float = 1.0


@dataclass
class SizeResult:
    qty: float
    method: str
    risk_pct: float
    risk_rs: float
    capped: bool
    detail: str


class PositionSizingEngine:
    """Never exceed configurable risk limits."""

    def __init__(self) -> None:
        self.cfg = get_settings()

    def compute(
        self,
        inp: SizeInput,
        method: SizingMethod = SizingMethod.FIXED_RISK,
    ) -> SizeResult:
        if inp.entry <= inp.stop_loss or inp.equity <= 0:
            return SizeResult(0, method.value, 0, 0, True, "Invalid entry/SL")

        per_share_risk = abs(inp.entry - inp.stop_loss)
        max_risk_pct = self.cfg.max_risk_per_trade_pct * inp.risk_multiplier
        risk_budget = inp.equity * max_risk_pct / 100

        if method == SizingMethod.ATR and inp.atr > 0:
            qty = risk_budget / max(inp.atr * 1.5, per_share_risk)
            detail = f"ATR sizing atr={inp.atr:.2f}"
        elif method == SizingMethod.VOLATILITY:
            vol_adj = max(0.3, min(1.0, 20 / max(inp.volatility_pct, 1)))
            qty = (risk_budget / per_share_risk) * vol_adj
            detail = f"Vol-adjusted x{vol_adj:.2f}"
        elif method == SizingMethod.KELLY:
            w, r = inp.win_rate / 100, inp.avg_win_loss_ratio
            kelly = max(0, w - (1 - w) / r) if r > 0 else 0
            kelly = min(kelly, self.cfg.kelly_cap_fraction)
            qty = (inp.equity * kelly) / inp.entry
            detail = f"Kelly fraction {kelly:.3f} (capped)"
        elif method == SizingMethod.PORTFOLIO:
            heat_room = max(0, self.cfg.max_portfolio_heat_pct - inp.portfolio_heat_pct)
            risk_budget = inp.equity * min(max_risk_pct, heat_room) / 100
            qty = risk_budget / per_share_risk
            detail = f"Portfolio heat room {heat_room:.2f}%"
        else:
            qty = risk_budget / per_share_risk
            detail = f"Fixed risk {max_risk_pct:.2f}%"

        max_capital = inp.equity * 0.15
        max_qty = max_capital / inp.entry
        capped = qty > max_qty
        qty = max(0, min(int(qty), int(max_qty)))

        risk_rs = qty * per_share_risk
        risk_pct = risk_rs / inp.equity * 100 if inp.equity else 0

        return SizeResult(
            qty, method.value, round(risk_pct, 3),
            round(risk_rs, 2), capped, detail,
        )
