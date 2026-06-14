"""Trade record persistence — order lifecycle for crash recovery."""

from __future__ import annotations

from sqlalchemy import select

from shared.database import SessionLocal
from shared.logging import audit
from shared.models import TradeRecord


class TradeRepository:
    async def create_pending(
        self,
        *,
        client_order_id: str,
        symbol: str,
        strategy: str,
        side: str,
        qty: float,
        stop_loss: float | None,
        take_profit: float | None,
        trading_mode: str,
    ) -> None:
        try:
            async with SessionLocal() as session:
                existing = await session.execute(
                    select(TradeRecord).where(
                        TradeRecord.client_order_id == client_order_id
                    )
                )
                if existing.scalar_one_or_none():
                    return
                row = TradeRecord(
                    client_order_id=client_order_id,
                    symbol=symbol,
                    strategy=strategy,
                    side=side,
                    qty=qty,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    trading_mode=trading_mode,
                    status="pending",
                )
                session.add(row)
                await session.commit()
        except Exception as e:
            audit("trade_record_create_failed", error=str(e), order_id=client_order_id)

    async def update_status(
        self,
        client_order_id: str,
        *,
        status: str,
        entry_price: float | None = None,
        exit_price: float | None = None,
        exit_reason: str | None = None,
        broker_order_id: str | None = None,
        stop_order_id: str | None = None,
        message: str | None = None,
    ) -> None:
        try:
            async with SessionLocal() as session:
                row = await session.execute(
                    select(TradeRecord).where(
                        TradeRecord.client_order_id == client_order_id
                    )
                )
                trade = row.scalar_one_or_none()
                if trade is None:
                    return
                trade.status = status
                if entry_price is not None:
                    trade.entry_price = entry_price
                if exit_price is not None:
                    trade.exit_price = exit_price
                if exit_reason is not None:
                    trade.exit_reason = exit_reason
                if broker_order_id is not None:
                    trade.broker_order_id = broker_order_id
                if stop_order_id is not None:
                    trade.stop_order_id = stop_order_id
                if message is not None:
                    trade.message = message
                await session.commit()
        except Exception as e:
            audit("trade_record_update_failed", error=str(e), order_id=client_order_id)

    async def get_by_client_id(self, client_order_id: str) -> TradeRecord | None:
        async with SessionLocal() as session:
            result = await session.execute(
                select(TradeRecord).where(TradeRecord.client_order_id == client_order_id)
            )
            return result.scalar_one_or_none()

    async def open_trades(self) -> list[TradeRecord]:
        open_status = {"pending", "submitted", "filled", "sl_placed"}
        async with SessionLocal() as session:
            result = await session.execute(
                select(TradeRecord).where(TradeRecord.status.in_(open_status))
            )
            return list(result.scalars().all())

    async def get_open_by_symbol(self, symbol: str) -> TradeRecord | None:
        open_status = {"pending", "submitted", "filled", "sl_placed"}
        async with SessionLocal() as session:
            result = await session.execute(
                select(TradeRecord)
                .where(TradeRecord.symbol == symbol.upper())
                .where(TradeRecord.status.in_(open_status))
                .order_by(TradeRecord.created_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()
