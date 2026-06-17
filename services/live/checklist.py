"""Unified live-mode prerequisite checks."""

from __future__ import annotations

from services.chaos.live_gate import ChaosLiveGate
from services.compliance.store import EventStore
from shared.config import Settings, get_settings


def crce_blockers() -> list[str]:
    try:
        integrity = EventStore().verify_chain()
        if integrity.get("valid"):
            return []
        return [
            "CRCE integrity failure — repair with POST /api/compliance/repair-chain "
            f"(broken at index {integrity.get('broken_at_index', '?')})",
        ]
    except Exception as exc:
        return [f"CRCE check error: {exc}"]


def chaos_blockers(*, require_full_suite: bool = True) -> list[str]:
    ok, blockers = ChaosLiveGate.check_for_live(require_full_suite=require_full_suite)
    return [] if ok else list(blockers)


def live_capital_blockers(*, require_full_suite: bool = True) -> list[str]:
    """Hard gates for LIVE trading — chaos + CRCE."""
    blockers: list[str] = []
    blockers.extend(crce_blockers())
    blockers.extend(chaos_blockers(require_full_suite=require_full_suite))
    return blockers


def operator_soft_blockers(cfg: Settings | None = None) -> list[str]:
    """Advisory only when GOLIVE_APPROVED=true (shown, not enforced)."""
    cfg = cfg or get_settings()
    if cfg.golive_approved:
        return []
    return ["GOLIVE_APPROVED is false — operator must approve live trading"]
