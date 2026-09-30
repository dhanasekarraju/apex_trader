#!/usr/bin/env python3
"""Read-only preflight. Never changes broker orders, configuration, or capital."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select, text
from shared.config import get_settings
from shared.database import SessionLocal
from shared.models import KiteSession, Position, TradeRecord


async def run(expected_revision=None):
    import httpx
    cfg = get_settings()
    checks = []
    def check(name, passed, detail):
        checks.append({"check": name, "passed": bool(passed), "detail": detail})
    check("api_auth", len(cfg.api_access_key or "") >= 32, "API_ACCESS_KEY must be a rotated, strong value (minimum 32 characters)")
    check("route", cfg.app_base_path == "/apex-trader", "Expected dedicated /apex-trader route; do not replace Nautilus /apex")
    check("callback", cfg.kite_redirect_url == cfg.public_url.rstrip('/') + '/apex-trader/api/kite/callback', "Kite redirect must match the dedicated route and developer application")
    check("auto_start", not cfg.autonomous_auto_start, "Automatic entry startup must remain disabled during this rollout")
    token = cfg.kite_access_token
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
            open_positions = list((await session.execute(select(Position.id).where(Position.status == "open"))).scalars())
            unresolved = list((await session.execute(select(TradeRecord.id).where(TradeRecord.status.in_(["pending","submitted","unknown","filled","sl_placed"])))).scalars())
            saved = await session.get(KiteSession, 1)
            if saved and saved.access_token:
                token = saved.access_token
        check("database", True, "Read-only database query succeeded")
        check("ledger_flat", not open_positions and not unresolved, f"Open positions={len(open_positions)}, unresolved/open trade records={len(unresolved)}; audit before restart")
    except Exception as exc:
        check("database", False, type(exc).__name__ + ": database/ledger check failed; details withheld to protect credentials")
    try:
        from kiteconnect import KiteConnect
        if not cfg.kite_api_key or not token:
            raise RuntimeError("No saved broker session")
        kite = KiteConnect(api_key=cfg.kite_api_key, timeout=10)
        kite.set_access_token(token)
        positions = await asyncio.to_thread(kite.positions)
        orders = await asyncio.to_thread(kite.orders)
        exposure = sum(1 for p in positions.get("net", []) if float(p.get("quantity", 0)))
        pending = sum(1 for o in orders if o.get("status") not in ("COMPLETE", "CANCELLED", "REJECTED"))
        check("broker_flat", exposure == 0 and pending == 0, f"Broker exposure rows={exposure}, pending orders={pending}; includes other apps/manual activity")
    except Exception as exc:
        check("broker_flat", False, type(exc).__name__ + ": broker flatness not confirmed")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"http://127.0.0.1:{cfg.api_port}/api/ready", headers={"X-API-Key": cfg.api_access_key})
            body = response.json()
            ready = body.get("data", body)
            check("running_process", response.status_code == 200 and ready.get("process_ready"), "Running API recovery, loops and ownership must be ready")
            check("reconciliation", ready.get("reconciliation", {}).get("status") == "OK", "Running process must report OK reconciliation")
            response = await client.get(f"http://127.0.0.1:{cfg.api_port}/api/health")
            body = response.json(); health = body.get("data", body)
            check("revision", bool(expected_revision) and health.get("revision") == expected_revision, "Running image must match the explicitly supplied expected commit")
    except Exception as exc:
        check("running_process", False, type(exc).__name__ + ": API readiness could not be confirmed")
    from services.golive.evidence import validation_blockers
    from services.shadow.engine import ShadowEngine
    blockers = validation_blockers(cfg)
    check("real_validation", not blockers, '; '.join(blockers) or 'Fresh real-data validation present')
    shadow = ShadowEngine().weekly_report()
    check("shadow_evidence", shadow.get("active_days", 0) >= cfg.golive_min_shadow_days and shadow.get("completed_trades", 0) >= cfg.golive_min_completed_trades and shadow.get("total_shadow_pnl", 0) > 0, "Required completed profitable shadow history must be present; do not fabricate or bypass it")
    passed = all(item["passed"] for item in checks)
    print(json.dumps({"passed": passed, "checks": checks, "note": "A passed preflight is a prerequisite, not proof of profitability or an instruction to start trading."}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--expected-revision', required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.expected_revision)))
