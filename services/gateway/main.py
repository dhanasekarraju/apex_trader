"""Apex Trader API Gateway — FastAPI + institutional dashboard v2."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import asyncio
import json

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, field_validator

from services.control.trade_stream import recent_trade_events
from services.core.orchestrator import TradingOrchestrator
from services.gateway.auth import cors_allowed_origins, require_api_auth, require_ws_auth, resolve_api_access_key
from services.gateway.middleware import EnvelopeMiddleware, register_exception_handlers
from shared.config import get_settings
from shared.database import init_db
from shared.logging import audit, setup_logging
from shared.validation import normalize_symbol

UI_DIR = Path(__file__).resolve().parents[2] / "ui"
orch = TradingOrchestrator()
_refresh_task: asyncio.Task | None = None
_lifecycle_task: asyncio.Task | None = None
_autonomous_task: asyncio.Task | None = None
_chaos_run_task: asyncio.Task | None = None
_chaos_run_status: dict = {"running": False, "error": "", "finished_at": None}


async def _control_refresh_loop() -> None:
    """Background PnL + risk cache refresh every 3 seconds."""
    from services.control.actions import ControlAction
    from services.control.layer import control_layer
    from services.control.pnl_reset import maybe_reset_pnl_periods
    from services.control.reconciliation_state import is_reconciliation_degraded

    while True:
        cfg = get_settings()
        try:
            icl = await control_layer.allow(
                ControlAction.RESET_PNL,
                {"portfolio": orch.portfolio, "trading_mode": cfg.trading_mode},
            )
            if icl.allowed:
                await maybe_reset_pnl_periods(orch.portfolio)
            if await is_reconciliation_degraded():
                await orch.execution.retry_reconciliation()
            await orch.refresh_control_cache()
        except Exception as e:
            audit("control_refresh_failed", error=str(e))
        await asyncio.sleep(cfg.control_refresh_sec)


async def _lifecycle_loop() -> None:
    cfg = get_settings()
    while True:
        try:
            await orch.lifecycle.tick()
        except Exception as e:
            audit("lifecycle_tick_failed", error=str(e))
        await asyncio.sleep(cfg.lifecycle_poll_sec)


async def _autonomous_loop() -> None:
    cfg = get_settings()
    while True:
        try:
            result = await orch.autonomous.tick()
            if result.get("skipped"):
                audit("autonomous_tick_skipped", reason=result.get("skipped"))
            elif result.get("stats"):
                audit("autonomous_tick_done", **result.get("stats", {}))
        except Exception as e:
            audit("autonomous_tick_failed", error=str(e))
        await asyncio.sleep(cfg.autonomous_scan_interval_sec)


def _task_alive(task: asyncio.Task | None) -> bool:
    return task is not None and not task.done()


async def _warmup_dynamic_universe() -> None:
    cfg = get_settings()
    if cfg.watchlist_mode != "dynamic":
        return
    try:
        from services.autonomous.dynamic_universe import DynamicUniverseSelector

        snap = await DynamicUniverseSelector(market_data=orch.data).refresh()
        orch.autonomous.watchlist._last_universe = snap.to_dict()
        audit("dynamic_universe_warmup", source=snap.source, scan=len(snap.scan))
    except Exception as exc:
        audit("dynamic_universe_warmup_failed", error=str(exc))


def ensure_background_loops() -> None:
    """Start background loops if missing — survives partial startup failures."""
    global _refresh_task, _lifecycle_task, _autonomous_task
    if not _task_alive(_refresh_task):
        _refresh_task = asyncio.create_task(_control_refresh_loop())
        audit("background_loop_started", loop="control_refresh")
    if not _task_alive(_lifecycle_task):
        _lifecycle_task = asyncio.create_task(_lifecycle_loop())
        audit("background_loop_started", loop="lifecycle")
    if not _task_alive(_autonomous_task):
        _autonomous_task = asyncio.create_task(_autonomous_loop())
        audit("background_loop_started", loop="autonomous")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _refresh_task, _lifecycle_task, _autonomous_task
    setup_logging()
    startup_errors: list[str] = []
    try:
        await init_db()
    except Exception as e:
        startup_errors.append(f"init_db: {e}")
    try:
        from services.compliance.recorder import crce

        await crce.recover()
    except Exception as e:
        startup_errors.append(f"crce_recover: {e}")
    try:
        await orch.startup()
    except Exception as e:
        startup_errors.append(f"orchestrator_startup: {e}")
    ensure_background_loops()
    if get_settings().watchlist_mode == "dynamic":
        asyncio.create_task(_warmup_dynamic_universe())
    if startup_errors:
        audit("startup_degraded", errors=startup_errors)
    yield
    for task in (_refresh_task, _lifecycle_task, _autonomous_task):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    try:
        await orch.shutdown()
    except Exception as e:
        audit("shutdown_failed", error=str(e))


app = FastAPI(
    title="Apex Trader",
    description="Institutional algorithmic trading platform — capital preservation first",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)
app.add_middleware(EnvelopeMiddleware)
register_exception_handlers(app)

if UI_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=UI_DIR / "assets"), name="assets")


def _base_path() -> str:
    base = get_settings().app_base_path.strip()
    if not base:
        return ""
    if not base.startswith("/"):
        base = f"/{base}"
    return base.rstrip("/")


def _app_url(path: str = "/") -> str:
    base = _base_path()
    if path.startswith("/"):
        return f"{base}{path}" if base else path
    return f"{base}/{path}" if base else f"/{path}"


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    symbol: str

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return normalize_symbol(value)


class BacktestRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    symbol: str
    strategy: str | None = None

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return normalize_symbol(value)


class ModeRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    mode: Literal["paper", "shadow", "live"]


class GovernanceStateRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    state: Literal["ACTIVE", "THROTTLED", "PAUSED", "DISABLED", "KILLED"]
    reason: str = ""
    throttle_factor: float | None = None


@app.get("/")
async def index():
    index_path = UI_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(404, "UI not found")
    base = _base_path()
    api_key = resolve_api_access_key()
    html = index_path.read_text(encoding="utf-8")
    html = html.replace(
        "window.APEX_BASE = '';",
        f"window.APEX_BASE = {json.dumps(base)};",
    )
    html = html.replace(
        "window.APEX_API_KEY = '';",
        f"window.APEX_API_KEY = {json.dumps(api_key)};",
    )
    cfg = get_settings()
    poll_ms = int(max(cfg.ui_poll_interval_sec, 1.0) * 1000)
    html = html.replace(
        "window.APEX_UI_POLL_MS = 3000;",
        f"window.APEX_UI_POLL_MS = {poll_ms};",
    )
    return HTMLResponse(html)


@app.get("/api/health")
async def health():
    cfg = get_settings()
    return {
        "ok": True,
        "service": "apex-trader",
        "version": "2.0.0",
        "mode": cfg.trading_mode,
        "env": cfg.env,
        "live_enabled": cfg.enable_live_execution,
        "base_path": _base_path(),
        "public_url": cfg.public_url,
    }


@app.get("/api/dashboard")
async def dashboard():
    return orch.dashboard()


@app.get("/api/regime/{symbol}")
async def regime(symbol: str):
    try:
        sym = normalize_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    df = orch.data.synthetic_ohlcv(sym)
    r = orch.regime.analyze(df)
    return {
        "symbol": sym,
        "regime": r.regime.value,
        "confidence": r.confidence,
        "volatility_pct": r.volatility_pct,
        "trend_strength": r.trend_strength,
        "trade_allowed": r.trade_allowed,
        "recommended_strategies": r.recommended_strategies,
        "explanation": r.explanation,
    }


@app.post("/api/analyze", dependencies=[Depends(require_api_auth)])
async def analyze(req: AnalyzeRequest):
    return await orch.analyze_symbol(req.symbol)


@app.post("/api/backtest", dependencies=[Depends(require_api_auth)])
async def backtest(req: BacktestRequest):
    return orch.run_backtest(req.symbol, req.strategy)


@app.get("/api/strategies")
async def strategies():
    from services.governance.engine import strategy_governance
    from services.strategies.engine import STRATEGY_REGISTRY

    await strategy_governance.ensure_loaded()
    return {
        "strategies": list(STRATEGY_REGISTRY.keys()),
        "ranking": orch.strategy_lab.ranking(),
        "enabled": orch.strategy_lab.enabled_strategies(),
        "governance": strategy_governance.status(),
    }


@app.get("/api/governance/status")
async def governance_status():
    from services.governance.engine import strategy_governance

    await strategy_governance.ensure_loaded()
    return strategy_governance.status()


@app.post("/api/governance/strategy/{name}", dependencies=[Depends(require_api_auth)])
async def governance_set_state(name: str, req: GovernanceStateRequest):
    from services.icb.actions import ICBAction
    from services.icb.engine import icb
    from services.governance.engine import strategy_governance
    from services.governance.states import StrategyState

    icb_result = await icb.authorize(
        ICBAction.GOVERNANCE_CHANGE,
        {"portfolio": orch.portfolio, "trading_mode": get_settings().trading_mode, "strategy": name},
    )
    if not icb_result.allowed:
        raise HTTPException(403, icb_result.reason)

    await strategy_governance.ensure_loaded()
    result = await strategy_governance.set_state(
        name,
        StrategyState(req.state),
        reason=req.reason,
        actor="admin",
        throttle_factor=req.throttle_factor,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("message", "Governance update failed"))
    return result


@app.get("/api/risk/limits")
async def risk_limits():
    cfg = get_settings()
    return {
        "max_risk_per_trade_pct": cfg.max_risk_per_trade_pct,
        "max_daily_loss_pct": cfg.max_daily_loss_pct,
        "max_weekly_loss_pct": cfg.max_weekly_loss_pct,
        "max_monthly_loss_pct": cfg.max_monthly_loss_pct,
        "max_monthly_drawdown_pct": cfg.max_monthly_drawdown_pct,
        "max_portfolio_heat_pct": cfg.max_portfolio_heat_pct,
        "max_correlated_exposure_pct": cfg.max_correlated_exposure_pct,
        "max_sector_concentration_pct": cfg.max_sector_concentration_pct,
        "min_confidence_score": cfg.min_confidence_score,
        "max_open_positions": cfg.max_open_positions,
        "consecutive_loss_reduce": cfg.consecutive_loss_reduce,
        "consecutive_loss_halt": cfg.consecutive_loss_halt,
    }


@app.get("/api/readiness")
async def readiness():
    return await orch.readiness_report()


@app.get("/api/shadow/report")
async def shadow_report():
    return orch.execution.shadow_report()


@app.get("/api/journal/weekly")
async def journal_weekly():
    return orch.journal.weekly_report()


@app.get("/api/journal/monthly")
async def journal_monthly():
    return orch.journal.monthly_report()


@app.get("/api/watchdog/health")
async def watchdog_health():
    health = await orch.watchdog.check_all(await orch.execution.connect())
    return {
        "ok": health.ok,
        "safe_mode": health.safe_mode,
        "postgres": health.postgres,
        "redis": health.redis,
        "broker": health.broker,
        "issues": health.issues,
        "checked_at": health.checked_at,
    }


@app.get("/api/mode")
async def get_mode():
    cfg = get_settings()
    return {
        "mode": cfg.trading_mode,
        "live_enabled": cfg.enable_live_execution,
        "broker": cfg.default_broker,
    }


@app.get("/api/kite/status")
async def kite_status():
    from services.brokers.kite_auth import kite_auth
    return await kite_auth.get_status()


@app.get("/api/kite/login")
async def kite_login(api_key: str | None = Query(default=None)):
    """Browser OAuth start — accepts X-API-Key header or ?api_key= query (dashboard link)."""
    from services.brokers.kite_auth import kite_auth
    from services.gateway.auth import verify_api_token

    verify_api_token(api_key)
    try:
        return RedirectResponse(kite_auth.login_url())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/kite/callback")
async def kite_callback(
    request_token: str | None = Query(default=None),
    status: str | None = Query(default=None),
):
    from services.brokers.kite_auth import kite_auth

    if status and status != "success":
        return RedirectResponse(_app_url("/?kite=error&reason=login_cancelled"))
    if not request_token:
        return RedirectResponse(_app_url("/?kite=error&reason=missing_token"))

    try:
        await kite_auth.complete_login(request_token)
        orch.data._real_data_ok = None
        await orch.execution.connect()
        return RedirectResponse(_app_url("/?kite=connected"))
    except Exception as e:
        import urllib.parse
        reason = urllib.parse.quote(str(e)[:120])
        return RedirectResponse(_app_url(f"/?kite=error&reason={reason}"))


@app.post("/api/kite/disconnect", dependencies=[Depends(require_api_auth)])
async def kite_disconnect():
    from services.brokers.kite_auth import kite_auth

    await kite_auth.disconnect()
    orch.data._real_data_ok = None
    await orch.execution.disconnect()
    return {"ok": True, "message": "Kite session cleared"}


@app.post("/api/mode", dependencies=[Depends(require_api_auth)])
async def set_mode(req: ModeRequest):
    mode = req.mode
    if mode not in ("paper", "shadow", "live"):
        raise HTTPException(400, "Invalid mode — use paper, shadow, or live")

    if mode == "live":
        blockers = await orch.execution.live_blockers()
        if blockers:
            raise HTTPException(
                403,
                f"Live trading blocked: {', '.join(blockers[:3])}",
            )

    import os

    os.environ["TRADING_MODE"] = mode
    get_settings.cache_clear()
    orch.cfg = get_settings()
    orch.execution.refresh_broker()
    from services.control.layer import control_layer

    await control_layer.sync_trading_mode_state(mode)
    audit("trading_mode_changed", mode=mode)
    return {"mode": mode, "message": f"Switched to {mode} mode"}


@app.get("/api/risk/pnl/live")
async def live_pnl():
    return await orch.live_pnl()


@app.get("/api/risk/status")
async def risk_status():
    from services.control.halt import get_cached_risk

    cached = await get_cached_risk()
    if cached:
        return cached
    pnl = await orch.live_pnl()
    return orch.risk_status(pnl)


@app.get("/api/risk/trades/recent")
async def recent_trades(limit: int = Query(default=30, ge=1, le=100)):
    return {"events": recent_trade_events(limit)}


@app.post("/api/admin/kill-switch/on", dependencies=[Depends(require_api_auth)])
async def kill_switch_on():
    return await orch.activate_kill_switch()


@app.post("/api/admin/kill-switch/off", dependencies=[Depends(require_api_auth)])
async def kill_switch_off():
    return await orch.resume_trading()


@app.post("/api/admin/flatten-all", dependencies=[Depends(require_api_auth)])
async def admin_flatten_all():
    return await orch.emergency_flatten()


@app.post("/api/emergency/shutdown", dependencies=[Depends(require_api_auth)])
async def emergency_shutdown():
    return await orch.activate_kill_switch()


@app.post("/api/emergency/resume", dependencies=[Depends(require_api_auth)])
async def emergency_resume():
    return await orch.resume_trading()


@app.post("/api/emergency/flatten", dependencies=[Depends(require_api_auth)])
async def emergency_flatten():
    return await orch.emergency_flatten()


@app.post("/api/backtest/validate", dependencies=[Depends(require_api_auth)])
async def backtest_validate(req: BacktestRequest):
    result = orch.run_backtest(req.symbol, req.strategy)
    return {
        **result,
        "auto_reject": not result.get("passed_validation", False),
    }


@app.get("/api/execution/dlq", dependencies=[Depends(require_api_auth)])
async def execution_dlq():
    return {
        "pending": await orch.execution.dead_letter_pending(),
        "circuit": orch.execution.circuit_status(),
    }


@app.post("/api/autonomous/universe/refresh", dependencies=[Depends(require_api_auth)])
async def autonomous_universe_refresh():
    """Rebuild today's trending pool (50) and scan list (15) from Kite."""
    from services.autonomous.dynamic_universe import DynamicUniverseSelector

    selector = DynamicUniverseSelector(market_data=orch.data)
    snap = await selector.refresh()
    orch.autonomous.watchlist._last_universe = snap.to_dict()
    return snap.to_dict()


