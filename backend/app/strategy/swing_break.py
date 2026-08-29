"""Detects the core "10:30 Candle" signal on 5-minute candles: from
settings.swing_start_time onward, track fractal swing highs/lows as they
confirm, and fire the first bar whose CLOSE breaks beyond the most recently
confirmed swing - high or low - PROVIDED that breaking candle itself shows
real displacement (a "long body" vs. recent bars, see structure.
_is_displacement_candle). A break above a swing high -> BUY; below a swing
low -> SELL. This is deliberately a single-stage detector (no separate
liquidity-tap vs. structure-confirm split like the sibling projects) -
entry itself is NOT this bar, it's the open of the very next candle (see
engine.run_day).
"""
from __future__ import annotations

import pandas as pd

from app.config import settings
from app.strategy.structure import _is_displacement_candle
from app.strategy.swings import confirmed_swings_as_of, last_swing
from app.strategy.types import Direction, LiquiditySide, StructureEvent, StructureType


def find_swing_break(candles_5m: pd.DataFrame, window: int, search_bar_limit: int) -> StructureEvent | None:
    """`candles_5m` must already be trimmed to start at/after
    settings.swing_start_time. Scans forward bar by bar (no look-ahead) for
    the first close beyond a confirmed swing high/low with displacement.
    Returns None if nothing qualifies within `search_bar_limit` bars."""
    n = min(len(candles_5m), search_bar_limit)
    if n < window * 2 + 1:
        return None

    closes = candles_5m["Close"].to_numpy()

    for i in range(window, n):
        swings_so_far = confirmed_swings_as_of(candles_5m, window, i - 1)
        close = float(closes[i])
        ts = candles_5m.index[i]

        swing_high = last_swing(swings_so_far, "high")
        swing_low = last_swing(swings_so_far, "low")
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
