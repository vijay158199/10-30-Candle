# 10:30 Candle Strategy

A local Python + web application, forked from the sibling **NIFTY 50 ICT/SMC intraday** project (same
codebase, same dashboard) but implementing a different, much simpler intraday strategy: a 5-minute
swing-break breakout, watched only from 10:30 IST onward. It runs a live signal monitor during market
hours, a historical backtester, and a dashboard - all on your own machine.

**This system only generates and logs signals. It never places real broker orders.**

## Strategy Recap

1. From 10:30 IST onward (`swing_start_time`), track fractal swing highs/lows on 5-minute candles.
2. The first 5-minute candle to CLOSE beyond the most recently confirmed swing - high or low - fires the
   signal, but ONLY if that breaking candle itself is a real displacement/"explosive" candle (a long body
   vs. recent bars, not just a routine close beyond the level). A break above a swing high is a BUY
   signal; below a swing low is a SELL signal.
3. Entry is at the OPEN of the very next 5-minute candle after the break - no retracement, no FVG/CISD/
   Order Block concept, straight into the breakout.
4. Risk: fixed `stop_loss_points`/`take_profit_points` from entry (10/20 by default, a 1:2 R:R) - not
   tied to the breaking candle's own range. An earlier version derived SL from the breaking candle's own
   opposite extreme instead; a 60-day backtest showed the fixed-point version clearly outperforms it
   (41.9% win / +110.0pts / 43 trades vs. 26.8% win / -82.8pts / 41 trades, with drawdown down from
   286.8pts to 50.0pts) - kept as the default.

See `docs` inline in `backend/app/strategy/*.py` for how each concept is implemented - every module has a
short docstring explaining the exact rule it applies.

## Project Layout

```
backend/app/
  config.py        # all tunables (capital, risk %, SL/TP points, lot size, priorities, etc.)
  data/            # yfinance fetch + local SQLite candle cache + NSE trading calendar + 30m resampling
  strategy/        # the strategy engine (swings, structure/displacement, swing-break detection, orchestration)
  models/          # SQLAlchemy schema + DB session helper
  backtest/        # replays the engine over a date range, computes stats, writes the Excel workbook
  live/            # market-hours polling monitor + APScheduler jobs (session, daily 17:00 report)
  reports/         # Excel report generation + per-trade chart snapshot rendering
  api/              # FastAPI routes + read-side query helpers
backend/tests/      # pytest unit tests for every strategy module (synthetic OHLC fixtures)
frontend/           # Jinja2 templates + CSS + vendored htmx/Alpine/Chart.js (no Node/npm needed)
data/                # created at runtime: sqlite DB, generated Excel reports, trade chart snapshots, logs
```

## Setup

Requires Python 3.11+ (tested on 3.13) on Windows.

```powershell
cd "10.30 Candle"
python -m venv venv
venv\Scripts\pip install -r backend\requirements.txt
```

## Running

```powershell
cd backend
..\venv\Scripts\python run.py
```

Then open **http://localhost:8000**. The scheduler starts automatically:

- Every `poll_interval_seconds` (default 60s) during 09:15-15:30 IST on trading days, it polls the latest
  candles and re-evaluates the day's setup.
- At 15:30 IST it marks the session stopped.
- At **17:00 IST** (matching the spec's daily run) it finalizes and writes that day's Excel monitoring
  sheet to `data/reports/`.

The dashboard itself also works with the scheduler paused/off - "Refresh Now" on the Overview page and
the Backtest page work independently of it.

## Running Tests

```powershell
cd backend
..\venv\Scripts\python -m pytest -v
```

25 tests cover swing/fractal detection, the swing-break-with-displacement detector, the full run_day
pipeline (break detection, next-candle-open entry, fixed SL/TP, exit simulation - target/stop/manual-exit
outcomes), and the backtest statistics engine.

## Data Source & Its Limits (important)

Historical/live candles come from **Yahoo Finance via `yfinance`** - no broker account or API key needed.
This strategy runs entirely on 5-minute candles, which Yahoo serves for the trailing **~60 days** - a
backtest further back than that will simply have no data for those days. Once a day has been fetched
once, it's cached locally in SQLite and stays available even after Yahoo's window rolls past it. If Yahoo
Finance access ever becomes unreliable, `jugaad-data`'s `nse` module is a solid drop-in alternative for
NSE-native historical/intraday data - the only file that would need to change is
`backend/app/data/fetcher.py`.

## Configuration

Everything tunable lives in `backend/app/config.py` and can be overridden via environment variables
prefixed `NIFTY_` (e.g. `NIFTY_ACCOUNT_CAPITAL=200000`, `NIFTY_RISK_PCT_PER_TRADE=0.5`) or a `.env` file
in `backend/`. Key ones:

| Setting | Default | Meaning |
|---|---|---|
| `swing_start_time` | 10:30 | Only 5m candles at/after this wall-clock time are watched for swings/breaks |
| `swing_fractal_window` | 2 | Bars either side to confirm a fractal swing point on the 5m chart |
| `require_displacement_candle` | True | Requires the breaking candle to have a long body (strong displacement) |
| `displacement_body_multiplier` | 3.0 | Breaking candle's body must be >= this x the recent average |
| `stop_loss_points` / `take_profit_points` | 10 / 20 | Fixed risk management from entry (a 1:2 R:R) |
| `account_capital` | 100000 | Used with `risk_pct_per_trade` to size positions |
| `risk_pct_per_trade` | 1.0 | % of capital risked per trade |
| `lot_size` | 75 | Points-to-currency multiplier per lot |
| `poll_interval_seconds` | 60 | Live monitor polling cadence |
| `daily_report_time` | 17:00 | IST time the daily Excel report job fires |

## Notes on What Was Validated

The engine was smoke-tested against real recent NIFTY data (not just synthetic fixtures), through the
actual live/backtest code paths, not a scratch script. Two risk models were tried on the same 60-day
window: SL derived from the breaking candle's own range (26.8% win / -82.8 net pts / 41 trades / 286.8pt
max drawdown) vs. the fixed 10/20-point model (41.9% win / +110.0 net pts / 43 trades / 50.0pt
max drawdown) - the fixed-point version won clearly on every metric.

Two alternate stop-loss placements (breakout candle's own low/high, and the broken swing level itself,
both with TP re-derived as 2x that wider SL distance) were also tried and both lost on every metric to
the fixed 10pt SL - wider structural stops meant a farther TP to reach, which cut win rate sharply
(27.9% and 25.6% respectively) despite unchanged 1:2 R:R.

A parameter sweep over `swing_fractal_window` (1-4), `displacement_body_multiplier` (1.2-4.0), and
`swing_start_time` (09:30-11:30) on the same 60-day window found that raising the displacement
multiplier from 1.5 to 3.0 - filtering out marginal, low-conviction breaks - improved results on every
metric (45.9% win / +161.4 net pts, vs 41.9% / +110.0 at 1.5), at the cost of ~14% fewer signals (37
trades vs 43) and a higher max drawdown (70pt vs 50pt). This is now the default. Multipliers beyond 3.0
(3.5, 4.0) overshoot - too few signals survive and results degrade again.

## Future Enhancements (not built, by design - out of scope for a first version)

- Real broker order execution (would need explicit re-authorization given this is currently signal-only).
- Multi-instrument support beyond NIFTY/BANKNIFTY.
- User accounts/auth (currently single-user, localhost-only, no auth by design).