@app.get("/api/autonomous/status", dependencies=[Depends(require_api_auth)])
async def autonomous_status():
    return await orch.autonomous.status()


@app.post("/api/autonomous/start", dependencies=[Depends(require_api_auth)])
async def autonomous_start():
    ensure_background_loops()
    result = await orch.autonomous.start()
    if not result.get("ok"):
        blockers = result.get("blockers") or ["unknown blocker"]
        raise HTTPException(403, f"Autonomous start blocked: {', '.join(blockers)}")
    if get_settings().watchlist_mode == "dynamic":
        asyncio.create_task(_warmup_dynamic_universe())
    return result


@app.post("/api/autonomous/stop", dependencies=[Depends(require_api_auth)])
async def autonomous_stop():
    return await orch.autonomous.stop()


@app.post("/api/autonomous/tick", dependencies=[Depends(require_api_auth)])
async def autonomous_tick_now():
    """Run one autonomous scan cycle immediately (also used to verify the loop)."""
    ensure_background_loops()
    return await orch.autonomous.tick()


@app.get("/api/control/status")
async def control_status():
    from services.icb.engine import icb

    return await icb.status(
        {"portfolio": orch.portfolio, "trading_mode": get_settings().trading_mode},
    )


@app.get("/api/icb/status")
async def icb_status():
    from services.icb.engine import icb

    return await icb.status(
        {"portfolio": orch.portfolio, "trading_mode": get_settings().trading_mode},
    )


