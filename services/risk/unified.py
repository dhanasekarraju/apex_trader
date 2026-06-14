"""Unified risk engine — sizing, limits, strategy rules, and entry validation."""

from __future__ import annotations

from dataclasses import dataclass

from services.governance.policy import evaluate_strategy
from services.governance.states import TRADEABLE_STATES
from services.market_data.service import MarketDataService
from services.portfolio.manager import PortfolioManager
from services.risk.advanced import AdvancedRiskEngine, AdvancedRiskState
from services.risk.engine import RiskCheck, RiskDecision, RiskVerdict, TradeProposal
from shared.config import get_settings
from shared.logging import audit


@dataclass
class UnifiedRiskDecision:
    verdict: str
    final_quantity: float
    reason: str
    checks: list[RiskCheck]
    size_multiplier: float = 1.0

    @property
    def approved(self) -> bool:
        return self.verdict in ("ALLOW", "REDUCE_SIZE")

    def to_risk_decision(self) -> RiskDecision:
        mapping = {
            "ALLOW": RiskVerdict.APPROVED,
            "REDUCE_SIZE": RiskVerdict.REDUCED,
            "REJECT": RiskVerdict.REJECTED,
        }
        return RiskDecision(
            mapping.get(self.verdict, RiskVerdict.REJECTED),
            self.final_quantity,
            self.checks,
            self.reason,
            self.size_multiplier,
        )


class UnifiedRiskEngine(AdvancedRiskEngine):
    """Single deterministic risk layer — no duplicate checks elsewhere."""

    async def evaluate_trade(
        self,
        proposal: TradeProposal,
        state: AdvancedRiskState,
        *,
        portfolio: PortfolioManager,
        market_data: MarketDataService,
        trading_mode: str,
        regime_recommended: list[str],
        lab_enabled: list[str],
    ) -> UnifiedRiskDecision:
        checks: list[RiskCheck] = []
        cfg = get_settings()

        gov_decision = await self._strategy_gate(
            proposal.strategy,
            proposal.regime,
            regime_recommended,
            lab_enabled,
        )
        if not gov_decision.allowed:
            checks.append(RiskCheck("strategy_governance", False, gov_decision.reason))
            return UnifiedRiskDecision("REJECT", 0, gov_decision.reason, checks)

        throttle = gov_decision.size_multiplier

        advanced = self.evaluate_advanced(proposal, state)
        checks.extend(advanced.checks)
        if not advanced.approved:
            return UnifiedRiskDecision(
                "REJECT",
                0,
                advanced.reason,
                checks,
                advanced.size_multiplier,
            )

        pre_ok, pre_msg, qty = await self._validate_entry(
            symbol=proposal.symbol,
            qty=advanced.approved_qty * advanced.size_multiplier * throttle,
            entry=proposal.entry,
            stop_loss=proposal.stop_loss,
            portfolio=portfolio,
            market_data=market_data,
            trading_mode=trading_mode,
        )
        if not pre_ok:
            checks.append(RiskCheck("entry_validation", False, pre_msg))
            return UnifiedRiskDecision("REJECT", 0, pre_msg, checks)

        final_qty = max(1, int(qty))
        verdict = "REDUCE_SIZE" if (advanced.size_multiplier < 1 or throttle < 1) else "ALLOW"
        return UnifiedRiskDecision(
            verdict,
            final_qty,
            advanced.reason or "Risk approved",
            checks,
            advanced.size_multiplier * throttle,
        )

    async def _strategy_gate(
        self,
        strategy: str,
        regime: str,
        regime_recommended: list[str],
        lab_enabled: list[str],
    ):
        from services.governance.engine import strategy_governance

        await strategy_governance.ensure_loaded()
        record = strategy_governance._records.get(strategy)
        if record is None:
            from services.governance.models import StrategyRecord

            record = StrategyRecord(name=strategy)
        if record.state not in TRADEABLE_STATES:
            from services.governance.models import GovernanceDecision

            return GovernanceDecision(False, record.state, record.reason or f"Strategy {record.state.value}")
        return evaluate_strategy(
            record,
            regime=regime,
            regime_recommended=regime_recommended,
            lab_enabled=strategy in lab_enabled,
        )

    async def _validate_entry(
        self,
        *,
        symbol: str,
        qty: float,
        entry: float,
        stop_loss: float,
        portfolio: PortfolioManager,
        market_data: MarketDataService,
        trading_mode: str,
    ) -> tuple[bool, str, int]:
        cfg = get_settings()
        approved_qty = max(0, int(qty))
        if approved_qty < 1:
            return False, "Quantity below 1 share", 0
        if stop_loss <= 0 or stop_loss >= entry:
            return False, "Stop-loss must be below entry", 0

        sym = symbol.upper()
        if any(p.symbol.upper() == sym for p in portfolio.state.positions):
            return False, f"Duplicate blocked — already holding {sym}", 0

        if trading_mode in ("live", "paper") and cfg.enforce_market_hours:
            from services.icb.engine import icb

            open_ok, msg = icb._market_open()
            if not open_ok:
                return False, msg, 0

        ltp = await self._reference_price(sym, entry, market_data)
        if ltp > 0 and entry > 0:
            deviation = abs(ltp - entry) / entry * 100
            if deviation > cfg.max_entry_deviation_pct:
                audit("risk_price_deviation", symbol=sym, entry=entry, ltp=ltp, deviation=deviation)
                return (
                    False,
                    f"Entry deviates {deviation:.1f}% from LTP (max {cfg.max_entry_deviation_pct}%)",
                    0,
                )
        return True, "Entry validation passed", approved_qty

    async def _reference_price(
        self,
        symbol: str,
        fallback: float,
        market_data: MarketDataService,
    ) -> float:
        if market_data.has_real_data_configured():
            ltps = await market_data.fetch_ltps([symbol])
            if symbol in ltps and ltps[symbol] > 0:
                return ltps[symbol]
        try:
            df = market_data.synthetic_ohlcv(symbol, bars=5)
            return float(df["close"].iloc[-1])
        except Exception:
            return fallback

    async def record_strategy_outcome(self, strategy: str, pnl: float) -> None:
        from services.governance.engine import strategy_governance

        await strategy_governance.record_outcome(strategy, pnl)
