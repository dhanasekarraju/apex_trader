# Deployment Guide

## Prerequisites

- Docker & Docker Compose
- Python 3.11+ (local dev)
- Broker credentials (Kite / IB / CCXT) for live mode

## Quick Start (Docker)

```bash
cd apex_trader
cp .env.example .env
# Edit .env with secrets and broker keys
docker compose up -d --build
```

Dashboard: `http://localhost:8090` (shared-VPS host port; see [VPS_COEXIST.md](VPS_COEXIST.md))

## Local Development

```bash
cd apex_trader
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Point DATABASE_URL to localhost:5439 / REDIS to :6389 if using compose-published ports
uvicorn services.gateway.main:app --host 0.0.0.0 --port 8080 --reload
```

## Environment Validation

Required before production:

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Must be changed from default |
| `DATABASE_URL` | PostgreSQL connection |
| `REDIS_URL` | Redis connection |
| `TRADING_MODE` | `paper` / `shadow` / `live` |
| `ENABLE_LIVE_EXECUTION` | Must be `true` only after go-live gate |

## Broker Setup

### Zerodha Kite
```
DEFAULT_BROKER=kite
KITE_API_KEY=...
KITE_API_SECRET=...
KITE_ACCESS_TOKEN=...
```

### Interactive Brokers
```
DEFAULT_BROKER=ib
IB_HOST=127.0.0.1
IB_PORT=7497
```

### CCXT (Crypto)
```
DEFAULT_BROKER=ccxt
CCXT_EXCHANGE=binance
CCXT_API_KEY=...
CCXT_API_SECRET=...
```

## Go-Live Checklist

1. Run backtests — `/api/backtest/validate`
2. Operate in shadow mode ≥ 14 days
3. Review readiness — `/api/readiness`
4. Set `ENABLE_LIVE_EXECUTION=true` only when all categories pass
5. Switch mode — `POST /api/mode {"mode":"live"}`
