"""Central trading orchestrator — production pipeline v2."""

from __future__ import annotations

from services.ai.ranking import AISupportLayer
from services.alerts.notifier import AlertNotifier
from services.backtest.professional import ProfessionalBacktester
from services.brokers.base import OrderRequest, OrderType
from services.data_quality.engine import DataQualityEngine
from services.execution.router import ExecutionRouter
from services.golive.gate import GoLiveGate
from services.journal.service import TradeJournal
from services.market_data.service import MarketDataService, RealDataRequired
from services.portfolio.manager import PortfolioManager, PositionView
from services.regime.detector import RegimeDetector
from services.risk.advanced import AdvancedRiskEngine, AdvancedRiskState
from services.risk.engine import TradeProposal
from services.sizing.engine import PositionSizingEngine, SizeInput, SizingMethod
from services.strategy_lab.registry import StrategyLab
from services.strategies.engine import StrategyEngine
from services.watchdog.service import WatchdogService
from shared.config import get_settings
from shared.logging import audit


class TradingOrchestrator:
    def __init__(self) -> None:
        self.cfg = get_settings()
        self.data = MarketDataService()
        self.regime = RegimeDetector()
        self.strategies = StrategyEngine()
        self.strategy_lab = StrategyLab()
        self.ai = AISupportLayer()
        self.risk = AdvancedRiskEngine()
        self.sizing = PositionSizingEngine()
        self.portfolio = PortfolioManager()
        self.execution = ExecutionRouter(portfolio=self.portfolio, market_data=self.data)
        self.data_quality = DataQualityEngine(
            min_score=self.cfg.min_data_quality_score,
            max_stale_sec=self.cfg.max_stale_feed_seconds,
        )
        self.watchdog = WatchdogService()
        self.journal = TradeJournal()
        self.backtest = ProfessionalBacktester()
        self.golive = GoLiveGate()
        self.alerts = AlertNotifier()
        self.decisions: list[dict] = []
        self.equity_curve: list[dict] = []

    async def startup(self) -> None:
        from services.brokers.kite_auth import kite_auth

        await self.portfolio.load()
        await kite_auth.startup()
        self.data._real_data_ok = None
        await self.execution.connect()

    async def analyze_symbol(self, symbol: str) -> dict:
        if self.portfolio.state.emergency_halt or self.portfolio.state.black_swan_mode:
            return self._no_trade(
                symbol,
                "Emergency halt active — trading suspended",
                "black_swan" if self.portfolio.state.black_swan_mode else "halted",
            )

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

        enabled = self.strategy_lab.enabled_strategies()
        signals = self.strategies.scan(symbol, df, regime.regime.value, enabled or regime.recommended_strategies)
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

        risk_decision = self.risk.evaluate_advanced(proposal, risk_state)

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
            "qty": round(risk_decision.approved_qty, 2),
            "sizing": {"method": size.method, "detail": size.detail, "risk_pct": size.risk_pct},
            "risk_verdict": risk_decision.verdict.value,
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
            position_size=risk_decision.approved_qty,
            entry_price=sig.entry,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            risk_checks=decision["risk_checks"],
            mode=self.cfg.trading_mode,
        )

        if risk_decision.approved and risk_decision.approved_qty > 0:
            if not sig.stop_loss or sig.stop_loss >= sig.entry:
                decision["action"] = "REJECTED"
                decision["risk_reason"] = "Invalid stop-loss — must be below entry"
            else:
                result = await self.execution.submit(
                    OrderRequest(
                        symbol=symbol, side="long", qty=risk_decision.approved_qty,
                        order_type=OrderType.MARKET, stop_price=sig.stop_loss,
                        take_profit=sig.take_profit, strategy=sig.strategy,
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
                    pos = PositionView(
                        symbol=symbol,
                        qty=result.filled_qty,
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

    def _risk_state(self, dq_score: float, safe_mode: bool) -> AdvancedRiskState:
        base = self.portfolio.state.to_risk_state()
        ps = self.portfolio.state
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
            disabled_strategies={n for n, p in self.strategy_lab.performance.items() if not p.enabled},
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
        blockers = list(report.blockers) + live_blockers
        overall = report.overall_passed and not live_blockers
        return {
            "overall_passed": overall,
            "live_allowed": overall and self.cfg.enable_live_execution,
            "recommendation": report.recommendation if overall else "NOT READY — " + "; ".join(blockers[:3]),
            "blockers": blockers,
            "categories": [
                {"name": c.name, "score": c.score, "passed": bool(c.passed), "details": c.details}
                for c in report.categories
            ],
        }

    async def emergency_flatten(self) -> dict:
        self.portfolio.enter_black_swan()
        await self.portfolio.persist()
        n = await self.execution.flatten_all()
        await self.portfolio.clear_after_flatten()
        await self.alerts.send("EMERGENCY", f"Flatten all — {n} orders", "critical")
        return {"flattened": n, "black_swan": True}

    def dashboard(self) -> dict:
        m = self.portfolio.metrics()
        self.equity_curve.append({"equity": m["equity"], "ts": __import__("datetime").datetime.now().isoformat()})
        self.equity_curve = self.equity_curve[-100:]
        return {
            "portfolio": m,
            "mode": self.cfg.trading_mode,
            "principles": [
                "Capital preservation first",
                "Risk overrides AI and signals",
                "Missing trades is acceptable",
            ],
            "equity_curve": self.equity_curve,
            "recent_decisions": self.decisions[:20],
            "strategy_ranking": self.strategy_lab.ranking(),
            "shadow_report": self.execution.shadow_report(),
            "journal_weekly": self.journal.weekly_report(),
            "enabled_strategies": self.strategy_lab.enabled_strategies(),
        }
