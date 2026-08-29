import datetime as dt

import pandas as pd
import pytest


def _make_5m(rows: list[tuple], start: dt.datetime) -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=len(rows), freq="5min")
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)
    df["Volume"] = 0.0
    df.index.name = "ts"
    return df


def _bullish_break_fixture(extra_rows: list[tuple] | None = None):
    """5m candles from 10:30 onward: a swing high forms at 103 (idx2,
    confirmed by idx3/idx4 per window=2), then idx6 closes above it with a
    real displacement candle (body 2.7 vs ~0.6 average of the prior 6 bars)
    -> BOS BUY. idx7 is the next candle, whose OPEN (104.2) is the entry.
    Breaking candle's own low (101.2) is the SL; sl_points=3.0 -> TP =
    104.2 + 2*3.0 = 110.2 at the default risk_reward_multiple=2.0."""
    start = dt.datetime(2026, 1, 5, 10, 30)
    rows = [
        (100.0, 100.5, 99.5, 100.2),   # idx0 10:30
        (100.2, 100.8, 99.8, 100.5),   # idx1 10:35
        (100.5, 103.0, 100.3, 102.5),  # idx2 10:40 - swing high candidate H=103
        (102.5, 102.2, 101.8, 102.0),  # idx3 10:45 - confirms idx2 (H<103)
        (102.0, 101.8, 101.2, 101.5),  # idx4 10:50 - confirms idx2 (H<103)
        (101.5, 101.9, 101.0, 101.3),  # idx5 10:55
        (101.3, 104.5, 101.2, 104.0),  # idx6 11:00 - BREAK: closes 104.0 > 103, displacement candle
        (104.2, 104.8, 103.8, 104.5),  # idx7 11:05 - next candle, entry at its OPEN (104.2)
    ]
    rows.extend(extra_rows or [])
    return _make_5m(rows, start), start.date()


def test_run_day_no_setup_before_swing_start_time():
    from app.strategy.engine import run_day
    from app.strategy.types import TradeStatus

    day = dt.date(2026, 1, 5)
    # candles entirely before 10:30 - the whole session gets trimmed away
    candles = _make_5m([(100, 101, 99, 100)] * 6, dt.datetime(2026, 1, 5, 9, 15))

    result = run_day(day, candles)

    assert result.status is TradeStatus.NO_SETUP
    assert result.structure is None


def test_run_day_no_setup_when_no_break_occurs():
    from app.strategy.engine import run_day
    from app.strategy.types import TradeStatus

    day = dt.date(2026, 1, 5)
    # flat candles after 10:30 - no swing ever gets broken
    candles = _make_5m([(100, 100.5, 99.5, 100.1)] * 10, dt.datetime(2026, 1, 5, 10, 30))

    result = run_day(day, candles)

    assert result.status is TradeStatus.NO_SETUP
    assert result.structure is None


def test_run_day_detects_bullish_break_and_enters_at_next_candle_open():
    from app.strategy.engine import run_day
    from app.strategy.types import Direction, EntryType, StructureType, TradeStatus

    candles, day = _bullish_break_fixture()
    result = run_day(day, candles)

    assert result.structure is not None
    assert result.structure.structure_type is StructureType.SWING_BREAK
    assert result.direction is Direction.BUY
    assert result.structure.broken_swing.price == 103.0

    assert result.entry is not None
    assert result.entry.entry_type is EntryType.NEXT_CANDLE_OPEN
    assert result.entry.entry_time == candles.index[7]
    assert result.entry.entry_price == 104.2

    assert result.risk is not None
    assert result.risk.stop_loss == pytest.approx(94.2)   # entry - fixed 10pt SL
    assert result.risk.take_profit == pytest.approx(124.2)  # entry + fixed 20pt TP


def test_run_day_resolves_target_hit():
    from app.strategy.engine import run_day
    from app.strategy.types import TradeStatus

    candles, day = _bullish_break_fixture(extra_rows=[
        (104.5, 105.0, 104.0, 104.8),   # idx8
        (104.8, 125.0, 104.5, 124.5),   # idx9 - high=125 clears TP=124.2
    ])
    result = run_day(day, candles)

    assert result.status is TradeStatus.TARGET_HIT
    assert result.exit_price == pytest.approx(124.2)
    assert result.pnl_points == pytest.approx(124.2 - 104.2)


def test_run_day_resolves_stop_hit():
    from app.strategy.engine import run_day
    from app.strategy.types import TradeStatus

    candles, day = _bullish_break_fixture(extra_rows=[
        (104.5, 104.6, 93.0, 93.5),   # idx8 - low=93.0 clears SL=94.2
    ])
    result = run_day(day, candles)

    assert result.status is TradeStatus.STOP_HIT
    assert result.exit_price == pytest.approx(94.2)
    assert result.pnl_points == pytest.approx(94.2 - 104.2)


def test_run_day_manual_exit_when_session_ends_first():
    from app.strategy.engine import run_day
    from app.strategy.types import TradeStatus

    candles, day = _bullish_break_fixture(extra_rows=[
        (104.5, 105.0, 104.0, 104.8),   # idx8 - neither SL nor TP touched
    ])
    result = run_day(day, candles)

    assert result.status is TradeStatus.MANUAL_EXIT
    assert result.exit_price == 104.8


def test_run_day_no_setup_when_break_candle_is_not_a_displacement_candle(monkeypatch):
    """A close beyond the swing that ISN'T backed by a long body doesn't
    count as a break - explicit spec: "break means... explosive candle"."""
    from app.strategy.engine import run_day
    from app.strategy.types import TradeStatus

    start = dt.datetime(2026, 1, 5, 10, 30)
    rows = [
        (100.0, 100.5, 99.5, 100.2),
        (100.2, 100.8, 99.8, 100.5),
        (100.5, 103.0, 100.3, 102.5),  # swing high candidate H=103
        (102.5, 102.2, 101.8, 102.0),
        (102.0, 101.8, 101.2, 101.5),
        (101.5, 101.9, 101.0, 101.3),
        (102.9, 103.3, 102.8, 103.1),  # closes just above 103 but with a tiny body (0.2 vs ~0.6 baseline) - no displacement
    ]
    candles = _make_5m(rows, start)

    result = run_day(dt.date(2026, 1, 5), candles)

    assert result.status is TradeStatus.NO_SETUP
    assert result.structure is None
