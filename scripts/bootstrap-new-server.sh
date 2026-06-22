#!/usr/bin/env bash
# Full Apex Trader bootstrap on new VPS — tn88seval.in (4GB+ recommended)
# Run as root or with sudo on Ubuntu 22.04/24.04
set -euo pipefail

DOMAIN="${APEX_DOMAIN:-tn88seval.in}"
APP_DIR="${APEX_DIR:-/home/ubuntu/apex_trader}"
SERVER_IP="${APEX_SERVER_IP:-103.194.228.130}"

echo "=== Apex Trader full setup: $DOMAIN ($SERVER_IP) ==="

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  usermod -aG docker "${SUDO_USER:-ubuntu}" || true
fi

if ! command -v nginx >/dev/null 2>&1; then
  apt-get update
  apt-get install -y nginx certbot python3-certbot-nginx git
fi

mkdir -p "/var/www/$DOMAIN"
if [ ! -d "$APP_DIR" ]; then
  echo "Clone or copy apex_trader to $APP_DIR first, then re-run."
  exit 1
fi

cd "$APP_DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from example — edit SECRET_KEY, KITE keys, then re-run."
  exit 1
fi

# Domain / Kite redirect (idempotent)
grep -q '^PUBLIC_URL=' .env && sed -i "s|^PUBLIC_URL=.*|PUBLIC_URL=https://$DOMAIN|" .env || echo "PUBLIC_URL=https://$DOMAIN" >> .env
grep -q '^APP_BASE_PATH=' .env && sed -i 's|^APP_BASE_PATH=.*|APP_BASE_PATH=/apex|' .env || echo 'APP_BASE_PATH=/apex' >> .env
grep -q '^KITE_REDIRECT_URL=' .env && sed -i "s|^KITE_REDIRECT_URL=.*|KITE_REDIRECT_URL=https://$DOMAIN/apex/api/kite/callback|" .env || echo "KITE_REDIRECT_URL=https://$DOMAIN/apex/api/kite/callback" >> .env
grep -q '^CORS_ALLOWED_ORIGINS=' .env && sed -i "s|^CORS_ALLOWED_ORIGINS=.*|CORS_ALLOWED_ORIGINS=https://$DOMAIN,https://www.$DOMAIN|" .env || echo "CORS_ALLOWED_ORIGINS=https://$DOMAIN,https://www.$DOMAIN" >> .env
grep -q '^ENV=' .env && sed -i 's|^ENV=.*|ENV=production|' .env || echo 'ENV=production' >> .env

echo "=== Docker stack (full: postgres + redis + api) ==="
docker compose build api
docker compose up -d

echo "=== Nginx ==="
cp "deploy/nginx-$DOMAIN.conf" "/etc/nginx/sites-available/$DOMAIN" 2>/dev/null || \
  cp deploy/nginx-tn88seval.conf "/etc/nginx/sites-available/$DOMAIN"
ln -sf "/etc/nginx/sites-available/$DOMAIN" "/etc/nginx/sites-enabled/$DOMAIN"
rm -f /etc/nginx/sites-enabled/default
nginx -t

if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
  echo "=== SSL (Let's Encrypt) — Cloudflare must be DNS only (grey cloud) for this step ==="
  certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" --non-interactive --agree-tos -m "admin@$DOMAIN" || {
    echo "Certbot failed. Ensure A record points to $SERVER_IP and proxy is OFF in Cloudflare."
    exit 1
  }
fi

systemctl reload nginx

echo ""
echo "=== Done ==="
echo "Dashboard: https://$DOMAIN/apex/"
echo "Health:    curl -s http://127.0.0.1:8080/api/health"
echo ""
echo "Next:"
echo "  1. Kite developer console → redirect URL: https://$DOMAIN/apex/api/kite/callback"
echo "  2. Register server IP $SERVER_IP on Kite (static IP rule)"
echo "  3. bash scripts/go-live.sh"
