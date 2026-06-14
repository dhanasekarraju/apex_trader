"""Structured logging + audit trail helpers."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any

import structlog

from shared.config import get_settings


def setup_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


def audit(event: str, **fields: Any) -> None:
    """Immutable-style audit log for every decision."""
    log = structlog.get_logger("audit")
    log.info(event, **fields, ts=datetime.now(timezone.utc).isoformat())
