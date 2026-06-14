#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

cp -n .env.example .env 2>/dev/null || true

if docker ps >/dev/null 2>&1; then
  echo "Starting full stack (postgres + redis + api)..."
  docker-compose up -d --build
elif [[ -f /.dockerenv ]]; then
  docker-compose up -d --build
else
  echo "Docker not accessible for user $(whoami)."
  echo ""
  echo "Option A — one-time sudo (infra only, API runs locally):"
  echo "  sudo docker-compose up -d postgres redis"
  echo "  ./scripts/run-local-api.sh"
  echo ""
  echo "Option B — add yourself to docker group (recommended):"
  echo "  sudo usermod -aG docker \"$USER\""
  echo "  newgrp docker"
  echo "  ./start.sh"
  echo ""
  echo "Option C — full stack with sudo:"
  echo "  sudo docker-compose up -d --build"
  exit 1
fi

echo ""
echo "Apex Trader → http://localhost:8080"
echo "Verify       → ./scripts/verify-stack.sh"
