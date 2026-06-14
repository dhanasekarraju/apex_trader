"""SQLAlchemy models — audit trail, positions, decisions, backtests."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    asset_class: Mapped[str] = mapped_column(String(16), default="equity")
    side: Mapped[str] = mapped_column(String(8), default="long")
    qty: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    strategy: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float)
    risk_pct: Mapped[float] = mapped_column(Float, default=0.0)
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    stop_order_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="open")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pnl: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[str | None] = mapped_column(String(64))


class SystemState(Base):
    """Singleton row — portfolio capital and emergency flags."""

    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equity: Mapped[float] = mapped_column(Float, default=1_000_000.0)
    cash: Mapped[float] = mapped_column(Float, default=1_000_000.0)
    daily_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    weekly_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    monthly_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    peak_equity: Mapped[float] = mapped_column(Float, default=1_000_000.0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    emergency_halt: Mapped[bool] = mapped_column(Boolean, default=False)
    circuit_breaker: Mapped[bool] = mapped_column(Boolean, default=False)
    black_swan_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TradeDecision(Base):
    __tablename__ = "trade_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(16))
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float)
    regime: Mapped[str] = mapped_column(String(32))
    strategy: Mapped[str] = mapped_column(String(64))
    risk_checks: Mapped[str] = mapped_column(Text)
    gates: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    heat_pct: Mapped[float] = mapped_column(Float)
    daily_pnl: Mapped[float] = mapped_column(Float)
    drawdown_pct: Mapped[float] = mapped_column(Float)
    open_positions: Mapped[int] = mapped_column(Integer)
    regime: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32))
    start_date: Mapped[str] = mapped_column(String(16))
    end_date: Mapped[str] = mapped_column(String(16))
    total_trades: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[float] = mapped_column(Float)
    sharpe: Mapped[float] = mapped_column(Float)
    sortino: Mapped[float] = mapped_column(Float)
    max_drawdown: Mapped[float] = mapped_column(Float)
    net_return_pct: Mapped[float] = mapped_column(Float)
    passed_stress: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KiteSession(Base):
    """Singleton — OAuth access token from daily Zerodha login."""

    __tablename__ = "kite_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    access_token: Mapped[str | None] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(String(32))
    user_name: Mapped[str | None] = mapped_column(String(128))
    login_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TradeRecord(Base):
    """Persisted order lifecycle — crash recovery source of truth."""

    __tablename__ = "trade_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(64), default="")
    side: Mapped[str] = mapped_column(String(8), default="long")
    qty: Mapped[float] = mapped_column(Float, default=0)
    entry_price: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    trading_mode: Mapped[str] = mapped_column(String(16), default="paper")
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    stop_order_id: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DeadLetterOrder(Base):
    """Failed orders queued for manual review."""

    __tablename__ = "dead_letter_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(64), default="")
    side: Mapped[str] = mapped_column(String(8), default="long")
    qty: Mapped[float] = mapped_column(Float, default=0)
    trading_mode: Mapped[str] = mapped_column(String(16), default="paper")
    failure_reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="pending_review", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
