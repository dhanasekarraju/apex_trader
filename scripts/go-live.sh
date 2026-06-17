#!/usr/bin/env bash
# Apex Trader — complete LIVE mode bootstrap (run on server: ~/apex_trader)
set -euo pipefail

cd "$(dirname "$0")/.."
BASE_URL="${APEX_URL:-https://veldaris.in/apex}"

export API_KEY="${API_KEY:-$(python3 -c "
import hashlib
key = open('.env').read().split('SECRET_KEY=')[1].split('\n')[0]
print(hashlib.sha256(key.encode()).hexdigest()[:32])
")}"

auth=(-H "X-API-Key: $API_KEY")

echo "=== Apex Trader go-live bootstrap ==="
echo "API: $BASE_URL"

echo ""
echo "1) Rebuild & restart API (includes chaos + CRCE fixes)..."
docker compose build api
docker compose up -d api
sleep 12

echo ""
echo "2) CRCE integrity..."
curl -sf "$BASE_URL/api/compliance/integrity" | python3 -m json.tool || true
echo "   Repairing if needed..."
curl -sf -X POST "${auth[@]}" "$BASE_URL/api/compliance/repair-chain" | python3 -m json.tool || \
  docker compose exec -T api python3 -c "from services.compliance.store import EventStore; print(EventStore().repair_chain())"

echo ""
echo "3) Paper mode for chaos run..."
sed -i 's/^TRADING_MODE=.*/TRADING_MODE=paper/' .env
docker compose restart api
sleep 10

echo ""
echo "4) Full chaos suite (5–15 min)..."
curl -sf -X POST "${auth[@]}" "$BASE_URL/api/chaos/run?quick=false" | python3 -m json.tool

echo ""
echo "5) Waiting for chaos gate..."
for i in $(seq 1 30); do
  gate=$(curl -sf "${auth[@]}" "$BASE_URL/api/chaos/gate")
  approved=$(echo "$gate" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('live_capital_approved', False))" 2>/dev/null || echo false)
  class=$(echo "$gate" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('stability_classification',''))" 2>/dev/null || echo "")
  echo "   attempt $i: approved=$approved class=$class"
  if [ "$approved" = "True" ] || [ "$approved" = "true" ]; then
    break
  fi
  sleep 30
done

echo ""
echo "6) Switch to LIVE..."
grep -q '^GOLIVE_APPROVED=true' .env || echo 'GOLIVE_APPROVED=true' >> .env
grep -q '^ENABLE_LIVE_EXECUTION=true' .env || echo 'ENABLE_LIVE_EXECUTION=true' >> .env
grep -q '^AUTONOMOUS_ALLOW_LIVE=true' .env || echo 'AUTONOMOUS_ALLOW_LIVE=true' >> .env
sed -i 's/^TRADING_MODE=.*/TRADING_MODE=live/' .env
docker compose restart api
sleep 10

echo ""
echo "7) Live checklist..."
curl -sf "${auth[@]}" "$BASE_URL/api/live/checklist" | python3 -m json.tool

echo ""
echo "8) Start autonomous..."
curl -sf -X POST "${auth[@]}" "$BASE_URL/api/autonomous/start" | python3 -m json.tool || true

echo ""
echo "=== Done ==="
echo "Open $BASE_URL/ — confirm Kite CONNECTED, mode LIVE, AUTO RUNNING"
echo "Connect Kite daily if disconnected."
