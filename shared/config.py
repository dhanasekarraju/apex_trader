"""Shared configuration — institutional defaults, capital preservation first."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://apex:apex@localhost:5432/apex_trader"
    database_url_sync: str = "postgresql://apex:apex@localhost:5432/apex_trader"
    redis_url: str = "redis://localhost:6379/0"

    api_host: str = "0.0.0.0"
    api_port: int = 8080
    secret_key: str = "dev-secret-change-me"
    # Public URL when behind nginx (no trailing slash). Set APP_BASE_PATH=/apex for subpath deploy.
    public_url: str = "https://tn88seval.in"
    app_base_path: str = "/apex"

    # Capital & risk
    initial_capital: float = 1_000_000.0
    max_risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 1.5
    max_weekly_loss_pct: float = 3.0
    max_monthly_loss_pct: float = 5.0
    max_monthly_drawdown_pct: float = 5.0
    max_portfolio_heat_pct: float = 4.0
    max_correlated_exposure_pct: float = 15.0
    max_sector_concentration_pct: float = 20.0
    min_confidence_score: float = 72.0
    max_open_positions: int = 8
    consecutive_loss_reduce: int = 3
    consecutive_loss_halt: int = 5
    high_vol_size_multiplier: float = 0.5
    vol_threshold_reduce: float = 35.0
    kelly_cap_fraction: float = 0.25

    # Trading modes: paper | shadow | live
    trading_mode: str = "paper"
    enable_live_execution: bool = False
    shadow_slippage_bps: float = 3.0

    # Go-live thresholds (all must pass)
    golive_min_sharpe: float = 1.0
    golive_min_win_rate: float = 45.0
    golive_max_drawdown: float = 8.0
    golive_min_shadow_days: int = 14
    golive_min_profit_factor: float = 1.2

    # Brokers
    default_broker: str = "paper"
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_access_token: str = ""  # optional fallback; prefer UI OAuth session
    kite_redirect_url: str = "https://tn88seval.in/apex/api/kite/callback"
    # -1 = auto (Kite exchange band); 1-100 = custom %. Never use 0 (rejected since Apr 2025).
    kite_market_protection: int = -1
    kite_exchange: str = "NSE"
    kite_product: str = "MIS"  # intraday equity default
    kite_autoslice: bool = True  # split at exchange freeze limits
    kite_static_ip_confirmed: bool = False  # set true after registering server IP on Kite dev console
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497
    ib_client_id: int = 1
    ccxt_exchange: str = "binance"
    ccxt_api_key: str = ""
    ccxt_api_secret: str = ""

    # Data
    market_data_source: str = "synthetic"  # synthetic | kite
    min_data_quality_score: float = 0.85
    max_stale_feed_seconds: int = 120

    # Alerts
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alert_email_to: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    # Strategy lab
    strategy_disable_win_rate: float = 35.0
    strategy_disable_min_trades: int = 20

    enable_news_sentiment: bool = False

    # Production ops
    external_api_timeout_sec: float = 15.0
    analyze_timeout_sec: float = 10.0
    execute_timeout_sec: float = 45.0
    log_dir: str = "data/logs"
    golive_approved: bool = False
    chaos_gate_enforce: bool = True
    chaos_report_max_age_hours: int = 168
    api_failure_threshold: int = 5
    api_circuit_pause_minutes: int = 10
    idempotency_bucket_minutes: int = 5
    enforce_market_hours: bool = True
    max_entry_deviation_pct: float = 2.0
    lifecycle_poll_sec: float = 5.0
    control_refresh_sec: float = 3.0
    ui_poll_interval_sec: float = 3.0
    database_pool_size: int = 5
    database_max_overflow: int = 2

    # Autonomous engine (institutional scan → analyze → execute)
    autonomous_enabled: bool = True
    autonomous_allow_live: bool = False
    autonomous_auto_start: bool = False
    autonomous_scan_interval_sec: float = 90.0
    autonomous_symbol_cooldown_sec: float = 300.0
    autonomous_max_symbols_per_cycle: int = 15
    autonomous_max_watchlist_size: int = 50
    autonomous_inter_symbol_delay_sec: float = 1.5
    autonomous_session_start: str = "09:20"
    autonomous_session_end: str = "15:15"
    # static = data/watchlist.yaml | dynamic = Kite trending pool daily
    watchlist_mode: str = "dynamic"
    autonomous_universe_pool_size: int = 50
    autonomous_universe_max_quotes: int = 120
    autonomous_universe_min_price: float = 50.0
    autonomous_universe_min_volume: int = 50_000
    # 0 = no cap; set ~1400 for ₹4–5k accounts so scan targets affordable NSE names
    autonomous_universe_max_price: float = 0.0
    watchlist_symbols: str = ""
    watchlist_file: str = "data/watchlist.yaml"
    enforce_sector_correlation_limits: bool = False
    sync_capital_from_kite: bool = True
    capital_sync_interval_sec: float = 300.0

    # API security
    api_access_key: str = ""
    cors_allowed_origins: str = ""

    # Institutional Control Brain (ICB)
    icb_timeout_sec: float = 25.0
    icb_max_trades_per_hour: int = 20
    icb_trade_window_minutes: int = 60
    icb_drift_restrict_threshold: int = 1
    icb_heat_degrade_pct: float = 3.0
    icb_degraded_size_multiplier: float = 0.5
    icb_autonomous_buy_spike: int = 8


@lru_cache
def get_settings() -> Settings:
    return Settings()
