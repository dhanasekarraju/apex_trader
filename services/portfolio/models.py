"""Portfolio datatypes — shared by manager and repository."""

from __future__ import annotations

from dataclasses import dataclass, field

from services.risk.engine import RiskState


@dataclass
class PositionView:
    symbol: str
    qty: float
    entry: float
    stop_loss: float
    take_profit: float
    strategy: str
    unrealized_pnl: float
    risk_pct: float
    broker_order_id: str = ""
    stop_order_id: str = ""
    db_id: int | None = None


@dataclass
class PortfolioState:
    equity: float
    cash: float
    positions: list[PositionView] = field(default_factory=list)
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    monthly_pnl: float = 0.0
    peak_equity: float = 0.0
    # MIS intraday buying power from Kite margins (0 = use equity for position caps)
    buying_power: float = 0.0
    consecutive_losses: int = 0
    emergency_halt: bool = False
    circuit_breaker: bool = False
    black_swan_mode: bool = False

    def to_risk_state(self) -> RiskState:
        heat = sum(p.risk_pct for p in self.positions)
        return RiskState(
            equity=self.equity,
            cash=self.cash,
            daily_pnl=self.daily_pnl,
            weekly_pnl=self.weekly_pnl,
            peak_equity=self.peak_equity or self.equity,
            open_positions=len(self.positions),
            portfolio_heat_pct=heat,
            consecutive_losses=self.consecutive_losses,
            emergency_halt=self.emergency_halt,
            circuit_breaker=self.circuit_breaker,
        )
