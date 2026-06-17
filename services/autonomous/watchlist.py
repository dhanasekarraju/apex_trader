"""Watchlist resolution for autonomous scanning."""

from __future__ import annotations

from pathlib import Path

import yaml

from services.market_data.service import MarketDataService
from shared.config import Settings, get_settings
from shared.logging import audit

_ROOT = Path(__file__).resolve().parents[2]


class WatchlistProvider:
    """Load symbols from YAML, env, or daily Kite trending universe."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.cfg = settings or get_settings()
        self._last_universe: dict | None = None

    def resolve(self) -> list[str]:
        """Static symbol list (yaml/env)."""
        symbols = self._from_file() or self._from_env()
        seen: set[str] = set()
        out: list[str] = []
        for sym in symbols:
            cleaned = sym.strip().upper()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append(cleaned)
        return out[: self.cfg.autonomous_max_watchlist_size]

    async def resolve_scan_symbols(self, market_data: MarketDataService | None = None) -> list[str]:
        """Symbols to scan this cycle — dynamic trending or static list."""
        if self.cfg.watchlist_mode == "dynamic":
            from services.autonomous.dynamic_universe import DynamicUniverseSelector

            selector = DynamicUniverseSelector(market_data=market_data)
            snap = await selector.get_snapshot()
            self._last_universe = snap.to_dict()
            return self._prioritize_scan(snap.scan)

        static = self.resolve()
        cap = self.cfg.autonomous_max_symbols_per_cycle
        self._last_universe = {
            "mode": "static",
            "pool": static,
            "scan": static[:cap],
            "source": "watchlist_file",
        }
        return static[:cap]

    def last_universe_meta(self) -> dict | None:
        return self._last_universe

    def _prioritize_scan(self, symbols: list[str]) -> list[str]:
        return symbols[: self.cfg.autonomous_max_symbols_per_cycle]

    def _from_env(self) -> list[str]:
        raw = self.cfg.watchlist_symbols.strip()
        if not raw:
            return ["RELIANCE", "TCS", "INFY", "HDFCBANK"]
        return [s.strip() for s in raw.split(",") if s.strip()]

    def _from_file(self) -> list[str] | None:
        path_str = self.cfg.watchlist_file.strip()
        if not path_str:
            default = _ROOT / "data" / "watchlist.yaml"
            path = default if default.is_file() else None
        else:
            path = Path(path_str)
            if not path.is_absolute():
                path = _ROOT / path
        if path is None or not path.is_file():
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            symbols = data.get("symbols") or []
            audit("watchlist_loaded", path=str(path), count=len(symbols))
            return [str(s) for s in symbols]
        except Exception as e:
            audit("watchlist_load_failed", error=str(e))
            return None