@app.post("/api/icb/override", dependencies=[Depends(require_api_auth)])
async def icb_override(state: str = Query(...), reason: str = Query(default="")):
    from services.icb.overrides import admin_set_state
    from services.icb.system_state import SystemState

    try:
        target = SystemState(state.upper())
    except ValueError as exc:
        raise HTTPException(400, f"Invalid system state: {state}") from exc
    return await admin_set_state(target, reason or f"Admin set {target.value}")


@app.post("/api/icb/clear-emergency", dependencies=[Depends(require_api_auth)])
async def icb_clear_emergency():
    from services.icb.overrides import admin_clear_emergency_lock

    return await admin_clear_emergency_lock()


@app.post("/api/admin/reset-kill-switch", dependencies=[Depends(require_api_auth)])
async def admin_reset_kill_switch():
    return await orch.admin_reset_kill_switch()


@app.get("/api/compliance/integrity")
async def compliance_integrity():
    from services.compliance.store import EventStore

    return EventStore().verify_chain()


@app.get("/api/compliance/events", dependencies=[Depends(require_api_auth)])
async def compliance_events(limit: int = Query(default=100, ge=1, le=1000)):
    from services.compliance.store import EventStore

    events = EventStore().load_all()
    return {"count": len(events), "events": events[-limit:]}


