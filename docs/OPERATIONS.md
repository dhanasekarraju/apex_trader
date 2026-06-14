# Operations Guide

## Daily Operations

1. **Check dashboard** — equity, drawdown, heat, daily PnL
2. **Review readiness** — `GET /api/readiness`
3. **Monitor watchdog** — `GET /api/watchdog/health`
4. **Review decisions** — audit trail in dashboard

## Trading Modes

| Action | Endpoint |
|--------|----------|
| Switch to paper | `POST /api/mode {"mode":"paper"}` |
| Switch to shadow | `POST /api/mode {"mode":"shadow"}` |
| Attempt live | `POST /api/mode {"mode":"live"}` (blocked if gate fails) |

## Alerts

Configure in `.env`:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ALERT_EMAIL_TO=ops@example.com
SMTP_HOST=smtp.example.com
```

Alerts fire on:
- Safe mode activation
- Emergency flatten
- Critical watchdog failures

## Emergency Procedures

### Emergency Stop (halt new trades)
```
POST /api/emergency/shutdown
```
Cancels pending orders, activates emergency halt.

### Flatten All (black swan)
```
POST /api/emergency/flatten
```
Closes all positions, enters black swan mode, sends critical alert.

## Weekly Review

- Shadow report: `GET /api/shadow/report`
- Journal: `GET /api/journal/weekly`
- Strategy ranking: `GET /api/strategies`

## Metrics

Prometheus metrics available via `prometheus-client` integration (extend as needed).

Structured logs via `structlog` — search for `audit` events.
