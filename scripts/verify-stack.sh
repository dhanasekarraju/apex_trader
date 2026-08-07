#!/usr/bin/env bash
# End-to-end verification: infra, API, persistence, emergency halt.
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${1:-http://127.0.0.1:8090}"

echo "== Health =="
curl -sf "$BASE/api/health" | python3 -m json.tool

echo ""
echo "== Watchdog =="
curl -sf "$BASE/api/watchdog/health" | python3 -m json.tool

echo ""
echo "== Emergency shutdown (persisted) =="
curl -sf -X POST "$BASE/api/emergency/shutdown" | python3 -m json.tool

echo ""
echo "== Analyze while halted (should NO_TRADE) =="
curl -sf -X POST "$BASE/api/analyze" \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"RELIANCE"}' | python3 -m json.tool

echo ""
echo "== Dashboard (emergency_halt should be true) =="
curl -sf "$BASE/api/dashboard" | python3 -c "
import json,sys
d=json.load(sys.stdin)
p=d['portfolio']
assert p.get('emergency_halt') is True, 'emergency_halt not persisted'
print(json.dumps({'emergency_halt': p['emergency_halt'], 'equity': p['equity']}, indent=2))
"

echo ""
echo "PASS: stack verification complete"
