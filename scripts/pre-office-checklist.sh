#!/usr/bin/env bash
# Run once before leaving for office — full go-live preflight for tn88seval.in
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${APEX_URL:-https://tn88seval.in/apex}"
export API_KEY="${API_KEY:-$(python3 -c "
import hashlib
key = open('.env').read().split('SECRET_KEY=')[1].split('\n')[0]
print(hashlib.sha256(key.encode()).hexdigest()[:32])
")}"
auth=(-H "X-API-Key: $API_KEY")

pass=0
fail=0
warn=0

ok()   { echo "  [OK]   $1"; pass=$((pass+1)); }
bad()  { echo "  [FAIL] $1"; fail=$((fail+1)); }
note() { echo "  [!!]   $1"; warn=$((warn+1)); }

echo "=============================================="
echo "  APEX PRE-OFFICE CHECK — $(date '+%Y-%m-%d %H:%M %Z')"
echo "  Market session: 09:20–15:15 IST"
echo "=============================================="
echo ""

echo "1) Server containers"
if docker compose ps 2>/dev/null | grep -q "apex_trader_api.*Up"; then
  ok "API container Up"
else
  bad "API not running — run: sudo docker compose up -d"
fi
if docker compose ps 2>/dev/null | grep postgres | grep -q "healthy\|Up"; then
  ok "Postgres Up"
else
  bad "Postgres down"
fi
echo ""

echo "2) Memory"
avail=$(free -m | awk '/^Mem:/{print $7}')
if [ "${avail:-0}" -ge 500 ] 2>/dev/null; then
  ok "RAM available ${avail}MB"
else
  note "RAM tight (${avail}MB) — close extra tabs on dashboard"
fi
echo ""

echo "3) API health"
health=$(curl -sf "$BASE/api/health" 2>/dev/null || true)
if echo "$health" | grep -q '"ok":true'; then
  mode=$(echo "$health" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',d).get('mode','?'))" 2>/dev/null || echo "?")
  if [ "$mode" = "live" ]; then ok "API live mode"; else bad "Mode is $mode — need live"; fi
else
  bad "API health failed"
fi
echo ""

echo "4) Risk & capital"
risk=$(curl -sf "${auth[@]}" "$BASE/api/risk/status" 2>/dev/null || true)
status=$(echo "$risk" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',d).get('status',''))" 2>/dev/null || echo "")
if [ "$status" = "SAFE" ]; then
  ok "Risk SAFE"
elif [ "$status" = "WARNING" ]; then
  note "Risk WARNING — may still trade"
else
  bad "Risk $status — fix before market"
fi
sync=$(curl -sf -X POST "${auth[@]}" "http://127.0.0.1:8080/api/portfolio/sync-capital" 2>/dev/null || \
       curl -sf -X POST "${auth[@]}" "$BASE/api/portfolio/sync-capital" 2>/dev/null || true)
if echo "$sync" | grep -q '"equity"'; then
  eq=$(echo "$sync" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',d).get('equity',''))" 2>/dev/null)
  ok "Capital synced from Kite: Rs $eq"
else
  note "Capital sync skipped — connect Kite first"
fi
echo ""

echo "5) Kite"
kite=$(curl -sf "$BASE/api/kite/status" 2>/dev/null || true)
if echo "$kite" | grep -q '"connected":true'; then
  ok "Kite CONNECTED"
else
  bad "Kite NOT connected — Connect Zerodha in UI before 09:20"
fi
echo "$kite" | grep -q '"needs_daily_login":true' && note "Kite needs daily login today"
echo ""

echo "6) Autonomous gates"
auto=$(curl -sf "${auth[@]}" "$BASE/api/autonomous/status" 2>/dev/null || true)
blockers=$(echo "$auto" | python3 -c "import sys,json; b=json.load(sys.stdin).get('data',{}).get('blockers',[]); print(len(b))" 2>/dev/null || echo 1)
running=$(echo "$auto" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('running',False))" 2>/dev/null || echo False)
if [ "$blockers" = "0" ]; then ok "No autonomous blockers"; else bad "Blockers present — repair CRCE / chaos in UI"; fi
if [ "$running" = "True" ]; then ok "Autonomous RUNNING"; else note "Autonomous OFF — Start auto before 09:20 IST"; fi
echo ""

echo "7) Live checklist"
curl -sf "${auth[@]}" "$BASE/api/live/checklist" 2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin).get('data',{})
if d.get('ready'): print('  [OK]   Live checklist PASSED')
else:
    print('  [FAIL] Live checklist blocked')
    for b in (d.get('blockers') or [])[:5]: print('         -', b)
" 2>/dev/null || note "Could not fetch live checklist"
echo ""

echo "8) HTTPS dashboard"
if curl -sf -o /dev/null --max-time 10 "$BASE/api/health"; then
  ok "https://tn88seval.in/apex/ reachable"
else
  bad "Dashboard URL not reachable"
fi
echo ""

echo "=============================================="
echo "  RESULT: $pass OK | $fail FAIL | $warn WARN"
echo "=============================================="
echo ""
if [ "$fail" -eq 0 ]; then
  echo "READY for office hours. On phone:"
  echo "  1. Open https://tn88seval.in/apex/"
  echo "  2. Connect Zerodha (if not connected)"
  echo "  3. Start auto before 09:20 IST"
  echo "  4. Check AUTO RUNNING + last_cycle updates after 09:25"
else
  echo "FIX failures above before 09:20 IST."
fi
echo ""
echo "Session ends 15:15 IST — bot stops new scans after that."
exit "$fail"
