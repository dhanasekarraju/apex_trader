"""Institutional autonomous trading engine — A→Z with full safety gates."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from services.autonomous.state import (
    get_autonomous_status,
    is_autonomous_running,
    set_autonomous_status,
)
from services.autonomous.watchlist import WatchlistProvider
from services.core.orchestrator import TradingOrchestrator
from services.icb.engine import icb
from services.risk.dashboard import RiskStatus
from shared.config import Settings, get_settings
from shared.logging import audit, trade_log

_IST = ZoneInfo("Asia/Kolkata")
_LOG_DIR = Path(__file__).resolve().parents[2] / "data" / "logs"


class AutonomousEngine:
    """
    Full autonomous scan → analyze → risk → execute pipeline.
    Delegates every symbol to TradingOrchestrator.analyze_symbol — no bypass.
    """

    def __init__(self, orchestrator: TradingOrchestrator) -> None:
        self.orch = orchestrator
        self.cfg = get_settings()
        self.watchlist = WatchlistProvider()
        self._symbol_cooldown: dict[str, datetime] = {}
        self._last_cycle_at: datetime | None = None
        self._running = False

    async def start(self) -> dict:
        from services.autonomous.state import set_autonomous_running
        from services.icb.actions import ICBAction
        from services.icb.engine import icb

        blockers = await self._start_blockers()
        if blockers:
            return {"ok": False, "running": False, "blockers": blockers}

        risk = self.orch.risk_dashboard.compute()
        icb_result = await icb.authorize(
            ICBAction.START_AUTONOMOUS,
            {
                "portfolio": self.orch.portfolio,
                "trading_mode": self.cfg.trading_mode,
                "risk_status": risk.get("status"),
            },
        )
        if not icb_result.allowed:
            return {"ok": False, "running": False, "blockers": [icb_result.reason]}

        await set_autonomous_running(True)
        self._running = True
        audit("autonomous_engine_started", mode=self.cfg.trading_mode)
        return {"ok": True, "running": True, "mode": self.cfg.trading_mode}

    async def maybe_auto_start(self) -> None:
        """Self-start at session open each day when AUTONOMOUS_AUTO_START=true.

        Runs from the background loop so it recovers even if Kite connected
        after boot or the box restarted overnight — no manual Start needed.
        """
        cfg = get_settings()
        self.cfg = cfg
        if not (cfg.autonomous_auto_start and cfg.autonomous_enabled):
            return
        if await is_autonomous_running():
            return
        if not self._in_session():
            return
        if self.orch.portfolio.is_trading_halted():
            return
        blockers = await self._start_blockers()
        if blockers:
            await self._auto_heal_chaos(blockers)
            audit("autonomous_auto_start_blocked", blockers=blockers[:3])
            return
        result = await self.start()
        if result.get("ok"):
            audit("autonomous_auto_started", mode=cfg.trading_mode)
        else:
            audit("autonomous_auto_start_failed", blockers=result.get("blockers"))

    async def _auto_heal_chaos(self, blockers: list[str]) -> None:
        """If autonomous is blocked only by a stale/missing chaos report, refresh it."""
        if self.cfg.trading_mode != "live":
            return
        if not any("chaos" in b.lower() for b in blockers):
            return
        from services.chaos.live_gate import ChaosLiveGate

        if not ChaosLiveGate.rerun_recommended():
            return
        from services.chaos.auto import ensure_fresh_report

        started = await ensure_fresh_report(quick=False)
        audit("autonomous_chaos_auto_refresh", started=started, context="auto_start")

    async def stop(self) -> dict:
        from services.autonomous.state import set_autonomous_running

        await set_autonomous_running(False)
        self._running = False
        audit("autonomous_engine_stopped")
        return {"ok": True, "running": False}

    async def status(self) -> dict:
        cached = await get_autonomous_status()
        running = await is_autonomous_running()
        await self.watchlist.load_universe_meta(self.orch.data)
        symbols = self.watchlist.resolve()
        universe = self.watchlist.last_universe_meta() or {}
        if self.cfg.watchlist_mode == "dynamic" and universe:
            symbols = universe.get("pool") or symbols
        blockers = await self._start_blockers() if not running else []
        last_cycle = self._last_cycle_at.isoformat() if self._last_cycle_at else None
        if cached and cached.get("last_cycle_at"):
            last_cycle = cached["last_cycle_at"]
        base = {
            "running": running,
            "mode": self.cfg.trading_mode,
            "watchlist_mode": self.cfg.watchlist_mode,
            "watchlist_count": len(symbols),
            "watchlist_preview": (universe.get("scan") or symbols)[:10],
            "universe_pool_size": universe.get("pool_size") or len(symbols),
            "universe_scan_size": universe.get("scan_size") or self.cfg.autonomous_max_symbols_per_cycle,
            "universe_source": universe.get("source"),
            "universe_refreshed_at": universe.get("refreshed_at"),
            "universe_trade_date": universe.get("trade_date"),
            "scan_interval_sec": self.cfg.autonomous_scan_interval_sec,
            "session": f"{self.cfg.autonomous_session_start}–{self.cfg.autonomous_session_end} IST",
            "blockers": blockers,
            "last_cycle": last_cycle,
            "skipped": cached.get("skipped") if cached else None,
            "stats": cached.get("stats") if cached else None,
            "updated_at": cached.get("updated_at") if cached else None,
        }
        if cached:
            base.update(cached)
        return base

    async def tick(self) -> dict:
        """One autonomous scan cycle."""
        from services.icb.actions import ICBAction
        from services.icb.engine import icb

        if not await is_autonomous_running():
            status = {"skipped": "not_running", "running": False}
            await set_autonomous_status(status)
            return status

        risk = self.orch.risk_dashboard.compute()
        icb_result = await icb.authorize(
            ICBAction.AUTONOMOUS_TICK,
            {
                "portfolio": self.orch.portfolio,
                "trading_mode": self.cfg.trading_mode,
                "risk_status": risk.get("status"),
                "source": "autonomous_tick",
            },
        )
        if not icb_result.allowed:
            status = {"skipped": icb_result.reason, "running": True, "system_state": icb_result.system_state.value}
            await set_autonomous_status(status)
            return status

        cfg = get_settings()
        self.cfg = cfg

        if cfg.trading_mode == "live":
            from services.chaos.live_gate import ChaosLiveGate

            chaos_ok, chaos_blockers = ChaosLiveGate.check_for_live(require_full_suite=True)
            if not chaos_ok:
                # Stale/missing report → auto-refresh in background and keep running.
                if ChaosLiveGate.rerun_recommended():
                    from services.chaos.auto import ensure_fresh_report

                    started = await ensure_fresh_report(quick=False)
                    audit("autonomous_chaos_auto_refresh", started=started, blockers=chaos_blockers[:2])
                    status = {"skipped": "chaos_report_refreshing", "running": True}
                    await set_autonomous_status(status)
                    return status
                # Genuine resilience failure → protect capital.
                reason = chaos_blockers[0] if chaos_blockers else "Chaos gate invalid"
                await icb.enter_safe_mode(f"Autonomous stopped — {reason}")
                await self.stop()
                audit("autonomous_stopped_chaos_gate", reason=reason)
                return {"skipped": reason, "running": False, "stopped": True, "chaos_gate": False}

        gate = self._cycle_gates()
        if gate:
            status = {"skipped": gate, "running": True}
            await set_autonomous_status(status)
            return status

        symbols = await self._prioritize_symbols(
            await self.watchlist.resolve_scan_symbols(self.orch.data)
        )
        if not symbols:
            status = {"skipped": "empty_watchlist", "running": True}
            await set_autonomous_status(status)
            return status

        results: list[dict] = []
        stats = {"scanned": 0, "buy": 0, "rejected": 0, "no_trade": 0, "errors": 0, "cooldown_skipped": 0, "insufficient_skipped": 0}

        for symbol in symbols[: cfg.autonomous_max_symbols_per_cycle]:
            if self.orch.portfolio.is_trading_halted():
                audit("autonomous_halted_mid_cycle")
                break

            if self._in_cooldown(symbol):
                stats["cooldown_skipped"] += 1
                continue

            stats["scanned"] += 1
            try:
                decision = await self.orch.analyze_symbol(symbol)
                action = decision.get("action", "NO_TRADE")
                reason = decision.get("risk_reason") or decision.get("reason", "")
                from services.brokers.messages import is_insufficient_balance

                if action == "BUY":
                    stats["buy"] += 1
                    self._symbol_cooldown[symbol] = datetime.now(_IST)
                elif action == "REJECTED" and is_insufficient_balance(reason):
                    stats["insufficient_skipped"] += 1
                    action = "SKIPPED"
                    reason = f"Insufficient margin — {reason}"
                elif action == "REJECTED":
                    stats["rejected"] += 1
                    self._symbol_cooldown[symbol] = datetime.now(_IST)
                else:
                    stats["no_trade"] += 1
                results.append(
                    {
                        "symbol": symbol,
                        "action": action,
                        "reason": reason,
                        "strategy": decision.get("strategy"),
                    }
                )
                self._log_cycle(symbol, decision)
            except Exception as e:
                stats["errors"] += 1
                self._symbol_cooldown[symbol] = datetime.now(_IST)
                audit("autonomous_symbol_error", symbol=symbol, error=str(e))
                results.append({"symbol": symbol, "action": "ERROR", "reason": str(e)})

            if cfg.autonomous_inter_symbol_delay_sec > 0:
                await asyncio.sleep(cfg.autonomous_inter_symbol_delay_sec)

        self._last_cycle_at = datetime.now(_IST)
        status = {
            "running": True,
            "last_cycle_at": self._last_cycle_at.isoformat(),
            "stats": stats,
            "recent": results[-10:],
            "open_positions": self.orch.portfolio.metrics().get("open_positions", 0),
        }
        await set_autonomous_status(status)
        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        await crce.record(
            event_type=EventType.AUTONOMOUS_TICK,
            action="AUTONOMOUS_SCAN",
            decision="EXECUTED",
            reason=f"scanned={stats['scanned']} buy={stats['buy']}",
            portfolio=self.orch.portfolio,
            stats=stats,
            autonomous_actions=results[-10:],
        )
        audit("autonomous_cycle_complete", **stats)
        return status

    async def _start_blockers(self) -> list[str]:
        import asyncio

        cfg = get_settings()
        blockers: list[str] = []
        if not cfg.autonomous_enabled:
            blockers.append("AUTONOMOUS_ENABLED is false in config")
        if cfg.trading_mode == "live":
            if not cfg.enable_live_execution:
                blockers.append("Live execution disabled")
            if not cfg.autonomous_allow_live:
                blockers.append("AUTONOMOUS_ALLOW_LIVE is false")
            if not cfg.golive_approved:
                blockers.append("GOLIVE_APPROVED is false")
            from services.live.checklist import live_capital_blockers

            blockers.extend(
                await asyncio.to_thread(live_capital_blockers, require_full_suite=True),
            )
        if self.orch.portfolio.is_trading_halted():
            blockers.append("Kill switch active — click Resume trading to clear")
        return blockers

    def _cycle_gates(self) -> str | None:
        if self.orch.portfolio.is_trading_halted():
            return "halted"
        if not self._in_session():
            return "outside_session"
        open_ok, _ = icb._market_open()
        if self.cfg.enforce_market_hours and not open_ok:
            return "market_closed"
        risk = self.orch.risk_dashboard.compute()
        if risk["status"] in (RiskStatus.HALTED.value, RiskStatus.DANGER.value):
            return f"risk_{risk['status'].lower()}"
        gate = self.orch._risk_dashboard_gate()
        if gate:
            return "risk_danger"
        if risk["open_positions"] >= self.cfg.max_open_positions:
            return "max_positions"
        return None

    def _in_session(self) -> bool:
        now = datetime.now(_IST)
        if now.weekday() >= 5:
            return False
        start = self._parse_time(self.cfg.autonomous_session_start)
        end = self._parse_time(self.cfg.autonomous_session_end)
        return start <= now.time() <= end

    @staticmethod
    def _parse_time(value: str) -> time:
        h, m = value.split(":")
        return time(int(h), int(m))

    async def _prioritize_symbols(self, symbols: list[str]) -> list[str]:
        """Fresh symbols first, sorted by price low → high (broker skips if no margin)."""
        held = {p.symbol.upper() for p in self.orch.portfolio.state.positions}
        priority = [s for s in symbols if s not in held]
        if not priority:
            return priority
        try:
            ltps = await self.orch.data.fetch_ltps(priority)
            priority.sort(key=lambda s: ltps.get(s.upper(), float("inf")))
        except Exception:
            pass
        return priority

    def _in_cooldown(self, symbol: str) -> bool:
        last = self._symbol_cooldown.get(symbol.upper())
        if last is None:
            return False
        elapsed = (datetime.now(_IST) - last).total_seconds()
        return elapsed < self.cfg.autonomous_symbol_cooldown_sec

    def _log_cycle(self, symbol: str, decision: dict) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "action": decision.get("action"),
            "strategy": decision.get("strategy"),
            "mode": self.cfg.trading_mode,
            "reason": decision.get("risk_reason") or decision.get("reason"),
        }
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = _LOG_DIR / "autonomous.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        trade_log(
            symbol=symbol,
            strategy=decision.get("strategy") or "autonomous",
            action="AUTO_SCAN",
            result=decision.get("action", ""),
            reason=record["reason"] or "",
        )
