#!/usr/bin/env bash
# Full end-to-end API verification (run against direct backend or nginx URL).
set -euo pipefail
cd "$(dirname "$0")/.."

# Direct backend (compose host publish): http://127.0.0.1:8090
# Behind nginx:   https://tn88seval.in/apex
BASE="${1:-http://127.0.0.1:8090}"
API_KEY="${APEX_API_KEY:-$(python3 -c "import hashlib; print(hashlib.sha256(b'test-secret-for-ci').hexdigest()[:32])" 2>/dev/null || echo test-api-key-for-ci)}"
AUTH=(-H "X-API-Key: $API_KEY")
PASS=0
FAIL=0

ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

json() { curl -sf "$@"; }

# Unwrap {success, data, error} envelope when present
pyunwrap() { python3 -c "
import json,sys
def unwrap(d):
    if isinstance(d, dict) and 'success' in d:
        assert d.get('success'), d.get('error','')
        return d.get('data') or {}
    return d
$1
"; }

echo "========================================"
echo "Apex Trader E2E — $BASE"
echo "========================================"

echo ""
echo "[1] Health"
if H=$(json "$BASE/api/health"); then
  echo "$H" | python3 -m json.tool
  echo "$H" | pyunwrap "d=unwrap(json.load(sys.stdin)); assert d.get('ok'); print('  ok:', d.get('service'))"
  ok "health ok"
else bad "health unreachable"; fi

echo ""
echo "[2] Watchdog"
if W=$(json "$BASE/api/watchdog/health"); then
  echo "$W" | python3 -m json.tool
  ok "watchdog responded"
else bad "watchdog failed"; fi

echo ""
echo "[3] Kite status"
if K=$(json "$BASE/api/kite/status"); then
  echo "$K" | python3 -m json.tool
  ok "kite status"
else bad "kite status failed"; fi

echo ""
echo "[4] Readiness / go-live gate"
if R=$(json "$BASE/api/readiness"); then
  echo "$R" | pyunwrap "
d=unwrap(json.load(sys.stdin))
print('  live_allowed:', d.get('live_allowed'))
print('  blockers:', d.get('blockers', [])[:3])
"
  ok "readiness report"
else bad "readiness failed"; fi

echo ""
echo "[5] Dashboard"
if D=$(json "$BASE/api/dashboard"); then
  echo "$D" | pyunwrap "
d=unwrap(json.load(sys.stdin))
p=d.get('portfolio',{})
print('  mode:', d.get('mode'))
print('  equity:', p.get('equity'))
print('  emergency_halt:', p.get('emergency_halt'))
"
  ok "dashboard"
else bad "dashboard failed"; fi

echo ""
echo "[6] Regime + analyze pipeline"
if json "$BASE/api/regime/RELIANCE" >/dev/null; then ok "regime"; else bad "regime"; fi
AN=$(curl -sf -X POST "$BASE/api/analyze" "${AUTH[@]}" -H 'Content-Type: application/json' -d '{"symbol":"RELIANCE"}')
echo "$AN" | pyunwrap "d=unwrap(json.load(sys.stdin)); print('  action:', d.get('action'), '| reason:', (d.get('risk_reason') or d.get('reason') or '')[:60])"
ok "analyze"

echo ""
echo "[7] Backtest + validate"
BT=$(curl -sf -X POST "$BASE/api/backtest" "${AUTH[@]}" -H 'Content-Type: application/json' -d '{"symbol":"RELIANCE","strategy":"momentum"}')
echo "$BT" | pyunwrap "d=unwrap(json.load(sys.stdin)); print('  trades:', d.get('total_trades'), 'sharpe:', d.get('sharpe'))"
ok "backtest"
BV=$(curl -sf -X POST "$BASE/api/backtest/validate" "${AUTH[@]}" -H 'Content-Type: application/json' -d '{"symbol":"RELIANCE"}')
echo "$BV" | pyunwrap "d=unwrap(json.load(sys.stdin)); print('  passed_validation:', d.get('passed_validation'))"
ok "backtest validate"

echo ""
echo "[8] Strategies + journal + shadow"
json "$BASE/api/strategies" >/dev/null && ok "strategies" || bad "strategies"
json "$BASE/api/journal/weekly" >/dev/null && ok "journal weekly" || bad "journal"
json "$BASE/api/shadow/report" >/dev/null && ok "shadow report" || bad "shadow"
json "$BASE/api/risk/limits" >/dev/null && ok "risk limits" || bad "risk limits"

echo ""
echo "[9] Mode switch (paper -> shadow -> paper)"
M1=$(curl -sf -X POST "$BASE/api/mode" "${AUTH[@]}" -H 'Content-Type: application/json' -d '{"mode":"shadow"}')
echo "$M1" | pyunwrap "d=unwrap(json.load(sys.stdin)); print(' ', d.get('mode'))"
M2=$(curl -sf -X POST "$BASE/api/mode" "${AUTH[@]}" -H 'Content-Type: application/json' -d '{"mode":"paper"}')
echo "$M2" | pyunwrap "d=unwrap(json.load(sys.stdin)); print(' ', d.get('mode'))"
ok "mode switch"

echo ""
echo "[10] Live mode blocked (safety gate)"
LC=$(curl -s -o /tmp/live_mode.json -w "%{http_code}" -X POST "$BASE/api/mode" "${AUTH[@]}" -H 'Content-Type: application/json' -d '{"mode":"live"}')
if [[ "$LC" == "403" ]]; then ok "live correctly blocked (403)"; else
  echo "  HTTP $LC"; cat /tmp/live_mode.json; bad "live should be blocked"; fi

echo ""
echo "[11] Emergency shutdown + halt blocks analyze"
curl -sf -X POST "$BASE/api/emergency/shutdown" "${AUTH[@]}" >/dev/null
HALT=$(curl -sf -X POST "$BASE/api/analyze" "${AUTH[@]}" -H 'Content-Type: application/json' -d '{"symbol":"RELIANCE"}')
echo "$HALT" | pyunwrap "d=unwrap(json.load(sys.stdin)); print('  action:', d.get('action')); assert d.get('action')!='BUY'"
ok "emergency halt blocks trades"

echo ""
echo "[12] Flatten endpoint"
FL=$(curl -sf -X POST "$BASE/api/emergency/flatten" "${AUTH[@]}")
echo "$FL" | python3 -m json.tool 2>/dev/null || echo "$FL"
ok "flatten endpoint"

echo ""
echo "[13] Admin reset after kill switch + resume"
RESET=$(curl -sf -X POST "$BASE/api/admin/reset-kill-switch" "${AUTH[@]}" 2>/dev/null || echo '{"success":false}')
echo "$RESET" | pyunwrap "d=unwrap(json.load(sys.stdin)); print('  reset:', d.get('ok'), d.get('message','')[:60])" 2>/dev/null || echo "  (reset skipped if not kill switched)"
RS=$(curl -sf -X POST "$BASE/api/emergency/resume" "${AUTH[@]}")
echo "$RS" | pyunwrap "d=unwrap(json.load(sys.stdin)); assert d.get('ok'); print(' ', d.get('message'))"
D=$(curl -sf "$BASE/api/dashboard")
echo "$D" | pyunwrap "d=unwrap(json.load(sys.stdin)); p=d['portfolio']; assert not p.get('trading_halted'); print('  trading_halted:', p.get('trading_halted'))"
ok "resume trading"

echo ""
echo "[14] Autonomous engine"
AS=$(json "$BASE/api/autonomous/status" "${AUTH[@]}")
echo "$AS" | pyunwrap "
d=unwrap(json.load(sys.stdin))
print('  running:', d.get('running'))
print('  watchlist:', d.get('watchlist_count'))
print('  session:', d.get('session'))
"
ok "autonomous status"
AST=$(curl -sf -X POST "$BASE/api/autonomous/start" "${AUTH[@]}")
echo "$AST" | pyunwrap "d=unwrap(json.load(sys.stdin)); print('  started:', d.get('running'))"
ok "autonomous start"
ASP=$(curl -sf -X POST "$BASE/api/autonomous/stop" "${AUTH[@]}")
echo "$ASP" | pyunwrap "d=unwrap(json.load(sys.stdin)); assert not d.get('running'); print('  stopped')"
ok "autonomous stop"

echo ""
echo "[15] UI index"
CODE=$(curl -s -o /tmp/index.html -w "%{http_code}" "$BASE/")
if [[ "$CODE" == "200" ]] && grep -q "Apex Trader" /tmp/index.html; then ok "UI index ($CODE)"; else bad "UI index ($CODE)"; fi

echo ""
echo "[16] Kite login redirect"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -L "$BASE/api/kite/login" 2>/dev/null | tail -1 || curl -s -o /dev/null -w "%{http_code}" "$BASE/api/kite/login")
# 307/302 to kite, or 400 if no api key
if [[ "$CODE" =~ ^(302|307|400)$ ]]; then ok "kite login route ($CODE)"; else bad "kite login ($CODE)"; fi

echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
echo "========================================"
[[ "$FAIL" -eq 0 ]]