@app.post("/api/compliance/repair-chain", dependencies=[Depends(require_api_auth)])
async def compliance_repair_chain():
    from services.compliance.store import EventStore

    result = EventStore().repair_chain()
    audit("compliance_chain_repaired", **{k: result[k] for k in ("kept", "dropped") if k in result})
    return result


@app.post("/api/compliance/replay", dependencies=[Depends(require_api_auth)])
async def compliance_replay():
    from services.compliance.recorder import portfolio_snapshot
    from services.compliance.replay import ReplayEngine

    reference = {"portfolio": portfolio_snapshot(orch.portfolio)}
    return ReplayEngine().replay(reference_snapshot=reference)


@app.post("/api/compliance/report", dependencies=[Depends(require_api_auth)])
async def compliance_report():
    from services.compliance.recorder import portfolio_snapshot
    from services.compliance.reports import ComplianceReportGenerator

    reference = {"portfolio": portfolio_snapshot(orch.portfolio)}
    return ComplianceReportGenerator().generate(reference_snapshot=reference)


@app.get("/api/metrics")
async def api_metrics():
    return await metrics()


@app.get("/metrics")
async def metrics():
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.websocket("/ws/system")
async def ws_system(ws: WebSocket):
    """Optional real-time feed — multiplexes PnL, risk, trades, system state."""
    try:
        await require_ws_auth(ws)
    except HTTPException:
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        while True:
            pnl = await orch.live_pnl()
            risk = orch.risk_status(pnl)
            payload = {
                "pnl": pnl,
                "risk": risk,
                "trades": recent_trade_events(10),
                "halted": orch.portfolio.is_trading_halted(),
            }
            await ws.send_text(json.dumps(payload, default=str))
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        return
    except Exception:
        await ws.close()


