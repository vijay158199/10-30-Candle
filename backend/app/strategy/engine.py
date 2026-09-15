"""Orchestrates the "10:30 Candle" pipeline for a single trading day:

  The fractal swing high/low that formed and confirmed BEFORE settings.
  swing_start_time (10:30 IST) is the fixed reference level for the day ->
  from 10:30 onward, the first 5-minute candle to CLOSE beyond that level,
  with real displacement (a "long body"), fires the signal - direction
  follows the side broken (BUY on a high break, SELL on a low break)
  (swing_break.find_swing_break)
    -> entry at the OPEN of the very next 5-minute candle
      -> risk: fixed settings.stop_loss_points/take_profit_points from
        entry (10/20 by default) - not tied to the breaking candle's own
        range or any leg/retracement concept
        -> exit simulation (SL/TP walk-forward on the same 5m candles)

This module is shared verbatim by the live monitor and the backtester -
only where the candle DataFrame comes from differs, keeping live and
backtest behaviour guaranteed consistent (same pattern as the sibling
projects).
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd

from app.config import settings
from app.strategy import swing_break as swing_break_mod
from app.strategy.types import Direction, EntrySignal, EntryType, RiskPlan, TradeResult, TradeStatus


def run_day(
    trade_date: dt.date,
    candles_5m: pd.DataFrame,
    symbol: str = settings.primary_symbol,
    symbol_label: str = settings.primary_label,
    reduced_resolution: bool = False,
    stop_loss_points: float | None = None,
    take_profit_points: float | None = None,
) -> TradeResult:
    """Runs the full pipeline for one session and returns a single
    TradeResult (possibly with status NO_SETUP if nothing qualified).
    `candles_5m` should cover the full session (or as much of it as is
    available so far, for a live poll).

    `stop_loss_points`/`take_profit_points` override settings.stop_loss_points/
    take_profit_points for this call only - used by the Backtest page's risk
    profile selector (see config.BACKTEST_RISK_PROFILES) to compare presets
    without touching the live monitor's default, which always calls this
    without them."""
    result = TradeResult(trade_date=trade_date, symbol=symbol, symbol_label=symbol_label,
                          reduced_resolution=reduced_resolution)

    if candles_5m.empty:
        result.status = TradeStatus.NO_SETUP
        result.notes.append("No 5m data available for this session.")
        return result

    start_h, start_m = (int(x) for x in settings.swing_start_time.split(":"))
    cutoff = dt.datetime.combine(trade_date, dt.time(start_h, start_m))
    before_cutoff = candles_5m[candles_5m.index < cutoff]
    onward = candles_5m[candles_5m.index >= cutoff]
    if onward.empty:
        result.status = TradeStatus.NO_SETUP
        result.notes.append(f"No 5m candles at/after {settings.swing_start_time} yet.")
        return result
    if before_cutoff.empty:
        result.status = TradeStatus.NO_SETUP
        result.notes.append(f"No 5m candles before {settings.swing_start_time} to mark a swing high/low against.")
        return result

    # --- Stage 1: swing break with displacement, on/after the 10:30 cutoff,
    # measured against the swing high/low that formed BEFORE the cutoff -----
    cutoff_index = len(before_cutoff)
    structure_event = swing_break_mod.find_swing_break(
        candles_5m, cutoff_index, settings.swing_fractal_window, len(onward)
    )
    if structure_event is None:
        result.status = TradeStatus.NO_SETUP
        result.notes.append(
            f"No 5m candle closed beyond the swing high/low tracked before {settings.swing_start_time} with real displacement."
        )
        return result
    result.structure = structure_event
    result.direction = structure_event.direction

    # --- Stage 2: entry at the OPEN of the very next 5m candle -----------------
    break_pos = candles_5m.index.get_loc(structure_event.ts)
    if break_pos + 1 >= len(candles_5m):
        result.status = TradeStatus.NO_SETUP
        result.notes.append("Swing break confirmed but the session ended before the next candle opened.")
        return result
    next_ts = candles_5m.index[break_pos + 1]
    entry_price = float(candles_5m.iloc[break_pos + 1]["Open"])
    result.entry = EntrySignal(
        entry_type=EntryType.NEXT_CANDLE_OPEN,
        direction=structure_event.direction,
        entry_time=next_ts,
        entry_price=entry_price,
        reason=f"Entered at the open of the candle after the {structure_event.signal_label} break.",
    )

    # --- Stage 3: risk - fixed SL/TP points from entry ------------------------
    sl_points = stop_loss_points if stop_loss_points is not None else settings.stop_loss_points
    tp_points = take_profit_points if take_profit_points is not None else settings.take_profit_points
    if structure_event.direction is Direction.BUY:
        stop_loss = entry_price - sl_points
        take_profit = entry_price + tp_points
    else:
        stop_loss = entry_price + sl_points
        take_profit = entry_price - tp_points

    risk_amount = settings.account_capital * (settings.risk_pct_per_trade / 100.0)
    points_at_risk_per_lot = sl_points * settings.lot_size
    position_size_lots = max(1, math.floor(risk_amount / points_at_risk_per_lot)) if points_at_risk_per_lot > 0 else 1

    result.risk = RiskPlan(
        stop_loss=stop_loss, take_profit=take_profit,
        position_size_lots=position_size_lots, risk_amount=risk_amount,
    )
    result.status = TradeStatus.OPEN

    # --- Stage 4: exit simulation (walk forward on 5m candles from entry) -----
    _simulate_exit(result, candles_5m)
    return result


def _simulate_exit(result: TradeResult, candles_5m: pd.DataFrame) -> None:
    """Walk 5m candles forward from the entry bar, exiting on whichever of
    SL/TP is touched first; if the session ends with the trade still open,
    mark it MANUAL_EXIT at the last available close (paper-trading
    convention: flatten at session end)."""
    entry = result.entry
    risk = result.risk
    direction = result.direction
    assert entry is not None and risk is not None and direction is not None

    after_entry = candles_5m[candles_5m.index >= entry.entry_time]
    for ts, row in after_entry.iterrows():
        low, high = float(row["Low"]), float(row["High"])
        if direction is Direction.BUY:
            hit_sl = low <= risk.stop_loss
            hit_tp = high >= risk.take_profit
        else:
            hit_sl = high >= risk.stop_loss
            hit_tp = low <= risk.take_profit

        # Conservative convention when both could occur in the same bar:
        # assume the stop is hit first (protects against overstating results).
        if hit_sl and hit_tp:
            result.exit_time = ts
            result.exit_price = risk.stop_loss
            result.exit_reason = "Stop-loss and target both in range on the same candle; stop assumed hit first."
            result.status = TradeStatus.STOP_HIT
            return
        if hit_sl:
            result.exit_time = ts
            result.exit_price = risk.stop_loss
            result.exit_reason = f"Stop-loss touched at {risk.stop_loss:.1f}."
            result.status = TradeStatus.STOP_HIT
            return
        if hit_tp:
            result.exit_time = ts
            result.exit_price = risk.take_profit
            result.exit_reason = f"Take-profit touched at {risk.take_profit:.1f}."
            result.status = TradeStatus.TARGET_HIT
            return

    if not after_entry.empty:
        last_ts = after_entry.index[-1]
        last_close = float(after_entry.iloc[-1]["Close"])
        result.exit_time = last_ts
        result.exit_price = last_close
        result.exit_reason = "Session ended before SL/TP was hit; flattened at last available close."
        result.status = TradeStatus.MANUAL_EXIT
    else:
        result.status = TradeStatus.AWAITING_ENTRY
        result.notes.append("Entry filled but no further 5m candles available yet to simulate an exit.")
