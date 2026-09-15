"""Maps an engine TradeResult to the flat dict shape shared by the `Trade`
DB table and the Excel report generator, so both the live monitor and the
backtester produce identically-shaped records from one place."""
from __future__ import annotations

import datetime as dt

from app.config import settings
from app.strategy.types import Direction, EntrySignal, EntryType, RiskPlan, TradeResult, TradeStatus


def trade_result_to_row(result: TradeResult, source: str, backtest_run_id: int | None = None) -> dict:
    entry = result.entry
    risk = result.risk
    trigger = result.trigger
    structure = result.structure

    return {
        "source": source,
        "backtest_run_id": backtest_run_id,
        "trade_date": result.trade_date,
        "symbol": result.symbol,
        "symbol_label": result.symbol_label,
        "direction": result.direction.value if result.direction else None,
        "trigger_time": trigger.trigger_time if trigger else None,
        "trigger_type": trigger.trigger_type.value if trigger else None,
        "liquidity_side": trigger.liquidity_side.value if trigger else None,
        "entry_time": entry.entry_time if entry else None,
        "entry_price": entry.entry_price if entry else None,
        "entry_type": entry.entry_type.value if entry else None,
        "entry_reason": entry.reason if entry else None,
        "stop_loss": risk.stop_loss if risk else None,
        "take_profit": risk.take_profit if risk else None,
        "position_size_lots": risk.position_size_lots if risk else None,
        "exit_time": result.exit_time,
        "exit_price": result.exit_price,
        "exit_reason": result.exit_reason,
        "pnl_points": result.pnl_points,
        "pnl_amount": result.pnl_amount,
        "rr_achieved": result.rr_achieved,
        "status": result.status.value,
        "reduced_resolution": result.reduced_resolution,
        "snapshot_path": None,  # filled in after chart rendering
        "mss_choch_bos": structure.structure_type.value if structure else None,
        "structure_time": structure.ts if structure else None,
        "explosive_candle_count": result.leg_candle_count,
        "smt_divergence": structure.smt_divergence if structure else False,
        "setup_notes": " ".join(result.notes) if result.notes else None,
    }


def row_to_trade_result(row: dict) -> TradeResult:
    """Inverse of trade_result_to_row: rebuilds a TradeResult from an
    already-persisted Trade row (dict-shaped, e.g. app.api.queries.
    _row_to_dict(trade)) - used to regenerate a trade's snapshot chart from
    exactly the numbers that row already shows, rather than re-running the
    strategy engine against cached candles (which could silently disagree
    with the original result if strategy logic changed after the row was
    first written, e.g. a swing-reference or SL/TP change made since).

    `structure` is deliberately left unset - the broken swing's price isn't
    its own stored column, so the regenerated chart omits that one
    annotation, but entry/SL/TP/exit and the WIN/LOSS badge - everything
    that drove the mismatch bug this exists to fix - are reproduced
    exactly."""
    trade_date = row["trade_date"]
    trade_date = trade_date.date() if isinstance(trade_date, dt.datetime) else trade_date

    direction = Direction(row["direction"]) if row.get("direction") else None

    entry = None
    if row.get("entry_time") is not None and row.get("entry_price") is not None and direction is not None:
        entry = EntrySignal(
            entry_type=EntryType(row["entry_type"]) if row.get("entry_type") else EntryType.NEXT_CANDLE_OPEN,
            direction=direction,
            entry_time=row["entry_time"],
            entry_price=row["entry_price"],
            reason=row.get("entry_reason") or "",
        )

    risk = None
    if row.get("stop_loss") is not None and row.get("take_profit") is not None:
        risk = RiskPlan(
            stop_loss=row["stop_loss"],
            take_profit=row["take_profit"],
            position_size_lots=row.get("position_size_lots") or 1,
            risk_amount=0.0,
        )

    return TradeResult(
        trade_date=trade_date,
        symbol=row["symbol"],
        symbol_label=row["symbol_label"],
        direction=direction,
        entry=entry,
        risk=risk,
        exit_time=row.get("exit_time"),
        exit_price=row.get("exit_price"),
        exit_reason=row.get("exit_reason"),
        status=TradeStatus(row["status"]),
        reduced_resolution=bool(row.get("reduced_resolution")),
    )
