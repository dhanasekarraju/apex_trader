"""Dead letter queue — failed orders for manual review."""

from __future__ import annotations

import json

from sqlalchemy import select

from shared.database import SessionLocal
from shared.logging import audit
from shared.models import DeadLetterOrder


class DeadLetterQueue:
    async def enqueue(
        self,
        *,
        client_order_id: str,
        symbol: str,
        strategy: str,
        side: str,
        qty: float,
        trading_mode: str,
        failure_reason: str,
        payload: dict | None = None,
    ) -> None:
        try:
            async with SessionLocal() as session:
                existing = await session.execute(
                    select(DeadLetterOrder).where(
                        DeadLetterOrder.client_order_id == client_order_id
                    )
                )
                if existing.scalar_one_or_none():
                    return
                row = DeadLetterOrder(
                    client_order_id=client_order_id,
                    symbol=symbol,
                    strategy=strategy,
                    side=side,
                    qty=qty,
                    trading_mode=trading_mode,
                    failure_reason=failure_reason[:512],
                    payload=json.dumps(payload or {}, default=str),
                    status="pending_review",
                )
                session.add(row)
                await session.commit()
                audit("dlq_enqueued", order_id=client_order_id, reason=failure_reason)
        except Exception as e:
            audit("dlq_enqueue_failed", error=str(e), order_id=client_order_id)

    async def pending(self, limit: int = 50) -> list[dict]:
        async with SessionLocal() as session:
            result = await session.execute(
                select(DeadLetterOrder)
                .where(DeadLetterOrder.status == "pending_review")
                .order_by(DeadLetterOrder.created_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
            return [
                {
                    "client_order_id": r.client_order_id,
                    "symbol": r.symbol,
                    "strategy": r.strategy,
                    "qty": r.qty,
                    "mode": r.trading_mode,
                    "failure_reason": r.failure_reason,
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                }
                for r in rows
            ]
