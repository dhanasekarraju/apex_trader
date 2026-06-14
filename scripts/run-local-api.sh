#!/usr/bin/env bash
# Run API locally against docker-compose postgres/redis on localhost.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

export PYTHONPATH=.

# Stop prior local uvicorn on 8080
if command -v fuser >/dev/null 2>&1; then
  fuser -k 8080/tcp 2>/dev/null || true
elif command -v lsof >/dev/null 2>&1; then
  pid=$(lsof -ti :8080 2>/dev/null || true)
  [[ -n "$pid" ]] && kill $pid 2>/dev/null || true
fi

echo "Starting Apex Trader API on http://0.0.0.0:8080"
exec python3 -m uvicorn services.gateway.main:app --host 0.0.0.0 --port 8080 --reload
