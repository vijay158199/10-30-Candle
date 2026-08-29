"""Central configuration for the strategy system.

All tunables live here so the engine, backtester, live monitor and
dashboard share a single source of truth. Values can be overridden via
environment variables (or a .env file) using the ``NIFTY_`` prefix, e.g.
``NIFTY_CAPITAL=200000``.
"""
import os
import secrets
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
REPORTS_DIR = DATA_DIR / "reports"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
LOGS_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "nifty_strategy.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NIFTY_", env_file=".env", extra="ignore")

    # --- Instruments -------------------------------------------------
    primary_symbol: str = "^NSEI"          # NIFTY 50 index (yfinance ticker)
    primary_label: str = "NIFTY 50"
    confirm_symbol: str = "^NSEBANK"       # BANKNIFTY, used only for SMT divergence
    confirm_label: str = "BANKNIFTY"

    # --- Session (IST) -------------------------------------------------
    session_start: str = "09:15"
    session_end: str = "15:30"
    timezone: str = "Asia/Kolkata"
    # Unused by this strategy's own logic (no first-candle-liquidity
    # concept here) but data/fetcher.py's session-aligned 30m resampling
    # utility still needs some value - kept at the sibling projects'
    # default rather than touching that shared utility.
    first_candle_minutes: int = 60

    # --- Strategy parameters -------------------------------------------
    # "10:30 Candle" strategy (explicit user spec, 2026-08-29): everything
    # runs on 5-minute candles - no separate structure-timeframe selector
    # like the sibling projects.
    structure_interval: str = "5m"
    # Only 5m candles at/after this wall-clock time are considered for
    # swing tracking/breaks - "see 10:30 candle... mark previous 5min swing
    # high and low (after 10.30)".
    swing_start_time: str = "10:30"
    # Bars either side needed to confirm a fractal swing point on the 5m
    # chart before a break can be evaluated against it.
    swing_fractal_window: int = 2
    # The breaking candle itself must show strong displacement (a "long
    # body" vs. recent bars, see structure._is_displacement_candle) -
    # explicit user spec: "break means 5min candle should close the swing
    # low or high WITH EXPLOSIVE CANDLE" - a routine close beyond the swing
    # without real displacement doesn't count as a break.
    require_displacement_candle: bool = True
    displacement_lookback_bars: int = 20       # prior 5m bars used for the average-body baseline
    # Breaking candle's body must be >= this x the recent-average body.
    # Raised from 1.5 to 3.0 (2026-08-29): backtested against 1.2-4.0 on 60
    # days of real data - 3.0 gave the best win rate (45.9% vs 41.9%) and net
    # points (+161.4 vs +110.0), trading off ~14% fewer signals and a higher
    # max drawdown (70 vs 50pts). Going past 3.0 (tried 3.5, 4.0) overshoots -
    # too few signals survive and results degrade again.
    displacement_body_multiplier: float = 3.0

    # --- Risk management -------------------------------------------------
    # Fixed-point SL/TP (explicit user spec, 2026-08-29, changed from the
    # breaking candle's own range): every trade risks exactly
    # stop_loss_points and targets exactly take_profit_points, regardless
    # of how wide or narrow the actual breaking candle was - tested against
    # the candle-range-based version (26.8% win / -82.8pts / 41 trades over
    # 60 days) to see whether a fixed, predictable distance improves on
    # that. See strategy/engine.py.
    stop_loss_points: float = 10.0
    take_profit_points: float = 20.0
    account_capital: float = 100_000.0
    risk_pct_per_trade: float = 1.0        # percent of capital risked per trade
    lot_size: int = 75                     # NIFTY point value per lot

    # --- Data ------------------------------------------------------------
    yfinance_1m_lookback_days: int = 30    # Yahoo hard limit for 1m candles (only relevant if structure_interval="1m")
    yfinance_5m_lookback_days: int = 60    # Yahoo hard limit for 5m candles (30m candles are derived from these)
    candle_cache_ttl_minutes: int = 5
    backtest_lookback_days: int = 60

    # --- Live monitor / scheduler ----------------------------------------
    poll_interval_seconds: int = 60
    daily_report_time: str = "17:00"       # IST, matches the spec's 5 PM run
    max_fetch_retries: int = 3

    # --- Auth (single local user) -----------------------------------------
    # Override both via env / fly secrets (NIFTY_AUTH_USERNAME / NIFTY_AUTH_PASSWORD) -
    # this is a local single-user gate, not multi-tenant auth. MUST be
    # overridden before deploying publicly - the defaults are not secret.
    auth_username: str = "vijay"
    auth_password: str = "changeme123"

    # --- Paths -------------------------------------------------------------
    data_dir: Path = DATA_DIR
    reports_dir: Path = REPORTS_DIR
    snapshots_dir: Path = SNAPSHOTS_DIR
    logs_dir: Path = LOGS_DIR
    db_path: Path = DB_PATH

    @field_validator("risk_pct_per_trade")
    @classmethod
    def _risk_in_range(cls, v: float) -> float:
        if not (0 < v <= 100):
            raise ValueError("risk_pct_per_trade must be between 0 and 100")
        return v

    @field_validator("account_capital")
    @classmethod
    def _capital_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("account_capital must be positive")
        return v

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.reports_dir, self.snapshots_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()

# Backtest-page-only risk presets (the live monitor always uses
# stop_loss_points/take_profit_points above - these never change that).
# Added 2026-08-30: a grid search found SL=15/TP=12 beats the live default
# on win rate, net points, AND drawdown simultaneously (wider stop survives
# 5m noise instead of getting clipped early; closer target is reached
# faster) - user wanted to compare both from the Backtest page before
# deciding whether to make it the new live default.
BACKTEST_RISK_PROFILES: dict[str, dict] = {
    "current": {"label": "Current live (SL=10, TP=20)", "sl": 10.0, "tp": 20.0},
    "new": {"label": "New (SL=15, TP=12)", "sl": 15.0, "tp": 12.0},
}


def get_session_secret() -> str:
    """A signing key for the login session cookie. Persisted to a local file
    (on the persistent volume in production) so sessions survive process
    restarts, rather than regenerated (and thus invalidated) every time the
    server boots. Can also be pinned via NIFTY_SESSION_SECRET so it survives
    even a fresh volume."""
    env_secret = os.environ.get("NIFTY_SESSION_SECRET")
    if env_secret:
        return env_secret
    secret_path = DATA_DIR / ".session_secret"
    if secret_path.exists():
        return secret_path.read_text().strip()
    secret = secrets.token_hex(32)
    secret_path.write_text(secret)
    return secret
