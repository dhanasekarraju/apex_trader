"""Dated real-data validation, invalidated when strategy/risk inputs change."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'data' / 'validation' / 'latest.json'

def fingerprint(cfg):
    inputs = {k: v for k, v in cfg.model_dump().items() if k.startswith(('max_', 'min_', 'estimated_', 'golive_')) and k != 'golive_approved'}
    source = (ROOT / 'services/strategies/engine.py').read_bytes()
    return hashlib.sha256(source + json.dumps(inputs, sort_keys=True).encode()).hexdigest()

def latest_validation():
    try:
        return json.loads(EVIDENCE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}

def save_validation(result, cfg):
    report = {**result, 'generated_at': datetime.now(timezone.utc).isoformat(), 'fingerprint': fingerprint(cfg)}
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    temporary = EVIDENCE.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, allow_nan=False), encoding='utf-8')
    temporary.replace(EVIDENCE)

def validation_blockers(cfg):
    report = latest_validation()
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(report['generated_at'])).total_seconds() / 86400
        valid = (0 <= age <= cfg.validation_max_age_days
                 and report.get('fingerprint') == fingerprint(cfg)
                 and report.get('data_source') == 'kite'
                 and report.get('passed_validation') is True
                 and report.get('total_trades', 0) >= cfg.golive_min_completed_trades
                 and report.get('expectancy', 0) > 0)
    except (KeyError, ValueError, TypeError):
        valid = False
    return [] if valid else ['Fresh real-data backtest with positive expectancy required; run /api/backtest/validate']
