#!/usr/bin/env bash
# Verify full institutional stack on production VPS.
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${APEX_URL:-https://tn88seval.in/apex}"
export API_KEY="${API_KEY:-$(python3 -c "
import hashlib
key = open('.env').read().split('SECRET_KEY=')[1].split('\n')[0]
print(hashlib.sha256(key.encode()).hexdigest()[:32])
")}"
auth=(-H "X-API-Key: $API_KEY")

echo "=== Full stack check: $BASE ==="
echo ""
echo "1) Docker (postgres + redis + api)"
docker compose ps
echo ""
echo "2) RAM"
free -h
echo ""
echo "3) Health"
curl -sf "$BASE/api/health" | python3 -m json.tool
echo ""
echo "4) Live checklist"
curl -sf "${auth[@]}" "$BASE/api/live/checklist" | python3 -m json.tool || true
echo ""
echo "5) Autonomous"
curl -sf "${auth[@]}" "$BASE/api/autonomous/status" | python3 -m json.tool || true
echo ""
echo "6) Postgres rows (trades + portfolio)"
docker compose exec -T postgres psql -U apex -d apex_trader -c \
  "SELECT 'trade_records' AS t, count(*) FROM trade_records
   UNION ALL SELECT 'positions', count(*) FROM positions
   UNION ALL SELECT 'system_state', count(*) FROM system_state;" 2>/dev/null || \
  echo "   (tables created on first API start)"
echo ""
echo "7) Persistent data dirs"
ls -la data/journal 2>/dev/null | tail -3 || echo "   journal: empty (fills after first trade)"
ls -la data/compliance 2>/dev/null | tail -3 || echo "   compliance: empty"
echo ""
echo "=== Full suite = postgres + redis + api + data volume + live gates ==="
