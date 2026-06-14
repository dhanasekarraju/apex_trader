"""Portfolio persistence — PostgreSQL source of truth."""

from __future__ import annotations

from sqlalchemy import select

from services.portfolio.models import PortfolioState, PositionView
from shared.database import SessionLocal
from shared.logging import audit
from shared.models import Position, SystemState


class PortfolioRepository:
    """Load/save portfolio and system flags in PostgreSQL."""

    _SINGLETON_ID = 1

    async def load(self, state: PortfolioState) -> bool:
        try:
            async with SessionLocal() as session:
                row = await session.get(SystemState, self._SINGLETON_ID)
                if row is None:
                    row = SystemState(id=self._SINGLETON_ID)
                    session.add(row)
                    await self._flush_state(session, row, state)
                    await session.commit()
                    audit("portfolio_initialized", equity=state.equity)
                    return True

                state.equity = row.equity
                state.cash = row.cash
                state.daily_pnl = row.daily_pnl
                state.weekly_pnl = row.weekly_pnl
                state.monthly_pnl = row.monthly_pnl
                state.peak_equity = row.peak_equity
                state.consecutive_losses = row.consecutive_losses
                state.emergency_halt = row.emergency_halt
                state.circuit_breaker = row.circuit_breaker
                state.black_swan_mode = row.black_swan_mode

                result = await session.execute(
                    select(Position).where(Position.status == "open")
                )
                state.positions = [
                    PositionView(
                        symbol=p.symbol,
                        qty=p.qty,
                        entry=p.entry_price,
                        stop_loss=p.stop_loss,
                        take_profit=p.take_profit,
                        strategy=p.strategy,
                        unrealized_pnl=0.0,
                        risk_pct=p.risk_pct,
                        broker_order_id=p.broker_order_id or "",
                        stop_order_id=p.stop_order_id or "",
                        db_id=p.id,
                    )
                    for p in result.scalars().all()
                ]
                return True
        except Exception as e:
            audit("portfolio_load_failed", error=str(e))
            return False

    async def save(self, state: PortfolioState) -> bool:
        try:
            async with SessionLocal() as session:
                row = await session.get(SystemState, self._SINGLETON_ID)
                if row is None:
                    row = SystemState(id=self._SINGLETON_ID)
                    session.add(row)
                await self._flush_state(session, row, state)
                await session.commit()
                return True
        except Exception as e:
            audit("portfolio_save_failed", error=str(e))
            return False

    async def add_position(
        self,
        state: PortfolioState,
        pos: PositionView,
        *,
        confidence: float = 0.0,
    ) -> bool:
        try:
            async with SessionLocal() as session:
                db_pos = Position(
                    symbol=pos.symbol,
                    asset_class="equity",
                    side="long",
                    qty=pos.qty,
                    entry_price=pos.entry,
                    stop_loss=pos.stop_loss,
                    take_profit=pos.take_profit,
                    strategy=pos.strategy,
                    confidence=confidence,
                    risk_pct=pos.risk_pct,
                    broker_order_id=pos.broker_order_id,
                    stop_order_id=pos.stop_order_id,
                    status="open",
                )
                session.add(db_pos)
                await session.flush()
                pos.db_id = db_pos.id

                row = await session.get(SystemState, self._SINGLETON_ID)
                if row:
                    await self._flush_state(session, row, state)
                await session.commit()
                return True
        except Exception as e:
            audit("portfolio_add_position_failed", error=str(e))
            return False

    async def close_all_positions(self, state: PortfolioState) -> bool:
        try:
            async with SessionLocal() as session:
                result = await session.execute(
                    select(Position).where(Position.status == "open")
                )
                for p in result.scalars().all():
                    p.status = "closed"
                    p.exit_reason = "emergency_flatten"
                row = await session.get(SystemState, self._SINGLETON_ID)
                if row:
                    await self._flush_state(session, row, state)
                await session.commit()
                state.positions.clear()
                return True
        except Exception as e:
            audit("portfolio_close_all_failed", error=str(e))
            return False

    async def close_position(
        self,
        state: PortfolioState,
        *,
        symbol: str,
        exit_price: float,
        exit_reason: str,
        pnl: float,
    ) -> bool:
        try:
            async with SessionLocal() as session:
                result = await session.execute(
                    select(Position).where(
                        Position.symbol == symbol.upper(),
                        Position.status == "open",
                    )
                )
                db_pos = result.scalar_one_or_none()
                if db_pos is None:
                    return False
                db_pos.status = "closed"
                db_pos.exit_reason = exit_reason
                db_pos.pnl = pnl
                db_pos.closed_at = __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                )

                state.positions = [
                    p for p in state.positions if p.symbol.upper() != symbol.upper()
                ]
                row = await session.get(SystemState, self._SINGLETON_ID)
                if row:
                    await self._flush_state(session, row, state)
                await session.commit()
                audit("portfolio_position_closed", symbol=symbol, reason=exit_reason, pnl=pnl)
                return True
        except Exception as e:
            audit("portfolio_close_position_failed", error=str(e), symbol=symbol)
            return False

    async def is_healthy(self) -> bool:
        try:
            async with SessionLocal() as session:
                await session.execute(select(SystemState.id).limit(1))
                return True
        except Exception:
            return False

    @staticmethod
    async def _flush_state(session, row: SystemState, state: PortfolioState) -> None:
        row.equity = state.equity
        row.cash = state.cash
        row.daily_pnl = state.daily_pnl
        row.weekly_pnl = state.weekly_pnl
        row.monthly_pnl = state.monthly_pnl
        row.peak_equity = state.peak_equity or state.equity
        row.consecutive_losses = state.consecutive_losses
        row.emergency_halt = state.emergency_halt
        row.circuit_breaker = state.circuit_breaker
        row.black_swan_mode = state.black_swan_mode