@app.get("/api/support/incident-bundle", dependencies=[Depends(require_api_auth)])
async def support_incident_bundle(format: str = Query(default="json")):
    from services.support.bundle import build_incident_bundle, bundle_as_text

    bundle = await build_incident_bundle(orchestrator=orch)
    audit("support_bundle_generated", mode=get_settings().trading_mode)
    if format.lower() == "text":
        return Response(
            bundle_as_text(bundle),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=apex-incident-bundle.txt"},
        )
    return bundle


@app.get("/api/chaos/scenarios", dependencies=[Depends(require_api_auth)])
async def chaos_scenarios():
    from services.chaos.scenarios import CHAOS_SCENARIOS

    return [
        {
            "id": s.id,
            "name": s.name,
            "category": s.category.value,
            "latency_profile": s.latency_profile.value,
        }
        for s in CHAOS_SCENARIOS
    ]


async def _run_chaos_background(*, quick: bool) -> None:
    global _chaos_run_status
    _chaos_run_status = {"running": True, "error": "", "finished_at": None}
    try:
        from services.chaos.chaos_engine import chaos_engine

        report = await chaos_engine.run_suite(quick=quick)
        _chaos_run_status = {
            "running": False,
            "error": "",
            "finished_at": report.get("generated_at"),
            "resilience_score": report.get("resilience_score"),
            "stability_classification": report.get("stability_classification"),
            "safe_for_live_capital": report.get("safe_for_live_capital"),
            "scenario_count": report.get("scenario_count"),
        }
        audit("chaos_background_complete", score=report.get("resilience_score"))
    except Exception as exc:
        _chaos_run_status = {"running": False, "error": str(exc), "finished_at": None}
        audit("chaos_background_failed", error=str(exc))


