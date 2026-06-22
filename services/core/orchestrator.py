"""Central trading orchestrator — production pipeline v2."""

from __future__ import annotations

from services.ai.ranking import AISupportLayer
from services.alerts.notifier import AlertNotifier
from services.backtest.professional import ProfessionalBacktester
from services.brokers.base import OrderRequest, OrderType
from services.data_quality.engine import DataQualityEngine
from services.execution.execution_engine import ExecutionEngine
from services.execution.idempotency import make_order_id
from services.execution.lifecycle import PositionLifecycleService
from services.golive.gate import GoLiveGate
from services.journal.service import TradeJournal
from services.market_data.service import MarketDataService, RealDataRequired
from services.pnl.engine import LivePnLEngine
from services.portfolio.manager import PortfolioManager, PositionView
from services.regime.detector import RegimeDetector
from services.risk.advanced import AdvancedRiskState
from services.risk.dashboard import RiskDashboard, RiskStatus
from services.risk.engine import TradeProposal
from services.risk.unified import UnifiedRiskEngine
from services.sizing.engine import PositionSizingEngine, SizeInput, SizingMethod
from services.strategy_lab.registry import StrategyLab
from services.strategies.engine import StrategyEngine
from services.watchdog.service import WatchdogService
from shared.config import get_settings
from shared.logging import audit, trade_log
from shared.timeout import with_timeout


