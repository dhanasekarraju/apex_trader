"""Calendar PnL period resets — daily / weekly / monthly loss limits."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from services.portfolio.manager import PortfolioManager
from shared.events import cache_get, cache_set
from shared.logging import audit

_IST = ZoneInfo("Asia/Kolkata")
_memory_reset: dict[str, str] = {}


async def maybe_reset_pnl_periods(portfolio: PortfolioManager) -> dict:
    """Reset realized PnL counters on IST calendar boundaries (once per period)."""
    now = datetime.now(_IST)
    today = now.date().isoformat()
    week_key = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    month_key = f"{now.year}-{now.month:02d}"

    resets: list[str] = []

    if await _should_reset("daily", today):
        portfolio.state.daily_pnl = 0.0
        await _mark_reset("daily", today)
        resets.append("daily")

    if await _should_reset("weekly", week_key):
        portfolio.state.weekly_pnl = 0.0
        await _mark_reset("weekly", week_key)
        resets.append("weekly")

    if await _should_reset("monthly", month_key):
        portfolio.state.monthly_pnl = 0.0
        await _mark_reset("monthly", month_key)
        resets.append("monthly")

    if resets:
        await portfolio.persist()
        audit("pnl_period_reset", periods=",".join(resets))

    return {"reset": resets}


async def _should_reset(period: str, current_key: str) -> bool:
    last = await _get_last_reset(period)
    return last != current_key


async def _get_last_reset(period: str) -> str | None:
    key = f"apex:pnl_reset:{period}"
    try:
        val = await cache_get(key)
        if val:
            return val
    except Exception:
        pass
    return _memory_reset.get(period)


async def _mark_reset(period: str, key: str) -> None:
    _memory_reset[period] = key
    try:
        await cache_set(f"apex:pnl_reset:{period}", key, ttl=86400 * 400)
    except Exception:
        pass