@app.post("/api/chaos/run", dependencies=[Depends(require_api_auth)])
async def chaos_run(
    quick: bool = Query(default=False),
    background: bool = Query(default=False),
):
    from services.chaos.chaos_engine import chaos_engine
    from services.icb.actions import ICBAction
    from services.icb.engine import icb

    icb_result = await icb.authorize(
        ICBAction.RUN_CHAOS,
        {"portfolio": orch.portfolio, "trading_mode": get_settings().trading_mode},
    )
    if not icb_result.allowed:
        raise HTTPException(403, icb_result.reason)

    if background:
        global _chaos_run_task
        if _chaos_run_task is not None and not _chaos_run_task.done():
            return {
                "started": False,
                "running": True,
                "message": "Chaos suite already running — poll /api/chaos/run/status",
            }
        _chaos_run_task = asyncio.create_task(_run_chaos_background(quick=quick))
        return {
            "started": True,
            "running": True,
            "quick": quick,
            "message": "Chaos suite started in background (typically 5–15 min for full suite)",
        }

    return await chaos_engine.run_suite(quick=quick)


@app.get("/api/chaos/run/status", dependencies=[Depends(require_api_auth)])
async def chaos_run_status():
    from services.chaos.live_gate import ChaosLiveGate

    gate = ChaosLiveGate.status()
    running = bool(_chaos_run_status.get("running")) or (
        _chaos_run_task is not None and not _chaos_run_task.done()
    )
    return {
        "running": running,
        "job": _chaos_run_status,
        "gate": gate,
    }


