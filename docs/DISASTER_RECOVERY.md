# Disaster Recovery Guide

## Failure Scenarios

### PostgreSQL Down
- **Detection**: Watchdog marks postgres unhealthy
- **Action**: Safe mode in production/live; no new trades
- **Recovery**: Restore DB from backup, verify `SELECT 1`, restart API

### Redis Down
- **Detection**: Watchdog marks redis unhealthy
- **Action**: Safe mode in production/live
- **Recovery**: Restart Redis container, verify ping

### Broker Disconnected
- **Detection**: Watchdog + execution retry failures
- **Action**: Safe mode, cancel pending orders
- **Recovery**: Reconnect broker, verify `GET /api/watchdog/health`

### Runaway Losses
- **Detection**: Daily/weekly/monthly loss limits, drawdown circuit breaker
- **Action**: Automatic size reduction → halt → emergency shutdown
- **Recovery**: Manual review required before re-enabling

### Black Swan Event
- **Action**: `POST /api/emergency/flatten`
- **Recovery**: Manual go-live re-assessment required

## Backup Strategy

| Asset | Frequency | Location |
|-------|-----------|----------|
| PostgreSQL | Daily | External volume snapshot |
| Journal JSONL | Continuous | `data/journal/` |
| Shadow JSONL | Continuous | `data/shadow/` |
| Config `.env` | On change | Secure secrets store |

## Recovery Procedure

1. Stop API: `docker compose stop api`
2. Restore PostgreSQL from latest backup
3. Verify Redis and broker connectivity
4. Start API: `docker compose up -d api`
5. Confirm watchdog healthy
6. Resume in **shadow mode** before live
7. Re-run readiness report

## RTO / RPO Targets

| Metric | Target |
|--------|--------|
| RPO (data loss) | ≤ 24 hours (DB daily backup) |
| RTO (recovery) | ≤ 2 hours |

## Post-Incident

1. Document timeline in trade journal
2. Review risk engine decisions
3. Update thresholds if needed (never bypass gates)
4. Require fresh shadow period before live resume
