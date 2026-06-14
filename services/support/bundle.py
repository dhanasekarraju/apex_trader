"""Support incident bundle — status + recent logs for troubleshooting."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from shared.config import get_settings

_ROOT = Path(__file__).resolve().parents[2]


def _tail_lines(path: Path, n: int = 80) -> list[str]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:] if len(lines) > n else lines
    except OSError:
        return []


def _tail_jsonl(path: Path, n: int = 50) -> list[dict]:
    rows: list[dict] = []
    for line in _tail_lines(path, n):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"raw": line})
    return rows


async def build_incident_bundle(*, orchestrator) -> dict:
    """Collect live system status and recent log tails for support."""
    from services.chaos.live_gate import ChaosLiveGate
    from services.icb.engine import icb

    cfg = get_settings()
    chaos = ChaosLiveGate.status()
    icb_status = await icb.status(
        {"portfolio": orchestrator.portfolio, "trading_mode": cfg.trading_mode},
    )
    autonomous = await orchestrator.autonomous.status()
    readiness = await orchestrator.readiness_report()

    try:
        from services.brokers.kite_auth import kite_auth
        kite = await kite_auth.get_status()
    except Exception as exc:
        kite = {"error": str(exc)}

    log_dir = _ROOT / cfg.log_dir if not Path(cfg.log_dir).is_absolute() else Path(cfg.log_dir)
    data_dir = _ROOT / "data"

    bundle = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "system": {
            "trading_mode": cfg.trading_mode,
            "env": cfg.env,
            "autonomous_enabled": cfg.autonomous_enabled,
            "enable_live_execution": cfg.enable_live_execution,
            "golive_approved": cfg.golive_approved,
            "enforce_market_hours": cfg.enforce_market_hours,
        },
        "icb": icb_status,
        "chaos_gate": chaos,
        "readiness_summary": {
            "overall_passed": readiness.get("overall_passed"),
            "live_allowed": readiness.get("live_allowed"),
            "blockers": readiness.get("blockers", [])[:10],
        },
        "autonomous": autonomous,
        "kite": kite,
        "portfolio": orchestrator.portfolio.metrics(),
        "recent_trades": _tail_jsonl(log_dir / "trades.jsonl", 80),
        "recent_autonomous": _tail_jsonl(data_dir / "logs" / "autonomous.jsonl", 40),
        "recent_icb_audit": _tail_jsonl(data_dir / "icb" / "audit.jsonl", 40),
        "recent_errors": _tail_lines(log_dir / "errors.log", 40),
        "recent_compliance_events": _tail_jsonl(data_dir / "compliance" / "event_log.jsonl", 30),
    }
    return bundle


def bundle_as_text(bundle: dict) -> str:
    """Human-readable text for clipboard paste."""
    lines = [
        f"Apex Trader Incident Bundle — {bundle.get('generated_at', '')}",
        "=" * 60,
        "",
        "SYSTEM",
        json.dumps(bundle.get("system", {}), indent=2),
        "",
        "ICB",
        json.dumps(bundle.get("icb", {}), indent=2),
        "",
        "CHAOS GATE",
        json.dumps(bundle.get("chaos_gate", {}), indent=2),
        "",
        "READINESS",
        json.dumps(bundle.get("readiness_summary", {}), indent=2),
        "",
        "AUTONOMOUS",
        json.dumps(bundle.get("autonomous", {}), indent=2),
        "",
        "KITE",
        json.dumps(bundle.get("kite", {}), indent=2),
        "",
        "PORTFOLIO",
        json.dumps(bundle.get("portfolio", {}), indent=2),
        "",
        "RECENT TRADES (last entries)",
        json.dumps(bundle.get("recent_trades", [])[-20:], indent=2, default=str),
        "",
        "RECENT ERRORS",
        "\n".join(bundle.get("recent_errors", [])),
    ]
    return "\n".join(lines)
