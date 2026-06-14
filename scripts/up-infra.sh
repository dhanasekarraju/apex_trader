#!/usr/bin/env bash
# Start PostgreSQL + Redis only (API can run locally or in Docker).
set -euo pipefail
cd "$(dirname "$0")/.."

if ! docker ps >/dev/null 2>&1; then
  echo "Docker permission denied."
  echo "Run ONE of:"
  echo "  sudo docker-compose up -d postgres redis"
  echo "  sudo usermod -aG docker \"$USER\"   # then log out/in"
  exit 1
fi

docker-compose up -d postgres redis
echo "Waiting for health checks..."
for i in {1..30}; do
  pg_ok=$(docker-compose exec -T postgres pg_isready -U apex -d apex_trader 2>/dev/null && echo ok || true)
  redis_ok=$(docker-compose exec -T redis redis-cli ping 2>/dev/null | grep -q PONG && echo ok || true)
  if [[ -n "$pg_ok" && -n "$redis_ok" ]]; then
    echo "PostgreSQL and Redis are ready."
    exit 0
  fi
  sleep 1
done
echo "Timed out waiting for infra."
exit 1
