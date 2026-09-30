"""Live trading safety gate — all prerequisites must pass."""

from __future__ import annotations

from services.brokers.base import BrokerAdapter
from services.market_data.service import MarketDataService
from services.portfolio.manager import PortfolioManager
from shared.config import Settings, get_settings


class LiveSafetyGate:
    """Live execution blocked unless every critical subsystem is operational."""

    @staticmethod
    async def check(
        *,
        market_data: MarketDataService,
        broker: BrokerAdapter,
        portfolio: PortfolioManager,
        settings: Settings | None = None,
    ) -> tuple[bool, list[str]]:
        cfg = settings or get_settings()
        blockers: list[str] = []

        if not await market_data.verify_real_data():
            blockers.append("Real market data not configured or fetch failed")

        if not await portfolio.repo.is_healthy():
            blockers.append("PostgreSQL unavailable — portfolio cannot persist")
        elif not portfolio.persistence_ok:
            blockers.append("Portfolio state not loaded from PostgreSQL")

        if cfg.default_broker.lower() != "kite":
            blockers.append("Live trading requires DEFAULT_BROKER=kite")

        if not getattr(broker, "live_ready", False):
            blockers.append("Broker missing live reconciliation/stop-loss/flatten support")

        if not await broker.is_connected():
            blockers.append("Broker not connected")

        if cfg.kite_autoslice:
            blockers.append("Auto-slice disabled: all child orders must be tracked before use")
        if not cfg.enable_live_execution:
            blockers.append("ENABLE_LIVE_EXECUTION is false")

        liquid_backing = portfolio.state.cash + sum(p.qty * p.entry for p in portfolio.state.positions)
        if portfolio.state.equity > liquid_backing + 1.0:
            blockers.append("Capital ledger exceeds liquid backing; reconcile the configured capital before live trading")

        if not cfg.golive_approved:
            blockers.append(
                "GOLIVE_APPROVED is false — operator must explicitly approve live trading"
            )

        if portfolio.is_trading_halted():
            blockers.append("EMERGENCY_HALT active — kill switch engaged")

        if cfg.default_broker.lower() == "kite" and not cfg.kite_static_ip_confirmed:
            blockers.append(
                "Kite static IP not confirmed — register server IP at developers.kite.trade "
                "and set KITE_STATIC_IP_CONFIRMED=true"
            )

        from services.live.checklist import live_capital_blockers

        blockers.extend(live_capital_blockers(require_full_suite=True))

        from services.golive.evidence import validation_blockers
        blockers.extend(validation_blockers(cfg))
        from services.shadow.engine import ShadowEngine
        shadow = ShadowEngine().weekly_report()
        if (shadow.get("active_days", 0) < cfg.golive_min_shadow_days
                or shadow.get("completed_trades", 0) < cfg.golive_min_completed_trades
                or shadow.get("total_shadow_pnl", 0) <= 0):
            blockers.append("Insufficient profitable completed shadow history; operator approval cannot bypass")
        return len(blockers) == 0, blockers
