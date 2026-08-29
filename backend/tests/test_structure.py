import datetime as dt

from tests.conftest import make_candles


def test_is_displacement_candle_true_for_a_long_body(session_start):
    from app.strategy.structure import _is_displacement_candle

    rows = [
        (100, 101, 99, 100.2),   # body 0.2
        (100.2, 101, 99.5, 100.4),  # body 0.2
        (100.4, 101, 99.5, 100.6),  # body 0.2
        (100.6, 106, 100.5, 105.5),  # body 4.9 - well over 1.5x the ~0.2 baseline
    ]
    candles = make_candles(rows, session_start, 1)

    assert _is_displacement_candle(candles, 3, lookback=20, multiplier=1.5)


def test_is_displacement_candle_false_for_a_routine_body(session_start):
    from app.strategy.structure import _is_displacement_candle

    rows = [
        (100, 101, 99, 100.5),   # body 0.5
        (100.5, 101.5, 99.5, 101.0),  # body 0.5
        (101.0, 102.0, 100.0, 101.5),  # body 0.5
        (101.5, 102.2, 101.3, 102.0),  # body 0.5 - not a long body relative to baseline
    ]
    candles = make_candles(rows, session_start, 1)

    assert not _is_displacement_candle(candles, 3, lookback=20, multiplier=1.5)


def test_is_displacement_candle_false_with_no_prior_bars():
    from app.strategy.structure import _is_displacement_candle

    rows = [(100, 105, 99, 104)]
    candles = make_candles(rows, dt.datetime(2026, 1, 5, 10, 30), 1)

    # bar 0 has no prior bars to compare against - rejected, not assumed to pass
    assert _is_displacement_candle(candles, 0, lookback=20, multiplier=1.5) is False
