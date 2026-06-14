#!/usr/bin/env bash
# Full end-to-end API verification (run against direct backend or nginx URL).
set -euo pipefail
cd "$(dirname "$0")/.."

# Direct backend: http://127.0.0.1:8080
# Behind nginx:   https://veldaris.in/apex
BASE="${1:-http://127.0.0.1:8080}"
PASS=0
FAIL=0

ok() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

json() { curl -sf "$@"; }

echo "========================================"
echo "Apex Trader E2E — $BASE"
echo "========================================"

echo ""
echo "[1] Health"
if H=$(json "$BASE/api/health"); then
  echo "$H" | python3 -m json.tool
  echo "$H" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('ok')"; ok "health ok"
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
  echo "$R" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('  live_allowed:', d.get('live_allowed'))
print('  blockers:', d.get('blockers', [])[:3])
"
  ok "readiness report"
else bad "readiness failed"; fi

echo ""
echo "[5] Dashboard"
if D=$(json "$BASE/api/dashboard"); then
  echo "$D" | python3 -c "
import json,sys
d=json.load(sys.stdin)
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
AN=$(curl -sf -X POST "$BASE/api/analyze" -H 'Content-Type: application/json' -d '{"symbol":"RELIANCE"}')
echo "$AN" | python3 -c "import json,sys; d=json.load(sys.stdin); print('  action:', d.get('action'), '| reason:', (d.get('risk_reason') or d.get('reason') or '')[:60])"
ok "analyze"

echo ""
echo "[7] Backtest + validate"
BT=$(curl -sf -X POST "$BASE/api/backtest" -H 'Content-Type: application/json' -d '{"symbol":"RELIANCE","strategy":"momentum"}')
echo "$BT" | python3 -c "import json,sys; d=json.load(sys.stdin); print('  trades:', d.get('total_trades'), 'sharpe:', d.get('sharpe'))"
ok "backtest"
BV=$(curl -sf -X POST "$BASE/api/backtest/validate" -H 'Content-Type: application/json' -d '{"symbol":"RELIANCE"}')
echo "$BV" | python3 -c "import json,sys; d=json.load(sys.stdin); print('  passed_validation:', d.get('passed_validation'))"
ok "backtest validate"

echo ""
echo "[8] Strategies + journal + shadow"
json "$BASE/api/strategies" >/dev/null && ok "strategies" || bad "strategies"
json "$BASE/api/journal/weekly" >/dev/null && ok "journal weekly" || bad "journal"
json "$BASE/api/shadow/report" >/dev/null && ok "shadow report" || bad "shadow"
json "$BASE/api/risk/limits" >/dev/null && ok "risk limits" || bad "risk limits"

echo ""
echo "[9] Mode switch (paper -> shadow -> paper)"
M1=$(curl -sf -X POST "$BASE/api/mode" -H 'Content-Type: application/json' -d '{"mode":"shadow"}')
echo "$M1" | python3 -c "import json,sys; print(' ', json.load(sys.stdin).get('mode'))"
M2=$(curl -sf -X POST "$BASE/api/mode" -H 'Content-Type: application/json' -d '{"mode":"paper"}')
echo "$M2" | python3 -c "import json,sys; print(' ', json.load(sys.stdin).get('mode'))"
ok "mode switch"

echo ""
echo "[10] Live mode blocked (safety gate)"
LC=$(curl -s -o /tmp/live_mode.json -w "%{http_code}" -X POST "$BASE/api/mode" -H 'Content-Type: application/json' -d '{"mode":"live"}')
if [[ "$LC" == "403" ]]; then ok "live correctly blocked (403)"; else
  echo "  HTTP $LC"; cat /tmp/live_mode.json; bad "live should be blocked"; fi

echo ""
echo "[11] Emergency shutdown + halt blocks analyze"
curl -sf -X POST "$BASE/api/emergency/shutdown" >/dev/null
HALT=$(curl -sf -X POST "$BASE/api/analyze" -H 'Content-Type: application/json' -d '{"symbol":"RELIANCE"}')
echo "$HALT" | python3 -c "import json,sys; d=json.load(sys.stdin); print('  action:', d.get('action')); assert d.get('action')!='BUY'"
ok "emergency halt blocks trades"

echo ""
echo "[12] Flatten endpoint"
FL=$(curl -sf -X POST "$BASE/api/emergency/flatten")
echo "$FL" | python3 -m json.tool 2>/dev/null || echo "$FL"
ok "flatten endpoint"

echo ""
echo "[13] Resume trading (clear emergency)"
RS=$(curl -sf -X POST "$BASE/api/emergency/resume")
echo "$RS" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('ok'); print(' ', d.get('message'))"
D=$(curl -sf "$BASE/api/dashboard")
echo "$D" | python3 -c "import json,sys; p=json.load(sys.stdin)['portfolio']; assert not p.get('trading_halted'); print('  trading_halted:', p.get('trading_halted'))"
ok "resume trading"

echo ""
echo "[14] UI index"
CODE=$(curl -s -o /tmp/index.html -w "%{http_code}" "$BASE/")
if [[ "$CODE" == "200" ]] && grep -q "Apex Trader" /tmp/index.html; then ok "UI index ($CODE)"; else bad "UI index ($CODE)"; fi

echo ""
echo "[15] Kite login redirect"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -L "$BASE/api/kite/login" 2>/dev/null | tail -1 || curl -s -o /dev/null -w "%{http_code}" "$BASE/api/kite/login")
# 307/302 to kite, or 400 if no api key
if [[ "$CODE" =~ ^(302|307|400)$ ]]; then ok "kite login route ($CODE)"; else bad "kite login ($CODE)"; fi

echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
echo "========================================"
[[ "$FAIL" -eq 0 ]]