@app.post("/api/chaos/run/{scenario_id}", dependencies=[Depends(require_api_auth)])
async def chaos_run_scenario(scenario_id: str):
    from services.chaos.chaos_engine import chaos_engine
    from services.icb.actions import ICBAction
    from services.icb.engine import icb

    icb_result = await icb.authorize(
        ICBAction.RUN_CHAOS,
        {"portfolio": orch.portfolio, "trading_mode": get_settings().trading_mode},
    )
    if not icb_result.allowed:
        raise HTTPException(403, icb_result.reason)

    try:
        result = await chaos_engine.run_scenario(scenario_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "scenario_id": result.scenario_id,
        "passed": result.passed,
        "safe": result.safe,
        "duration_ms": result.duration_ms,
        "failures": result.failures,
    }


@app.get("/api/chaos/report", dependencies=[Depends(require_api_auth)])
async def chaos_report():
    from services.chaos.chaos_engine import chaos_engine
    from services.chaos.live_gate import ChaosLiveGate

    if chaos_engine.last_report is None:
        report = ChaosLiveGate.load_report()
        if report is None:
            raise HTTPException(404, "No chaos report available — run /api/chaos/run first")
        return report
    return chaos_engine.last_report


@app.get("/api/chaos/gate")
async def chaos_gate_status():
    from services.chaos.live_gate import ChaosLiveGate

    return ChaosLiveGate.status()


@app.post("/api/live/repair-crce", dependencies=[Depends(require_api_auth)])
async def live_repair_crce():
    """Repair CRCE hash chain and return updated live blockers."""
    from services.compliance.store import EventStore
    from services.live.checklist import crce_blockers, live_capital_blockers

    result = EventStore().repair_chain()
    audit("live_crce_repaired", **{k: result[k] for k in ("kept", "dropped") if k in result})
    integrity = EventStore().verify_chain()
    blockers = live_capital_blockers(require_full_suite=True)
    return {
        "repair": result,
        "integrity": integrity,
        "crce_ok": integrity.get("valid", False),
        "blockers": blockers,
        "crce_blockers": crce_blockers(),
    }


@app.get("/api/live/checklist", dependencies=[Depends(require_api_auth)])
async def live_checklist():
    """Hard prerequisites for complete LIVE mode."""
    from services.brokers.kite_auth import kite_auth
    from services.live.checklist import live_capital_blockers

    cfg = get_settings()
    hard = live_capital_blockers(require_full_suite=True)
    exec_blockers = await orch.execution.live_blockers()
    kite = await kite_auth.get_status()
    autonomous = await orch.autonomous.status()
    return {
        "trading_mode": cfg.trading_mode,
        "live_ready": len(exec_blockers) == 0,
        "hard_blockers": exec_blockers,
        "crce_and_chaos": hard,
        "kite_connected": kite.get("connected", False),
        "autonomous_running": autonomous.get("running", False),
        "autonomous_blockers": autonomous.get("blockers", []),
        "steps": [
            "1. docker compose build api && docker compose up -d",
            "2. Connect Kite (dashboard Connect Zerodha)",
            "3. POST /api/compliance/repair-chain if CRCE invalid",
            "4. POST /api/chaos/run?quick=false (paper mode OK)",
            "5. TRADING_MODE=live in .env + restart",
            "6. POST /api/autonomous/start",
        ],
    }

