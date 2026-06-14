"""Structured logging — audit, trade, and error streams with rotation."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

from shared.config import get_settings

_LOG_DIR = Path(__file__).resolve().parents[1] / "data" / "logs"
_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    cfg = get_settings()
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)

    error_handler = RotatingFileHandler(
        _LOG_DIR / "errors.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(error_handler)

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )
    _CONFIGURED = True


def audit(event: str, **fields: Any) -> None:
    log = structlog.get_logger("audit")
    log.info(event, **fields, ts=datetime.now(timezone.utc).isoformat())


def trade_log(
    *,
    symbol: str,
    strategy: str,
    action: str,
    confidence: float | str = "",
    risk_check: str = "",
    result: str,
    **extra: Any,
) -> None:
    """Append-only trade lifecycle log (separate from error log)."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "strategy": strategy,
        "action": action,
        "confidence": confidence,
        "risk_check": risk_check,
        "result": result,
        **extra,
    }
    path = _LOG_DIR / "trades.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")
    audit("trade_event", **record)


def log_error(message: str, **fields: Any) -> None:
    logging.getLogger("apex.errors").error(message, extra=fields)
    audit("error", message=message, **fields)
