#!/usr/bin/env bash
# Restore https://tn88seval.in/apex/ when the API backend is down.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== Apex Trader site recovery =="
echo "Time: $(date -Is)"

if ! command -v docker-compose >/dev/null 2>&1 && ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: docker-compose not found"
  exit 1
fi

DC="docker-compose"
if ! docker ps >/dev/null 2>&1; then
  if sudo docker ps >/dev/null 2>&1; then
    DC="sudo docker-compose"
  else
    echo "ERROR: cannot access Docker (add user to docker group or use sudo)"
    exit 1
  fi
fi

echo ""
echo "== Container status =="
$DC ps -a || true

echo ""
echo "== Recent API logs =="
$DC logs api --tail 80 2>/dev/null || true

echo ""
echo "== Starting stack =="
$DC up -d --build postgres redis api

echo ""
echo "== Waiting for health =="
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
    echo "API healthy after ${i}s"
    curl -sf http://127.0.0.1:8080/api/health | python3 -m json.tool
    echo ""
    echo "Site should load at: https://tn88seval.in/apex/"
    exit 0
  fi
  sleep 2
done

echo "ERROR: API did not become healthy on :8080"
echo "Check: $DC logs api --tail 100"
exit 1
