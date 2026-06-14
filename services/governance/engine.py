"""
Strategy Governance Engine (SGE) — highest authority on strategy lifecycle.

Answers: "Should this strategy be allowed to trade in current market conditions?"
"""

from __future__ import annotations

from datetime import datetime, timezone

from services.governance.models import GovernanceDecision, StrategyRecord
from services.governance.policy import evaluate_strategy, is_tradeable_state
from services.governance.states import DEFAULT_THROTTLE_FACTOR, StrategyState
from services.governance.store import GovernanceStore
from services.strategies.engine import STRATEGY_REGISTRY
from shared.config import get_settings
from shared.logging import audit


class StrategyGovernanceEngine:
    """Governs strategy lifecycle — enable, disable, throttle, pause, kill."""

    def __init__(self) -> None:
        self.cfg = get_settings()
        self.store = GovernanceStore()
        self._records: dict[str, StrategyRecord] = {}
        self._loaded = False

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        stored = await self.store.load()
        for name in STRATEGY_REGISTRY:
            self._records[name] = stored.get(name, StrategyRecord(name=name))
        for name, rec in stored.items():
            if name not in self._records:
                self._records[name] = rec
        self._loaded = True

    async def resolve_allowed(
        self,
        *,
        symbol: str,
        regime: str,
        regime_recommended: list[str],
        lab_enabled: list[str] | None = None,
    ) -> tuple[list[str], dict[str, GovernanceDecision]]:
        """Merge lab + governance + regime policy into allowed strategy list."""
        await self.ensure_loaded()
        lab_set = set(lab_enabled or list(STRATEGY_REGISTRY.keys()))
        allowed: list[str] = []
        decisions: dict[str, GovernanceDecision] = {}

        for name, record in self._records.items():
            decision = evaluate_strategy(
                record,
                regime=regime,
                regime_recommended=regime_recommended,
                lab_enabled=name in lab_set,
            )
            decisions[name] = decision
            if decision.allowed:
                allowed.append(name)

        await self._audit_batch(symbol=symbol, regime=regime, allowed=allowed, decisions=decisions)
        return allowed, decisions

    async def check_trade(
        self,
        strategy: str,
        *,
        regime: str,
        regime_recommended: list[str],
        lab_enabled: bool = True,
    ) -> GovernanceDecision:
        """Final governance gate before order placement."""
        await self.ensure_loaded()
        record = self._records.get(strategy, StrategyRecord(name=strategy))
        decision = evaluate_strategy(
            record,
            regime=regime,
            regime_recommended=regime_recommended,
            lab_enabled=lab_enabled,
        )
        await self._audit_decision(
            strategy,
            decision,
            action="CHECK_TRADE",
            regime=regime,
        )
        return decision

    def size_multiplier(self, strategy: str) -> float:
        record = self._records.get(strategy)
        if record is None:
            return 1.0
        if record.state == StrategyState.THROTTLED:
            return record.throttle_factor or DEFAULT_THROTTLE_FACTOR
        return 1.0

    async def set_state(
        self,
        strategy: str,
        state: StrategyState,
        *,
        reason: str = "",
        actor: str = "admin",
        throttle_factor: float | None = None,
    ) -> dict:
        await self.ensure_loaded()
        if strategy not in self._records and strategy not in STRATEGY_REGISTRY:
            return {"ok": False, "message": f"Unknown strategy: {strategy}"}

        record = self._records.setdefault(strategy, StrategyRecord(name=strategy))
        if record.state == StrategyState.KILLED and state != StrategyState.KILLED:
            return {"ok": False, "message": "KILLED strategies cannot be modified"}

        record.state = state
        record.reason = reason or f"Set to {state.value} by {actor}"
        record.updated_at = datetime.now(timezone.utc).isoformat()
        record.updated_by = actor
        if throttle_factor is not None:
            record.throttle_factor = throttle_factor
        elif state == StrategyState.THROTTLED and record.throttle_factor <= 0:
            record.throttle_factor = DEFAULT_THROTTLE_FACTOR

        await self.store.save(self._records)
        audit("sge_state_change", strategy=strategy, state=state.value, actor=actor, reason=record.reason)

        decision = GovernanceDecision(
            allowed=is_tradeable_state(state),
            state=state,
            reason=record.reason,
            size_multiplier=record.throttle_factor if state == StrategyState.THROTTLED else 1.0,
        )
        await self._audit_decision(strategy, decision, action="SET_STATE", actor=actor)
        return {"ok": True, "strategy": strategy, "record": record.to_dict()}

    async def record_outcome(self, strategy: str, pnl: float) -> None:
        """Performance feedback — may auto-disable underperforming strategies."""
        await self.ensure_loaded()
        record = self._records.setdefault(strategy, StrategyRecord(name=strategy))
        if record.state == StrategyState.KILLED:
            return

        record.trades += 1
        if pnl > 0:
            record.wins += 1
        record.total_pnl += pnl
        record.win_rate = record.wins / record.trades * 100 if record.trades else 0.0
        record.updated_at = datetime.now(timezone.utc).isoformat()
        record.updated_by = "performance"

        if (
            record.trades >= self.cfg.strategy_disable_min_trades
            and record.win_rate < self.cfg.strategy_disable_win_rate
            and record.state == StrategyState.ACTIVE
        ):
            record.state = StrategyState.DISABLED
            record.reason = (
                f"Auto-disabled: win rate {record.win_rate:.1f}% "
                f"below {self.cfg.strategy_disable_win_rate}%"
            )
            audit("sge_auto_disable", strategy=strategy, win_rate=record.win_rate)

        await self.store.save(self._records)

        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        await crce.record(
            event_type=EventType.STRATEGY_PERFORMANCE_UPDATE,
            action="RECORD_OUTCOME",
            symbol="",
            decision=record.state.value,
            reason=f"pnl={pnl:.2f} win_rate={record.win_rate:.1f}%",
            metadata={"strategy": strategy, "trades": record.trades, "total_pnl": record.total_pnl},
        )

    def disabled_for_risk(self) -> set[str]:
        """Strategies blocked at risk layer (not ACTIVE/THROTTLED)."""
        return {
            name
            for name, rec in self._records.items()
            if not is_tradeable_state(rec.state)
        }

    def status(self) -> dict:
        rows = sorted(self._records.values(), key=lambda r: r.name)
        return {
            "strategies": [r.to_dict() for r in rows],
            "active": [r.name for r in rows if r.state == StrategyState.ACTIVE],
            "throttled": [r.name for r in rows if r.state == StrategyState.THROTTLED],
            "paused": [r.name for r in rows if r.state == StrategyState.PAUSED],
            "disabled": [r.name for r in rows if r.state == StrategyState.DISABLED],
            "killed": [r.name for r in rows if r.state == StrategyState.KILLED],
        }

    async def _audit_batch(
        self,
        *,
        symbol: str,
        regime: str,
        allowed: list[str],
        decisions: dict[str, GovernanceDecision],
    ) -> None:
        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        denied = {k: v.reason for k, v in decisions.items() if not v.allowed}
        await crce.record(
            event_type=EventType.STRATEGY_GOVERNANCE_DECISION,
            action="RESOLVE_ALLOWED",
            symbol=symbol,
            decision="ALLOW" if allowed else "DENY",
            reason=f"allowed={len(allowed)} denied={len(denied)}",
            metadata={"regime": regime, "allowed": allowed, "denied": denied},
        )

    async def _audit_decision(
        self,
        strategy: str,
        decision: GovernanceDecision,
        *,
        action: str,
        symbol: str = "",
        regime: str = "",
        actor: str = "system",
    ) -> None:
        from services.compliance.events import EventType
        from services.compliance.recorder import crce

        await crce.record(
            event_type=EventType.STRATEGY_GOVERNANCE_DECISION,
            action=action,
            symbol=symbol,
            decision="ALLOW" if decision.allowed else "DENY",
            reason=decision.reason,
            metadata={
                "strategy": strategy,
                "governance_state": decision.state.value,
                "size_multiplier": decision.size_multiplier,
                "regime": regime,
                "actor": actor,
            },
        )


strategy_governance = StrategyGovernanceEngine()
