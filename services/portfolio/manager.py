"""Portfolio management — heat, exposure, persistence."""

from __future__ import annotations

from services.portfolio.models import PortfolioState, PositionView
from services.portfolio.repository import PortfolioRepository
from services.risk.engine import TradeProposal
from shared.config import get_settings
from shared.logging import audit

# Re-export for callers importing from manager
__all__ = ["PortfolioManager", "PortfolioState", "PositionView"]


class PortfolioManager:
    def __init__(self) -> None:
        self.cfg = get_settings()
        self.repo = PortfolioRepository()
        self.persistence_ok = False
        self.state = PortfolioState(
            equity=self.cfg.initial_capital,
            cash=self.cfg.initial_capital,
            peak_equity=self.cfg.initial_capital,
        )

    async def load(self) -> bool:
        self.persistence_ok = await self.repo.load(self.state)
        return self.persistence_ok

    async def persist(self) -> bool:
        self.persistence_ok = await self.repo.save(self.state)
        return self.persistence_ok

    async def record_fill(
        self,
        pos: PositionView,
        *,
        confidence: float = 0.0,
    ) -> bool:
        cost = pos.qty * pos.entry
        self.state.cash = max(0.0, self.state.cash - cost)
        self.state.positions.append(pos)
        ok = await self.repo.add_position(self.state, pos, confidence=confidence)
        if ok:
            self.persistence_ok = await self.repo.save(self.state)
        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        await crce.record(
            event_type=EventType.PORTFOLIO_UPDATE,
            action="RECORD_FILL",
            symbol=pos.symbol,
            decision="EXECUTED",
            reason=f"fill qty={pos.qty} entry={pos.entry}",
            portfolio=self,
        )
        return ok and self.persistence_ok

    async def record_exit(
        self,
        *,
        symbol: str,
        exit_price: float,
        exit_reason: str,
        pnl: float,
    ) -> bool:
        sym = symbol.upper()
        pos = next((p for p in self.state.positions if p.symbol.upper() == sym), None)
        if pos is None:
            return False

        proceeds = pos.qty * exit_price
        self.state.cash += proceeds
        self.state.daily_pnl += pnl
        self.state.weekly_pnl += pnl
        self.state.monthly_pnl += pnl
        self.state.equity += pnl
        if self.state.equity > self.state.peak_equity:
            self.state.peak_equity = self.state.equity

        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        self.state.positions = [
            p for p in self.state.positions if p.symbol.upper() != sym
        ]

        ok = await self.repo.close_position(
            self.state,
            symbol=sym,
            exit_price=exit_price,
            exit_reason=exit_reason,
            pnl=pnl,
        )
        if ok:
            self.persistence_ok = await self.repo.save(self.state)
        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        await crce.record(
            event_type=EventType.PORTFOLIO_UPDATE,
            action="RECORD_EXIT",
            symbol=sym,
            decision="EXECUTED",
            reason=f"{exit_reason} pnl={pnl}",
            portfolio=self,
            exit_price=exit_price,
            pnl=pnl,
        )
        return ok and self.persistence_ok

    def emergency_shutdown(self) -> None:
        self.state.emergency_halt = True
        self.state.circuit_breaker = True

    def enter_black_swan(self) -> None:
        self.state.black_swan_mode = True
        self.emergency_shutdown()

    def resume_trading(self) -> None:
        """Clear emergency halt / black swan after operator review."""
        self.state.emergency_halt = False
        self.state.circuit_breaker = False
        self.state.black_swan_mode = False

    def is_trading_halted(self) -> bool:
        return (
            self.state.emergency_halt
            or self.state.circuit_breaker
            or self.state.black_swan_mode
        )

    async def clear_after_flatten(self) -> bool:
        ok = await self.repo.close_all_positions(self.state)
        if ok:
            self.persistence_ok = await self.repo.save(self.state)
        return ok and self.persistence_ok

    def size_position(self, proposal: TradeProposal) -> float:
        if proposal.entry <= proposal.stop_loss:
            return 0
        risk_budget = self.state.equity * self.cfg.max_risk_per_trade_pct / 100
        per_share_risk = abs(proposal.entry - proposal.stop_loss)
        qty = risk_budget / per_share_risk
        max_capital = self.state.equity * 0.15
        max_qty = max_capital / proposal.entry
        return max(0, min(qty, max_qty))

    def metrics(self) -> dict:
        s = self.state
        dd = 0.0
        if s.peak_equity > 0:
            dd = (s.peak_equity - s.equity) / s.peak_equity * 100
        heat = sum(p.risk_pct for p in s.positions)
        return {
            "equity": round(s.equity, 2),
            "cash": round(s.cash, 2),
            "daily_pnl": round(s.daily_pnl, 2),
            "drawdown_pct": round(dd, 2),
            "portfolio_heat_pct": round(heat, 2),
            "open_positions": len(s.positions),
            "consecutive_losses": s.consecutive_losses,
            "emergency_halt": s.emergency_halt,
            "circuit_breaker": s.circuit_breaker,
            "black_swan_mode": s.black_swan_mode,
            "trading_halted": self.is_trading_halted(),
        }

    async def sync_capital_from_kite(self, equity: float, cash: float) -> dict:
        """Update internal ledger from Zerodha margins — broker is source of truth for capital."""
        if equity <= 0:
            return {"ok": False, "reason": "invalid_equity"}
        previous = round(self.state.equity, 2)
        self.state.equity = round(equity, 2)
        self.state.cash = round(max(0.0, cash), 2)
        if self.state.peak_equity < self.state.equity:
            self.state.peak_equity = self.state.equity
        await self.persist()
        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        await crce.record(
            event_type=EventType.PORTFOLIO_UPDATE,
            action="SYNC_KITE_CAPITAL",
            decision="EXECUTED",
            reason=f"equity {previous} -> {self.state.equity}",
            portfolio=self,
        )
        audit(
            "capital_synced_from_kite",
            previous=previous,
            equity=self.state.equity,
            cash=self.state.cash,
        )
        return {
            "ok": True,
            "previous_equity": previous,
            "equity": self.state.equity,
            "cash": self.state.cash,
            "source": "kite_margins",
        }
