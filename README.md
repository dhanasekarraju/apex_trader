# Apex Trader — Institutional Algorithmic Trading Platform v2

Production-grade autonomous trading platform with **capital preservation first**.

## Principles

- Risk management overrides signal generation and AI
- Shadow mode before live trading
- Go-live gate refuses live until all validations pass
- Every decision logged in trade journal

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full system diagram.

```
apex_trader/
├── services/
│   ├── gateway/         FastAPI + dashboard UI
│   ├── core/            Trading orchestrator v2
│   ├── brokers/         Kite, IB, CCXT, paper
│   ├── shadow/          Real data, simulated fills
│   ├── risk/            Base + advanced risk engine
│   ├── sizing/          Fixed risk, ATR, Kelly, portfolio
│   ├── execution/       Retry, idempotency, routing
│   ├── golive/          Readiness gate
│   ├── watchdog/        Safe mode on failure
│   ├── journal/         Trade decision audit
│   ├── strategy_lab/    Registration, ranking, auto-disable
│   ├── backtest/        Walk-forward, Monte Carlo
│   └── alerts/          Telegram, email
├── docs/                Architecture, ops, deployment, DR
├── ui/                  Institutional dashboard v2
└── tests/
```

## Quick start

```bash
cd apex_trader
cp .env.example .env
docker compose up -d --build
```

Open **http://localhost:8080**

## Trading modes

| Mode | Description |
|------|-------------|
| `paper` | Simulated broker (default) |
| `shadow` | Real data, simulated fills, slippage tracking |
| `live` | Real broker — blocked until go-live gate passes |

## API (v2)

| Endpoint | Description |
|----------|-------------|
| `GET /api/dashboard` | Portfolio, equity curve, strategies |
| `GET /api/readiness` | Go-live readiness report |
| `GET /api/shadow/report` | Shadow mode weekly stats |
| `GET /api/watchdog/health` | Infrastructure health |
| `POST /api/mode` | Switch paper/shadow/live |
| `POST /api/emergency/flatten` | Black swan flatten all |
| `POST /api/backtest/validate` | Full validation gate |

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Deployment](docs/DEPLOYMENT.md)
- [Operations](docs/OPERATIONS.md)
- [Disaster Recovery](docs/DISASTER_RECOVERY.md)

## Tests

```bash
pip install -r requirements.txt
cd apex_trader && PYTHONPATH=. pytest tests/ -v
```
