"""Displacement-candle check, shared by swing_break.py: whether a given
5-minute candle's body is a "long body" (real momentum) relative to recent
bars, rather than just a routine close. This strategy has no BOS/CHOCH/MSS
continuation-vs-reversal concept - see swing_break.py for the actual
structural event this feeds into."""
from __future__ import annotations

import pandas as pd


def _is_displacement_candle(candles_5m: pd.DataFrame, i: int, lookback: int, multiplier: float) -> bool:
    """True if bar `i`'s body is at least `multiplier`x the average body of
    the up-to-`lookback` bars immediately before it - a "long body" showing
    strong displacement rather than a routine close beyond a swing point.
    With no prior bars to compare against, there's no baseline to judge
    "long" by, so it's rejected rather than assumed to pass."""
    start = max(0, i - lookback)
    window = candles_5m.iloc[start:i]
    if window.empty:
        return False
    avg_body = (window["Close"] - window["Open"]).abs().mean()
    if avg_body <= 0:
        return False
    body = abs(float(candles_5m.iloc[i]["Close"]) - float(candles_5m.iloc[i]["Open"]))
    return body >= avg_body * multiplier