class TradingOrchestrator:
    def __init__(self) -> None:
        self.cfg = get_settings()
        self.data = MarketDataService()
        self.regime = RegimeDetector()
        self.strategies = StrategyEngine()
        self.strategy_lab = StrategyLab()
        self.ai = AISupportLayer()
        self.risk = UnifiedRiskEngine()
        self.sizing = PositionSizingEngine()
        self.portfolio = PortfolioManager()
        self.journal = TradeJournal()
        self.execution = ExecutionEngine(portfolio=self.portfolio, market_data=self.data)
        self.lifecycle = PositionLifecycleService(
            portfolio=self.portfolio,
            execution=self.execution,
            market_data=self.data,
            journal=self.journal,
        )
        self.pnl_engine = LivePnLEngine(portfolio=self.portfolio, market_data=self.data)
        self.risk_dashboard = RiskDashboard(self.portfolio)
        self.data_quality = DataQualityEngine(
            min_score=self.cfg.min_data_quality_score,
            max_stale_sec=self.cfg.max_stale_feed_seconds,
        )
        self.watchdog = WatchdogService()
        self.backtest = ProfessionalBacktester()
        self.golive = GoLiveGate()
        self.alerts = AlertNotifier()
        self.decisions: list[dict] = []
        self.equity_curve: list[dict] = []
        from services.autonomous.engine import AutonomousEngine

        self.autonomous = AutonomousEngine(self)

    async def startup(self) -> None:
        from services.brokers.kite_auth import kite_auth
        from services.icb.engine import icb
        from services.icb.system_state import get_kill_switch_latched

        await self.portfolio.load()
        await kite_auth.startup()
        self.data._real_data_ok = None
        from services.control.halt import set_emergency_halt

        if await get_kill_switch_latched() or self.portfolio.is_trading_halted():
            await set_emergency_halt(True)
            await icb.activate_emergency_lock("Startup with active kill switch")
        recovery = await self.execution.recover()
        await self.refresh_control_cache()
        if self.cfg.autonomous_auto_start and self.cfg.autonomous_enabled:
            if not self.portfolio.is_trading_halted() and not await get_kill_switch_latched():
                await self.autonomous.start()
        audit("orchestrator_startup", **recovery)

    async def refresh_control_cache(self) -> None:
        """Refresh Redis PnL + risk snapshots for live UI."""
        from services.control.halt import cache_pnl_snapshot, cache_risk_snapshot

        pnl = await self.pnl_engine.compute()
        risk = self.risk_dashboard.compute(pnl)
        await cache_pnl_snapshot(pnl)
        await cache_risk_snapshot(risk)

    async def live_pnl(self) -> dict:
        cached = None
        try:
            from services.control.halt import get_cached_pnl
            cached = await get_cached_pnl()
        except Exception:
            pass
        if cached:
            return cached
        return await self.pnl_engine.compute()

    def risk_status(self, pnl_snapshot: dict | None = None) -> dict:
        return self.risk_dashboard.compute(pnl_snapshot)

    async def shutdown(self) -> None:
        await self.autonomous.stop()
        await self.portfolio.persist()
        await self.execution.shutdown()
        audit("orchestrator_shutdown")

    async def analyze_symbol(self, symbol: str) -> dict:
        timeout = (
            self.cfg.execute_timeout_sec
            if self.cfg.trading_mode == "live"
            else self.cfg.analyze_timeout_sec
        )
        try:
            return await with_timeout(
                self._analyze_symbol(symbol),
                seconds=timeout,
                label="analyze_symbol",
            )
        except TimeoutError as exc:
            audit("analyze_timeout", symbol=symbol, error=str(exc))
            return self._no_trade(symbol, str(exc), "unclear")

    async def _analyze_symbol(self, symbol: str) -> dict:
        from services.icb.actions import ICBAction
        from services.icb.engine import icb

        risk_snapshot = self.risk_dashboard.compute()
        icb_result = await icb.authorize(
            ICBAction.ANALYZE_SYMBOL,
            {
                "portfolio": self.portfolio,
                "trading_mode": self.cfg.trading_mode,
                "symbol": symbol,
                "risk_status": risk_snapshot.get("status"),
            },
        )
        if not icb_result.allowed:
            return self._no_trade(symbol, f"ICB: {icb_result.reason}", "halted")

        try:
            df, _source = await self.data.get_trading_ohlcv(
                symbol, mode=self.cfg.trading_mode,
            )
        except RealDataRequired as exc:
            return self._no_trade(symbol, str(exc), "unclear")

        dq = self.data_quality.assess(df, symbol)
        regime = self.regime.analyze(df)
        health = await self.watchdog.check_all(await self.execution.connect())
        risk_state = self._risk_state(dq.score, health.safe_mode)

        if not dq.trade_allowed:
            return self._no_trade(symbol, f"Data quality {dq.score}: {dq.issues}", regime.regime.value)
        if health.safe_mode:
            await self.alerts.send("Safe Mode", "Trading paused — infrastructure issue", "critical")
            return self._no_trade(symbol, f"Safe mode: {health.issues}", regime.regime.value)
        if not regime.trade_allowed:
            return self._no_trade(symbol, regime.explanation, regime.regime.value)

        lab_enabled = self.strategy_lab.enabled_strategies()
        scan_allowed = lab_enabled or regime.recommended_strategies
        signals = self.strategies.scan(symbol, df, regime.regime.value, scan_allowed)
        if not signals:
            self.execution._shadow.record_missed(symbol, "No signal", float(df["close"].iloc[-1]))
            return self._no_trade(symbol, "No strategy edge", regime.regime.value)

        rankings = self.ai.rank_signals(
            signals,
            regime_confidence=regime.confidence,
            volatility_pct=regime.volatility_pct,
        )
        top = rankings[0]
        sig = next(s for s in signals if s.strategy == top.strategy)

        size_in = SizeInput(
            equity=self.portfolio.state.equity,
            entry=sig.entry,
            stop_loss=sig.stop_loss,
            volatility_pct=regime.volatility_pct,
            portfolio_heat_pct=self.portfolio.state.to_risk_state().portfolio_heat_pct,
            risk_multiplier=top.position_size_factor,
        )
        size = self.sizing.compute(size_in, SizingMethod.PORTFOLIO)

        proposal = TradeProposal(
            symbol=symbol, asset_class="equity", side="long",
            entry=sig.entry, stop_loss=sig.stop_loss, take_profit=sig.take_profit,
            qty=size.qty, confidence=top.ai_confidence, strategy=sig.strategy,
            regime=regime.regime.value, liquidity_score=0.85,
            volatility_pct=regime.volatility_pct,
            correlation_bucket=sig.strategy,
        )

        risk_decision = await self.risk.evaluate_trade(
            proposal,
            risk_state,
            portfolio=self.portfolio,
            market_data=self.data,
            trading_mode=self.cfg.trading_mode,
            regime_recommended=regime.recommended_strategies,
            lab_enabled=lab_enabled,
        )
        risk_summary = risk_decision.reason
        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        await crce.record(
            event_type=EventType.RISK_EVALUATION,
            action="EVALUATE",
            symbol=symbol,
            decision="ALLOW" if risk_decision.approved else "DENY",
            reason=risk_summary,
            portfolio=self.portfolio,
            risk_state={"verdict": risk_decision.verdict, "qty": risk_decision.final_quantity},
            system_state=(await self._system_state_name()),
        )
        trade_log(
            symbol=symbol,
            strategy=sig.strategy,
            action="RISK_CHECK",
            confidence=top.ai_confidence,
            risk_check=risk_summary,
            result=risk_decision.verdict,
        )

        decision = {
            "symbol": symbol,
            "action": "BUY" if risk_decision.approved else "REJECTED",
            "mode": self.cfg.trading_mode,
            "regime": regime.regime.value,
            "strategy": sig.strategy,
            "ai_confidence": top.ai_confidence,
            "entry": sig.entry,
            "stop_loss": sig.stop_loss,
            "take_profit": sig.take_profit,
            "qty": round(risk_decision.final_quantity, 2),
            "sizing": {"method": size.method, "detail": size.detail, "risk_pct": size.risk_pct},
            "risk_verdict": risk_decision.verdict,
            "risk_reason": risk_decision.reason,
            "risk_checks": [{"name": c.name, "passed": bool(c.passed), "detail": c.detail} for c in risk_decision.checks],
            "data_quality": {"score": dq.score, "issues": dq.issues},
            "ai_explanation": top.explanation,
        }

        self.journal.record(
            symbol=symbol,
            action=decision["action"],
            regime=regime.regime.value,
            strategy=sig.strategy,
            entry_reason=top.explanation[0] if top.explanation else "Signal",
            risk_score=size.risk_pct,
            confidence=top.ai_confidence,
            position_size=risk_decision.final_quantity,
            entry_price=sig.entry,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            risk_checks=decision["risk_checks"],
            mode=self.cfg.trading_mode,
        )

        if risk_decision.approved and risk_decision.final_quantity > 0:
            order_id = make_order_id(
                symbol,
                sig.strategy,
                time_bucket_minutes=self.cfg.idempotency_bucket_minutes,
            )
            result = await self.execution.place_order(
                OrderRequest(
                    symbol=symbol,
                    side="long",
                    qty=risk_decision.final_quantity,
                    order_type=OrderType.MARKET,
                    stop_price=sig.stop_loss,
                    take_profit=sig.take_profit,
                    strategy=sig.strategy,
                    client_order_id=order_id,
                ),
                sig.entry,
            )
            decision["execution"] = {
                "client_order_id": result.client_order_id,
                "broker_order_id": result.broker_order_id,
                "status": result.status.value,
                "filled_qty": result.filled_qty,
                "avg_price": result.avg_price,
                "slippage_bps": result.slippage_bps,
                "message": result.message,
                "stop_order_id": result.raw.get("stop_order_id"),
            }
            if result.status.value in ("filled", "partial"):
                if self.cfg.trading_mode != "shadow":
                    pos = PositionView(
                        symbol=symbol,
                        qty=result.filled_qty or risk_decision.final_quantity,
                        entry=result.avg_price,
                        stop_loss=sig.stop_loss,
                        take_profit=sig.take_profit,
                        strategy=sig.strategy,
                        unrealized_pnl=0,
                        risk_pct=size.risk_pct,
                        broker_order_id=result.broker_order_id,
                        stop_order_id=result.raw.get("stop_order_id", ""),
                    )
                    await self.portfolio.record_fill(pos, confidence=top.ai_confidence)
            elif result.status.value == "rejected":
                decision["action"] = "REJECTED"
                decision["risk_reason"] = result.message

        audit("trade_decision", symbol=symbol, action=decision["action"])
        self.decisions.insert(0, decision)
        return decision

    def _risk_dashboard_gate(self) -> str | None:
        """Unified risk gate for manual and autonomous paths."""
        risk = self.risk_dashboard.compute()
        if risk["status"] in (RiskStatus.HALTED.value, RiskStatus.DANGER.value):
            return f"Risk status {risk['status']} — new trades blocked"
        return None

    async def _system_state_name(self) -> str:
        from services.icb.engine import icb

        status = await icb.status({"portfolio": self.portfolio, "trading_mode": self.cfg.trading_mode})
        return status["system_state"]

    def _risk_state(self, dq_score: float, safe_mode: bool) -> AdvancedRiskState:
        base = self.portfolio.state.to_risk_state()
        ps = self.portfolio.state
        lab_disabled = {n for n, p in self.strategy_lab.performance.items() if not p.enabled}
        return AdvancedRiskState(
            equity=base.equity, cash=base.cash,
            daily_pnl=base.daily_pnl, weekly_pnl=base.weekly_pnl,
            monthly_pnl=ps.monthly_pnl,
            peak_equity=base.peak_equity or base.equity,
            open_positions=base.open_positions,
            portfolio_heat_pct=base.portfolio_heat_pct,
            consecutive_losses=base.consecutive_losses,
            emergency_halt=ps.emergency_halt,
            circuit_breaker=ps.circuit_breaker or safe_mode,
            black_swan_mode=ps.black_swan_mode,
            disabled_strategies=lab_disabled,
            safe_mode=safe_mode,
            data_quality_score=dq_score,
        )

    def _no_trade(self, symbol: str, reason: str, regime: str) -> dict:
        d = {"symbol": symbol, "action": "NO_TRADE", "reason": reason, "regime": regime}
        self.decisions.insert(0, d)
        return d

    def run_backtest(self, symbol: str, strategy: str | None = None) -> dict:
        df = self.data.synthetic_ohlcv(symbol, bars=800)
        r = self.backtest.run_full_validation(symbol, df, strategy)
        return {
            "strategy": r.strategy, "symbol": r.symbol,
            "total_trades": r.total_trades, "win_rate": r.win_rate,
            "net_return_pct": r.net_return_pct, "sharpe": r.sharpe,
            "sortino": r.sortino, "calmar": r.calmar,
            "profit_factor": r.profit_factor, "max_drawdown": r.max_drawdown,
            "expectancy": r.expectancy, "passed_validation": r.passed_validation,
            "walk_forward_passed": r.walk_forward_passed,
            "monte_carlo_passed": r.monte_carlo_passed,
            "rejection_reasons": r.rejection_reasons,
        }

    async def readiness_report(self) -> dict:
        bt = self.run_backtest("RELIANCE")
        shadow = self.execution.shadow_report()
        health = await self.watchdog.check_all()
        live_blockers = await self.execution.live_blockers()
        report = self.golive.evaluate(
            backtest=bt, shadow=shadow, risk_healthy=not health.safe_mode,
            data_quality=1.0 if await self.data.verify_real_data() else 0.0,
            watchdog_ok=health.ok,
            strategy_scores={s["name"]: s["win_rate"] for s in self.strategy_lab.ranking()},
        )
        from services.chaos.live_gate import ChaosLiveGate

        chaos_status = ChaosLiveGate.status()
        blockers: list[str] = []
        seen: set[str] = set()
        for item in list(report.blockers) + live_blockers:
            if item not in seen:
                seen.add(item)
                blockers.append(item)
        hard_live_clear = len(live_blockers) == 0
        overall = report.overall_passed and hard_live_clear
        return {
            "overall_passed": overall,
            "live_allowed": overall and self.cfg.enable_live_execution,
            "recommendation": report.recommendation if overall else "NOT READY — " + "; ".join(blockers[:3]),
            "blockers": blockers,
            "chaos_gate_status": chaos_status,
            "chaos_gate": chaos_status,
            "categories": [
                {"name": c.name, "score": c.score, "passed": bool(c.passed), "details": c.details}
                for c in report.categories
            ],
        }

    async def activate_kill_switch(self) -> dict:
        """Full kill switch — irreversible until admin reset."""
        from services.control.halt import set_emergency_halt
        from services.icb.actions import ICBAction
        from services.icb.engine import icb

        icb_result = await icb.authorize(
            ICBAction.KILL_SWITCH,
            {"portfolio": self.portfolio, "trading_mode": self.cfg.trading_mode},
        )
        if not icb_result.allowed and icb_result.decision != "EMERGENCY_LOCK":
            return {"ok": False, "message": icb_result.reason}

        await self.autonomous.stop()
        result = await self.execution.activate_kill_switch()
        await icb.activate_emergency_lock("Kill switch activated")
        await set_emergency_halt(True)
        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        await crce.record(
            event_type=EventType.KILL_SWITCH_TRIGGERED,
            action="ADMIN_KILL_SWITCH",
            decision="EXECUTED",
            reason=f"cancelled={result['cancelled']} flattened={result['flattened']}",
            portfolio=self.portfolio,
            system_state="EMERGENCY_LOCK",
        )
        await self.refresh_control_cache()
        await self.alerts.send(
            "KILL SWITCH",
            f"EMERGENCY_LOCK — cancelled {result['cancelled']}, flattened {result['flattened']}",
            "critical",
        )
        return {**result, "system_state": "EMERGENCY_LOCK", "recovery": "admin_reset_required"}

    async def resume_trading(self) -> dict:
        from services.control.halt import set_emergency_halt
        from services.icb.actions import ICBAction
        from services.icb.engine import icb
        from services.icb.system_state import get_kill_switch_latched

        if await get_kill_switch_latched():
            return await self.admin_reset_kill_switch()

        icb_result = await icb.authorize(
            ICBAction.RESUME,
            {"portfolio": self.portfolio, "trading_mode": self.cfg.trading_mode},
        )
        if not icb_result.allowed:
            return {"ok": False, "message": icb_result.reason, "trading_halted": True}

        if not self.portfolio.is_trading_halted():
            return {"ok": True, "message": "Trading already active", "trading_halted": False}
        self.portfolio.resume_trading()
        await self.portfolio.persist()
        await set_emergency_halt(False)
        await icb.recover_safe_mode()
        await self.refresh_control_cache()
        return {
            "ok": True,
            "message": "Emergency halt cleared — trading resumed",
            "trading_halted": False,
        }

    async def admin_reset_kill_switch(self) -> dict:
        from services.control.halt import set_emergency_halt
        from services.icb.engine import icb
        from services.icb.system_state import get_kill_switch_latched

        if not await get_kill_switch_latched():
            return {"ok": False, "message": "System is not in EMERGENCY_LOCK"}

        reset = await icb.admin_reset_emergency()
        if not reset.get("ok"):
            return reset
        self.portfolio.resume_trading()
        await self.portfolio.persist()
        await set_emergency_halt(False)
        await self.refresh_control_cache()
        return {
            "ok": True,
            "message": "Kill switch cleared — trading resumed. Start autonomous when ready.",
            "system_state": self.cfg.trading_mode,
            "trading_halted": False,
        }

    async def emergency_flatten(self) -> dict:
        from services.control.halt import set_emergency_halt
        from services.icb.engine import icb

        self.portfolio.enter_black_swan()
        await self.portfolio.persist()
        await self.autonomous.stop()
        result = await self.execution.activate_kill_switch()
        await icb.activate_emergency_lock("Emergency flatten")
        await set_emergency_halt(True)
        await self.refresh_control_cache()
        await self.alerts.send(
            "EMERGENCY",
            f"Flatten all — {result['flattened']} positions",
            "critical",
        )
        return {
            "flattened": result["flattened"],
            "black_swan": True,
            "halted": True,
            "system_state": "EMERGENCY_LOCK",
        }

    def dashboard(self) -> dict:
        m = self.portfolio.metrics()
        self.equity_curve.append({"equity": m["equity"], "ts": __import__("datetime").datetime.now().isoformat()})
        self.equity_curve = self.equity_curve[-100:]
        return {
            "portfolio": m,
            "mode": self.cfg.trading_mode,
            "principles": [
                "ICB → Risk → Execution — no bypass",
                "Broker is source of truth",
                "CRCE audit only",
            ],
            "equity_curve": self.equity_curve,
            "recent_decisions": self.decisions[:20],
            "strategy_ranking": self.strategy_lab.ranking(),
            "shadow_report": self.execution.shadow_report(),
            "journal_weekly": self.journal.weekly_report(),
            "enabled_strategies": self.strategy_lab.enabled_strategies(),
        }
