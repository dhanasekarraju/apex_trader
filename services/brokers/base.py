"""Broker abstraction — production order types with retry-safe interface."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    BRACKET = "bracket"
    TRAILING_STOP = "trailing_stop"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class OrderRequest:
    symbol: str
    side: str
    qty: float
    order_type: OrderType
    limit_price: float | None = None
    stop_price: float | None = None
    take_profit: float | None = None
    trailing_pct: float | None = None
    strategy: str = ""
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderResult:
    client_order_id: str
    broker_order_id: str
    status: OrderStatus
    filled_qty: float
    avg_price: float
    slippage_bps: float
    message: str
    raw: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class BrokerAdapter(ABC):
    name: str = "base"

    @abstractmethod
    async def connect(self) -> bool:
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    @abstractmethod
    async def is_connected(self) -> bool:
        ...

    @abstractmethod
    async def place_order(self, req: OrderRequest, market_price: float) -> OrderResult:
        ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        ...

    @abstractmethod
    async def cancel_all(self) -> int:
        ...

    @abstractmethod
    async def flatten_all(self) -> int:
        ...

    async def fetch_open_positions(self) -> list[dict]:
        """Return normalized open positions: [{symbol, qty, entry, side}]."""
        return []

    async def reconcile_order(
        self,
        broker_order_id: str,
        req: OrderRequest,
        timeout_sec: float = 30.0,
    ) -> OrderResult:
        """Poll broker for fill confirmation. Override in production brokers."""
        return OrderResult(
            req.client_order_id,
            broker_order_id,
            OrderStatus.SUBMITTED,
            0,
            0,
            0,
            "Reconciliation not implemented",
        )

    async def place_stop_loss(
        self,
        req: OrderRequest,
        market_price: float,
    ) -> OrderResult:
        """Place protective stop-loss after entry. Override in production brokers."""
        return OrderResult(
            req.client_order_id,
            "",
            OrderStatus.FAILED,
            0,
            0,
            0,
            "Stop-loss not implemented",
        )
