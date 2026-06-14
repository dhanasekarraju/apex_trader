"""Chaos scenario definitions — deterministic failure injection."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ScenarioCategory(str, Enum):
    BROKER = "broker"
    NETWORK = "network"
    MARKET = "market"
    SYSTEM = "system"
    STATE_CORRUPTION = "state_corruption"


class LatencyProfile(str, Enum):
    NORMAL = "NORMAL"
    STRESSED = "STRESSED"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class ChaosScenario:
    id: str
    name: str
    category: ScenarioCategory
    seed: int = 42
    latency_profile: LatencyProfile = LatencyProfile.NORMAL
    fault_config: dict[str, Any] = field(default_factory=dict)
    expect_kill_switch: bool = False
    expect_safe_mode: bool = False
    expect_deny: bool = False


CHAOS_SCENARIOS: list[ChaosScenario] = [
    # A. Broker failure modes
    ChaosScenario("broker_rejection_spike", "Order rejection spike", ScenarioCategory.BROKER,
                  seed=101, fault_config={"broker_mode": "rejection_spike", "reject_rate": 0.9}),
    ChaosScenario("broker_delayed_response", "Delayed order response", ScenarioCategory.BROKER,
                  seed=102, latency_profile=LatencyProfile.DEGRADED,
                  fault_config={"broker_mode": "delayed_response", "delay_ms": 2000}),
    ChaosScenario("broker_partial_fill", "Partial fills only", ScenarioCategory.BROKER,
                  seed=103, fault_config={"broker_mode": "partial_fill", "fill_ratio": 0.5}),
    ChaosScenario("broker_duplicate_fill", "Duplicate fill events", ScenarioCategory.BROKER,
                  seed=104, fault_config={"broker_mode": "duplicate_fill"}),
    ChaosScenario("broker_missing_confirm", "Missing execution confirmation", ScenarioCategory.BROKER,
                  seed=105, fault_config={"broker_mode": "missing_confirmation"}),
    ChaosScenario("broker_disconnect", "Websocket disconnect during trade", ScenarioCategory.BROKER,
                  seed=106, fault_config={"broker_mode": "disconnect", "disconnect_on_order": True}),
    # B. Network failure modes
    ChaosScenario("network_latency_spike", "API latency spike", ScenarioCategory.NETWORK,
                  seed=201, latency_profile=LatencyProfile.CRITICAL,
                  fault_config={"inject_latency": True}),
    ChaosScenario("network_intermittent", "Intermittent disconnects", ScenarioCategory.NETWORK,
                  seed=202, fault_config={"intermittent_fail_rate": 0.3}),
    ChaosScenario("network_packet_loss", "Packet loss simulation", ScenarioCategory.NETWORK,
                  seed=203, fault_config={"packet_loss_rate": 0.4}),
    ChaosScenario("network_api_timeout_burst", "API timeout burst", ScenarioCategory.NETWORK,
                  seed=204, fault_config={"api_timeout_burst": True}, expect_deny=True),
    # C. Market failure modes
    ChaosScenario("market_illiquidity", "No fills — illiquidity", ScenarioCategory.MARKET,
                  seed=301, fault_config={"broker_mode": "illiquidity"}),
    ChaosScenario("market_spread_widen", "Spread widening", ScenarioCategory.MARKET,
                  seed=302, fault_config={"broker_mode": "spread_widen", "spread_bps": 200}),
    ChaosScenario("market_gap_down", "Gap-down simulation", ScenarioCategory.MARKET,
                  seed=303, fault_config={"price_gap_pct": -5.0}),
    ChaosScenario("market_gap_up", "Gap-up simulation", ScenarioCategory.MARKET,
                  seed=304, fault_config={"price_gap_pct": 5.0}),
    ChaosScenario("market_volatility_spike", "Extreme volatility spike", ScenarioCategory.MARKET,
                  seed=305, fault_config={"volatility_spike_pct": 12.0}),
    # D. System failure modes
    ChaosScenario("system_icb_delay", "ICB delayed response", ScenarioCategory.SYSTEM,
                  seed=401, latency_profile=LatencyProfile.CRITICAL,
                  fault_config={"icb_delay_ms": 6000}, expect_deny=True),
    ChaosScenario("system_risk_timeout", "Risk engine timeout", ScenarioCategory.SYSTEM,
                  seed=402, fault_config={"risk_timeout": True}, expect_deny=True),
    ChaosScenario("system_crce_failure", "CRCE write failure", ScenarioCategory.SYSTEM,
                  seed=403, fault_config={"crce_fail": True}, expect_safe_mode=True),
    ChaosScenario("system_crce_delay", "CRCE write delay", ScenarioCategory.SYSTEM,
                  seed=404, latency_profile=LatencyProfile.DEGRADED,
                  fault_config={"crce_delay_ms": 1500}),
    ChaosScenario("system_redis_crash", "Redis crash mid-trade", ScenarioCategory.SYSTEM,
                  seed=405, fault_config={"redis_crash": True}, expect_deny=True),
    ChaosScenario("system_db_latency", "DB latency injection", ScenarioCategory.SYSTEM,
                  seed=406, latency_profile=LatencyProfile.STRESSED,
                  fault_config={"db_latency_ms": 800}),
    # E. Partial state corruption
    ChaosScenario("state_portfolio_mismatch", "Portfolio mismatch with broker", ScenarioCategory.STATE_CORRUPTION,
                  seed=501, fault_config={"broker_mode": "position_mismatch"}, expect_deny=True),
    ChaosScenario("state_sl_not_confirmed", "SL not confirmed but order filled", ScenarioCategory.STATE_CORRUPTION,
                  seed=502, fault_config={"broker_mode": "sl_not_confirmed"}),
    ChaosScenario("state_duplicate_events", "Duplicated trade events", ScenarioCategory.STATE_CORRUPTION,
                  seed=503, fault_config={"broker_mode": "duplicate_fill"}),
    ChaosScenario("state_reconciliation_drift", "Reconciliation drift", ScenarioCategory.STATE_CORRUPTION,
                  seed=504, fault_config={"reconciliation_drift": True}, expect_deny=True),
]

SCENARIO_BY_ID = {s.id: s for s in CHAOS_SCENARIOS}
