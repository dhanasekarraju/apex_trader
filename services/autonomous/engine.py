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

        blockers = self._start_blockers()
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

    async def stop(self) -> dict:
        from services.autonomous.state import set_autonomous_running

        await set_autonomous_running(False)
        self._running = False
        audit("autonomous_engine_stopped")
        return {"ok": True, "running": False}

    async def status(self) -> dict:
        cached = await get_autonomous_status()
        running = await is_autonomous_running()
        symbols = self.watchlist.resolve()
        blockers = self._start_blockers() if not running else []
        base = {
            "running": running,
            "mode": self.cfg.trading_mode,
            "watchlist_count": len(symbols),
            "watchlist_preview": symbols[:10],
            "scan_interval_sec": self.cfg.autonomous_scan_interval_sec,
            "session": f"{self.cfg.autonomous_session_start}–{self.cfg.autonomous_session_end} IST",
            "blockers": blockers,
            "last_cycle": self._last_cycle_at.isoformat() if self._last_cycle_at else None,
        }
        if cached:
            base.update(cached)
        return base

    async def tick(self) -> dict:
        """One autonomous scan cycle."""
        from services.icb.actions import ICBAction
        from services.icb.engine import icb

        if not await is_autonomous_running():
            return {"skipped": "not_running"}

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

        symbols = self._prioritize_symbols(self.watchlist.resolve())
        if not symbols:
            status = {"skipped": "empty_watchlist", "running": True}
            await set_autonomous_status(status)
            return status

        results: list[dict] = []
        stats = {"scanned": 0, "buy": 0, "rejected": 0, "no_trade": 0, "errors": 0}

        for symbol in symbols[: cfg.autonomous_max_symbols_per_cycle]:
            if self.orch.portfolio.is_trading_halted():
                audit("autonomous_halted_mid_cycle")
                break

            if self._in_cooldown(symbol):
                continue

            stats["scanned"] += 1
            try:
                decision = await self.orch.analyze_symbol(symbol)
                action = decision.get("action", "NO_TRADE")
                if action == "BUY":
                    stats["buy"] += 1
                elif action == "REJECTED":
                    stats["rejected"] += 1
                else:
                    stats["no_trade"] += 1
                results.append(
                    {
                        "symbol": symbol,
                        "action": action,
                        "reason": decision.get("risk_reason") or decision.get("reason", ""),
                        "strategy": decision.get("strategy"),
                    }
                )
                self._symbol_cooldown[symbol] = datetime.now(_IST)
                self._log_cycle(symbol, decision)
            except Exception as e:
                stats["errors"] += 1
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

    def _start_blockers(self) -> list[str]:
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

            blockers.extend(live_capital_blockers(require_full_suite=True))
        if self.orch.portfolio.is_trading_halted():
            blockers.append("Kill switch active")
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

    def _prioritize_symbols(self, symbols: list[str]) -> list[str]:
        """Open positions first (for exit monitoring context), then fresh symbols."""
        held = {p.symbol.upper() for p in self.orch.portfolio.state.positions}
        priority = [s for s in symbols if s not in held]
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
