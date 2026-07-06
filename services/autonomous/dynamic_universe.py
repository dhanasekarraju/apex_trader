"""
Daily dynamic watchlist — trending NSE equities from Kite, no manual symbol list.

Flow (once per IST trading day):
  1. Quote a liquid candidate set via Kite (~120 names, NOT full NSE)
  2. Rank by volume × momentum → top 50 pool
  3. Pick top 15 for autonomous scanning that day
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from services.market_data.service import MarketDataService
from shared.config import Settings, get_settings
from shared.events import cache_get, cache_set
from shared.logging import audit

_IST = ZoneInfo("Asia/Kolkata")
_CACHE_PREFIX = "apex:universe"
_TTL_SEC = 86400 * 2
_BUILD_TIMEOUT_SEC = 45.0

# Liquid NSE candidates for quote ranking — avoids quoting 2000+ symbols (API freeze).
_LIQUID_CANDIDATES: list[str] = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "BHARTIARTL", "ITC",
    "KOTAKBANK", "LT", "AXISBANK", "HINDUNILVR", "MARUTI", "SUNPHARMA", "TITAN",
    "BAJFINANCE", "WIPRO", "HCLTECH", "ASIANPAINT", "ULTRACEMCO", "NESTLEIND",
    "TATASTEEL", "POWERGRID", "NTPC", "ONGC", "ADANIENT", "ADANIPORTS", "COALINDIA",
    "M&M", "TECHM", "JSWSTEEL", "INDUSINDBK", "BAJAJFINSV", "TRENT", "GRASIM",
    "HINDALCO", "CIPLA", "DRREDDY", "EICHERMOT", "BPCL", "DIVISLAB", "APOLLOHOSP",
    "HEROMOTOCO", "SBILIFE", "HDFCLIFE", "TATAMOTORS", "BEL", "HAL", "IRFC", "RVNL",
    "VEDL", "DLF", "SIEMENS", "ABB", "PIDILITIND", "GODREJCP", "DABUR", "BRITANNIA",
    "AMBUJACEM", "SHREECEM", "HAVELLS", "POLYCAB", "TORNTPHARM", "ICICIPRULI", "NAUKRI",
    "ZOMATO", "JIOFIN", "DMART", "CANBK", "PNB", "BANKBARODA", "UNIONBANK", "IDFCFIRSTB",
    "FEDERALBNK", "AUBANK", "CHOLAFIN", "SHRIRAMFIN", "MUTHOOTFIN", "LICI", "IOC",
    "GAIL", "HINDPETRO", "TATAPOWER", "ADANIGREEN", "VBL", "UNITDSPR", "COLPAL",
    "MARICO", "PAGEIND", "BOSCHLTD", "MRF", "TVSMOTOR", "ASHOKLEY", "BHARATFORG",
    "CONCOR", "IRCTC", "INDIGO", "JINDALSTEL", "SAIL", "NMDC", "OBEROIRLTY", "LODHA",
    "PRESTIGE", "PHOENIXLTD", "MAXHEALTH", "FORTIS", "LUPIN", "AUROPHARMA", "BIOCON",
    "PERSISTENT", "COFORGE", "MPHASIS", "LTIM", "OFSS", "TATAELXSI", "KPITTECH",
]


@dataclass
class UniverseSnapshot:
    trade_date: str
    pool: list[str]
    scan: list[str]
    source: str
    refreshed_at: str
    pool_size: int
    scan_size: int

    def to_dict(self) -> dict:
        return {
            "trade_date": self.trade_date,
            "pool": self.pool,
            "scan": self.scan,
            "source": self.source,
            "refreshed_at": self.refreshed_at,
            "pool_size": self.pool_size,
            "scan_size": self.scan_size,
            "mode": "dynamic",
        }


def _today_ist() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d")


def _cache_key(trade_date: str) -> str:
    return f"{_CACHE_PREFIX}:{trade_date}"


def trending_score(quote: dict) -> float:
    """Higher = more volume + stronger intraday move."""
    volume = float(quote.get("volume") or 0)
    last = float(quote.get("last_price") or 0)
    prev = float((quote.get("ohlc") or {}).get("close") or last or 1)
    if last <= 0 or volume <= 0:
        return 0.0
    pct = abs((last - prev) / prev * 100) if prev else 0.0
    return volume * (1.0 + pct)


def quote_candidates(extra: list[str] | None = None, limit: int = 120) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for sym in list(_LIQUID_CANDIDATES) + list(extra or []):
        s = sym.strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return out


class DynamicUniverseSelector:
    def __init__(
        self,
        market_data: MarketDataService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.data = market_data or MarketDataService()
        self.cfg = settings or get_settings()

    async def get_daily_scan_symbols(self) -> list[str]:
        snap = await self.get_snapshot()
        return snap.scan

    async def get_snapshot(self, *, allow_build: bool = True) -> UniverseSnapshot:
        trade_date = _today_ist()
        cached = await self._load_cache(trade_date)
        if cached:
            return cached
        if not allow_build:
            return self._fallback_snapshot(trade_date, source="fallback_pending")
        return await self.refresh()

    async def get_cached_snapshot(self) -> UniverseSnapshot | None:
        return await self._load_cache(_today_ist())

    async def refresh(self) -> UniverseSnapshot:
        """Force rebuild of today's pool + scan list."""
        trade_date = _today_ist()
        try:
            built = await asyncio.wait_for(
                self._build_universe(trade_date),
                timeout=_BUILD_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            audit("dynamic_universe_timeout", trade_date=trade_date)
            built = self._fallback_snapshot(trade_date, source="fallback_timeout")
        await self._save_cache(built)
        return built

    async def _load_cache(self, trade_date: str) -> UniverseSnapshot | None:
        try:
            raw = await cache_get(_cache_key(trade_date))
            if not raw:
                return None
            data = json.loads(raw)
            if data.get("trade_date") != trade_date:
                return None
            return UniverseSnapshot(
                trade_date=data["trade_date"],
                pool=list(data.get("pool") or []),
                scan=list(data.get("scan") or []),
                source=str(data.get("source") or "cache"),
                refreshed_at=str(data.get("refreshed_at") or ""),
                pool_size=int(data.get("pool_size") or len(data.get("pool") or [])),
                scan_size=int(data.get("scan_size") or len(data.get("scan") or [])),
            )
        except Exception:
            return None

    async def _save_cache(self, snap: UniverseSnapshot) -> None:
        await cache_set(_cache_key(snap.trade_date), json.dumps(snap.to_dict()), ttl=_TTL_SEC)

    def _fallback_snapshot(self, trade_date: str, *, source: str) -> UniverseSnapshot:
        pool_size = self.cfg.autonomous_universe_pool_size
        scan_size = min(self.cfg.autonomous_max_symbols_per_cycle, pool_size)
        pool = _LIQUID_CANDIDATES[:pool_size]
        scan = pool[:scan_size]
        return UniverseSnapshot(
            trade_date=trade_date,
            pool=pool,
            scan=scan,
            source=source,
            refreshed_at=datetime.now(_IST).isoformat(),
            pool_size=len(pool),
            scan_size=len(scan),
        )

    async def _build_universe(self, trade_date: str) -> UniverseSnapshot:
        pool_size = self.cfg.autonomous_universe_pool_size
        scan_size = min(self.cfg.autonomous_max_symbols_per_cycle, pool_size)
        refreshed_at = datetime.now(_IST).isoformat()

        if self.data.has_real_data_configured():
            try:
                pool, scan, source = await self._build_from_kite(pool_size, scan_size)
                audit(
                    "dynamic_universe_built",
                    source=source,
                    pool=len(pool),
                    scan=len(scan),
                    trade_date=trade_date,
                )
                return UniverseSnapshot(
                    trade_date=trade_date,
                    pool=pool,
                    scan=scan,
                    source=source,
                    refreshed_at=refreshed_at,
                    pool_size=len(pool),
                    scan_size=len(scan),
                )
            except Exception as exc:
                audit("dynamic_universe_kite_failed", error=str(exc))

        return self._fallback_snapshot(trade_date, source="fallback_liquid")

    async def _build_from_kite(self, pool_size: int, scan_size: int) -> tuple[list[str], list[str], str]:
        from services.autonomous.watchlist import WatchlistProvider

        static_extra = WatchlistProvider(self.cfg).resolve()
        candidates = quote_candidates(
            static_extra,
            limit=self.cfg.autonomous_universe_max_quotes,
        )
        quotes = await self.data.fetch_market_quotes(candidates)
        min_price = self.cfg.autonomous_universe_min_price
        min_volume = self.cfg.autonomous_universe_min_volume

        ranked: list[tuple[str, float]] = []
        for sym in candidates:
            q = quotes.get(sym.upper())
            if not q:
                continue
            last = float(q.get("last_price") or 0)
            vol = float(q.get("volume") or 0)
            if last < min_price or vol < min_volume:
                continue
            ranked.append((sym.upper(), trending_score(q)))

        ranked.sort(key=lambda x: x[1], reverse=True)
        pool = [s for s, _ in ranked[:pool_size]]
        if len(pool) < scan_size:
            extras = [s for s in _LIQUID_CANDIDATES if s not in pool]
            pool.extend(extras[: max(0, pool_size - len(pool))])

        # Scan cheapest first — broker is final gate if margin insufficient.
        price_by_sym: dict[str, float] = {}
        for sym in pool:
            q = quotes.get(sym.upper())
            if q:
                price_by_sym[sym.upper()] = float(q.get("last_price") or 0)
        pool.sort(key=lambda s: price_by_sym.get(s.upper(), float("inf")))
        scan = pool[:scan_size]
        return pool, scan, "kite_trending"
