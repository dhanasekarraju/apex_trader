"""Apex Trader API Gateway — FastAPI + institutional dashboard v2."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from services.core.orchestrator import TradingOrchestrator
from shared.config import get_settings
from shared.database import init_db
from shared.logging import setup_logging

UI_DIR = Path(__file__).resolve().parents[2] / "ui"
orch = TradingOrchestrator()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    try:
        await init_db()
        await orch.startup()
    except Exception as e:
        import structlog
        structlog.get_logger("startup").error("startup_failed", error=str(e))
    yield


app = FastAPI(
    title="Apex Trader",
    description="Institutional algorithmic trading platform — capital preservation first",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    symbol: str


class BacktestRequest(BaseModel):
    symbol: str
    strategy: str | None = None


class ModeRequest(BaseModel):
    mode: str  # paper | shadow | live


@app.get("/")
async def index():
    index_path = UI_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(404, "UI not found")
    base = _base_path()
    html = index_path.read_text(encoding="utf-8").replace("__BASE_PATH__", base)
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
    df = orch.data.synthetic_ohlcv(symbol.upper())
    r = orch.regime.analyze(df)
    return {
        "symbol": symbol.upper(),
        "regime": r.regime.value,
        "confidence": r.confidence,
        "volatility_pct": r.volatility_pct,
        "trend_strength": r.trend_strength,
        "trade_allowed": r.trade_allowed,
        "recommended_strategies": r.recommended_strategies,
        "explanation": r.explanation,
    }


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    return await orch.analyze_symbol(req.symbol.upper())


@app.post("/api/backtest")
async def backtest(req: BacktestRequest):
    return orch.run_backtest(req.symbol.upper(), req.strategy)


@app.get("/api/strategies")
async def strategies():
    from services.strategies.engine import STRATEGY_REGISTRY
    return {
        "strategies": list(STRATEGY_REGISTRY.keys()),
        "ranking": orch.strategy_lab.ranking(),
        "enabled": orch.strategy_lab.enabled_strategies(),
    }


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
async def kite_login():
    from services.brokers.kite_auth import kite_auth
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


@app.post("/api/kite/disconnect")
async def kite_disconnect():
    from services.brokers.kite_auth import kite_auth

    await kite_auth.disconnect()
    orch.data._real_data_ok = None
    if hasattr(orch.execution._broker, "disconnect"):
        await orch.execution._broker.disconnect()
    return {"ok": True, "message": "Kite session cleared"}


@app.post("/api/mode")
async def set_mode(req: ModeRequest):
    mode = req.mode.lower()
    if mode not in ("paper", "shadow", "live"):
        raise HTTPException(400, "Mode must be paper, shadow, or live")
    if mode == "live":
        blockers = await orch.execution.live_blockers()
        if blockers:
            raise HTTPException(
                403,
                f"Live trading blocked: {', '.join(blockers[:3])}",
            )
    import os
    from services.brokers.factory import get_broker
    os.environ["TRADING_MODE"] = mode
    get_settings.cache_clear()
    orch.cfg = get_settings()
    orch.execution._broker = get_broker()
    return {"mode": mode, "message": f"Switched to {mode} mode"}


@app.post("/api/emergency/shutdown")
async def emergency_shutdown():
    orch.portfolio.emergency_shutdown()
    await orch.portfolio.persist()
    await orch.execution.cancel_all()
    return {"ok": True, "message": "Emergency shutdown — no new trades, pending cancelled"}


@app.post("/api/emergency/resume")
async def emergency_resume():
    if not orch.portfolio.is_trading_halted():
        return {
            "ok": True,
            "message": "Trading already active",
            "trading_halted": False,
        }
    orch.portfolio.resume_trading()
    await orch.portfolio.persist()
    return {
        "ok": True,
        "message": "Emergency cleared — trading resumed (paper/shadow/live per mode)",
        "trading_halted": False,
    }


@app.post("/api/emergency/flatten")
async def emergency_flatten():
    return await orch.emergency_flatten()


@app.post("/api/backtest/validate")
async def backtest_validate(req: BacktestRequest):
    result = orch.run_backtest(req.symbol.upper(), req.strategy)
    return {
        **result,
        "auto_reject": not result.get("passed_validation", False),
    }


@app.get("/metrics")
async def metrics():
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    from fastapi.responses import Response
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

