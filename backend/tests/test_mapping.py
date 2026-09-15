import datetime as dt

from app.strategy.mapping import row_to_trade_result, trade_result_to_row
from app.strategy.types import (
    Direction,
    EntrySignal,
    EntryType,
    RiskPlan,
    TradeResult,
    TradeStatus,
)


def _sample_result() -> TradeResult:
    entry = EntrySignal(
        entry_type=EntryType.NEXT_CANDLE_OPEN,
        direction=Direction.SELL,
        entry_time=dt.datetime(2026, 9, 2, 12, 15),
        entry_price=23838.8,
        reason="Entered at the open of the candle after the Swing Break Low break.",
    )
    risk = RiskPlan(stop_loss=23853.8, take_profit=23818.8, position_size_lots=4, risk_amount=1000.0)
    result = TradeResult(
        trade_date=dt.date(2026, 9, 2),
        symbol="^NSEI",
        symbol_label="NIFTY 50",
        direction=Direction.SELL,
        entry=entry,
        risk=risk,
        exit_time=dt.datetime(2026, 9, 2, 12, 45),
        exit_price=23818.8,
        exit_reason="Take-profit touched at 23818.8.",
        status=TradeStatus.TARGET_HIT,
    )
    return result


def test_row_to_trade_result_reproduces_pnl_points_exactly():
    """The whole point of the mapping.row_to_trade_result() reconstruction
    (used by queries.regenerate_snapshots to rebuild a trade's chart) is
    that it must reproduce the SAME pnl_points/status the row already
    displays - a round trip through trade_result_to_row -> a stored-row
    dict -> row_to_trade_result must never drift."""
    original = _sample_result()
    assert original.pnl_points == 20.0

    row = trade_result_to_row(original, source="backtest", backtest_run_id=11)
    row["id"] = 999  # only present on an actually-persisted row, not the engine dict

    rebuilt = row_to_trade_result(row)

    assert rebuilt.pnl_points == original.pnl_points == 20.0
    assert rebuilt.status is TradeStatus.TARGET_HIT
    assert rebuilt.direction is Direction.SELL
    assert rebuilt.entry.entry_price == 23838.8
    assert rebuilt.risk.stop_loss == 23853.8
    assert rebuilt.risk.take_profit == 23818.8
    assert rebuilt.structure is None  # not a stored column - deliberately omitted


def test_row_to_trade_result_handles_a_no_setup_row():
    """A NO_SETUP day has no entry/risk at all - the reconstruction must not
    blow up on nulls (queries.regenerate_snapshots only calls this for rows
    that DO have an entry_time, but the function itself should still be
    defensive)."""
    no_setup = TradeResult(
        trade_date=dt.date(2026, 9, 1),
        symbol="^NSEI",
        symbol_label="NIFTY 50",
        status=TradeStatus.NO_SETUP,
    )
    row = trade_result_to_row(no_setup, source="backtest", backtest_run_id=11)
    row["id"] = 1000

    rebuilt = row_to_trade_result(row)

    assert rebuilt.entry is None
    assert rebuilt.risk is None
    assert rebuilt.pnl_points is None
    assert rebuilt.status is TradeStatus.NO_SETUP
