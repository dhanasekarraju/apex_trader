#!/usr/bin/env bash
# Rebuild Apex on a 1GB RAM VPS with memory limits and slower background polling.
set -euo pipefail
cd "$(dirname "$0")/.."

DC="docker compose"
if ! $DC version >/dev/null 2>&1; then
  DC="docker-compose"
fi

echo "=== Memory before ==="
free -h

echo "=== Rebuild with lowmem overlay ==="
$DC -f docker-compose.yml -f docker-compose.lowmem.yml up -d --build api

echo "=== Container memory ==="
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>/dev/null || true

echo "=== Health ==="
sleep 5
curl -sf http://127.0.0.1:8080/api/health && echo ""

echo "=== Memory after ==="
free -h
