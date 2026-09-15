"""Detects the core "10:30 Candle" signal on 5-minute candles: the fractal
swing high/low that already formed and confirmed BEFORE settings.
swing_start_time (10:30) is the fixed reference level for the session - from
10:30 onward we just watch for the first bar whose CLOSE breaks beyond that
pre-10:30 level, PROVIDED that breaking candle itself shows real
displacement (a "long body" vs. recent bars, see structure.
_is_displacement_candle). A break above the pre-10:30 swing high -> BUY;
below the pre-10:30 swing low -> SELL. This is deliberately a single-stage
detector (no separate liquidity-tap vs. structure-confirm split like the
sibling projects) - entry itself is NOT this bar, it's the open of the very
next candle (see engine.run_day).
"""
from __future__ import annotations

import pandas as pd

from app.config import settings
from app.strategy.structure import _is_displacement_candle
from app.strategy.swings import find_swings, last_swing
from app.strategy.types import Direction, LiquiditySide, StructureEvent, StructureType


def find_swing_break(candles_5m: pd.DataFrame, cutoff_index: int, window: int, search_bar_limit: int) -> StructureEvent | None:
    """`candles_5m` must cover the full session (pre-cutoff candles included,
    not trimmed). `cutoff_index` is the positional index of the first candle
    at/after settings.swing_start_time.

    The swing high/low to trade against is computed once, from the
    pre-cutoff candles only (`candles_5m.iloc[:cutoff_index]`) - by the time
    we reach the cutoff that whole window is already closed, so no
    look-ahead concern applies there, unlike a swing still forming live.
    Scans forward bar by bar from the cutoff (no look-ahead on this side
    either) for the first close beyond that fixed level with displacement.
    Returns None if there's no valid pre-cutoff swing, or nothing qualifies
    within `search_bar_limit` bars after the cutoff."""
    pre_cutoff = candles_5m.iloc[:cutoff_index]
    if len(pre_cutoff) < window * 2 + 1:
        return None

    pre_swings = find_swings(pre_cutoff, window)
    swing_high = last_swing(pre_swings, "high")
    swing_low = last_swing(pre_swings, "low")
    if swing_high is None and swing_low is None:
        return None

    n = min(len(candles_5m), cutoff_index + search_bar_limit)
    closes = candles_5m["Close"].to_numpy()

    for i in range(cutoff_index, n):
        close = float(closes[i])
        ts = candles_5m.index[i]

        broke_high = swing_high is not None and close > swing_high.price
        broke_low = swing_low is not None and close < swing_low.price
        if not (broke_high or broke_low):
            continue

        is_displacement = not settings.require_displacement_candle or _is_displacement_candle(
            candles_5m, i, settings.displacement_lookback_bars, settings.displacement_body_multiplier
        )
        if not is_displacement:
            continue

        if broke_high and broke_low:
            # a single bar spanning both swings (wide-range bar) - treat
            # whichever the close sits closer to as the side that actually broke
            broke_low = abs(close - swing_low.price) <= abs(close - swing_high.price)
            broke_high = not broke_low

        if broke_high:
            return StructureEvent(StructureType.SWING_BREAK, Direction.BUY, LiquiditySide.HIGH, ts, swing_high)
        return StructureEvent(StructureType.SWING_BREAK, Direction.SELL, LiquiditySide.LOW, ts, swing_low)

    return None
