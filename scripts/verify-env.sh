#!/usr/bin/env bash
# Verify .env is loaded and wired to shared.config.Settings
set -euo pipefail
cd "$(dirname "$0")/.."

python3 << 'PY'
from shared.config import get_settings

get_settings.cache_clear()
s = get_settings()

checks = []

def ok(name, cond, detail=""):
    checks.append((name, cond, detail))

ok("ENV production", s.env == "production", s.env)
ok("TRADING_MODE live", s.trading_mode == "live", s.trading_mode)
ok("Live execution", s.enable_live_execution, str(s.enable_live_execution))
ok("GOLIVE_APPROVED", s.golive_approved, str(s.golive_approved))
ok("AUTONOMOUS live", s.autonomous_allow_live, str(s.autonomous_allow_live))
ok("PUBLIC_URL", "tn88seval.in" in s.public_url, s.public_url)
ok("APP_BASE_PATH", s.app_base_path == "/apex", s.app_base_path)
ok("Kite redirect", s.kite_redirect_url.endswith("/apex/api/kite/callback"), s.kite_redirect_url)
ok("Kite API key", bool(s.kite_api_key.strip()), "set" if s.kite_api_key.strip() else "MISSING")
ok("Kite API secret", bool(s.kite_api_secret.strip()), "set" if s.kite_api_secret.strip() else "MISSING")
ok("Kite static IP flag", s.kite_static_ip_confirmed, str(s.kite_static_ip_confirmed))
ok("Market data kite", s.market_data_source == "kite", s.market_data_source)
ok("Watchlist dynamic", s.watchlist_mode == "dynamic", s.watchlist_mode)
ok("DB host docker", "postgres" in s.database_url, s.database_url.split("@")[-1])
ok("Redis docker", "redis" in s.redis_url, s.redis_url)

print("=== Apex .env wiring check ===\n")
failed = 0
for name, cond, detail in checks:
    mark = "OK" if cond else "FAIL"
    if not cond:
        failed += 1
    print(f"  [{mark}] {name}: {detail}")

print(f"\nAutonomous: pool={s.autonomous_universe_pool_size} scan={s.autonomous_max_symbols_per_cycle} "
      f"quotes={s.autonomous_universe_max_quotes} interval={s.autonomous_scan_interval_sec}s")
print(f"Session: {s.autonomous_session_start}–{s.autonomous_session_end} IST | "
      f"min_price={s.autonomous_universe_min_price} min_vol={s.autonomous_universe_min_volume}")
print(f"Risk: capital={s.initial_capital} max_positions={s.max_open_positions} "
      f"min_confidence={s.min_confidence_score}")

if failed:
    print(f"\n{failed} check(s) failed — fix .env and restart api")
    raise SystemExit(1)
print("\nAll critical checks passed.")
PY
