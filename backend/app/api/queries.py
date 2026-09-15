"""Read-side query helpers for the dashboard routes - keeps SQL/ORM code out
of the route handlers and returns plain dicts that templates and the shared
`compute_stats` function can both consume."""
from __future__ import annotations

import datetime as dt
import os

from sqlalchemy import delete as sa_delete
from sqlalchemy import desc, select

from typing import Callable

from app.backtest.stats import BacktestStats, compute_stats
from app.data.calendar import now_ist
from app.data.fetcher import get_session_data
from app.models.db import get_session, log_event
from app.models.schema import BacktestRun, ErrorLog, LiveHeartbeat, Trade
from app.reports import charts
from app.strategy.mapping import row_to_trade_result


def _row_to_dict(t: Trade) -> dict:
    return {c.name: getattr(t, c.name) for c in t.__table__.columns}


def get_live_trades(
    start: dt.date | None = None,
    end: dt.date | None = None,
    direction: str | None = None,
    entry_type: str | None = None,
    status: str | None = None,
    limit: int = 500,
) -> list[dict]:
    with get_session() as session:
        stmt = select(Trade).where(Trade.source == "live")
        if start:
            # datetime.min, not the bare date: a bare date binds as e.g.
            # "2026-08-04" which is lexicographically LESS than the stored
            # "2026-08-04 00:00:00.000000" (shorter string vs. its own
            # prefix), so a bare-date upper bound silently excludes every
            # row on that day - only >= happens to work by accident of
            # string comparison, which is what let this hide until now.
            stmt = stmt.where(Trade.trade_date >= dt.datetime.combine(start, dt.time.min))
        if end:
            stmt = stmt.where(Trade.trade_date <= dt.datetime.combine(end, dt.time.max))
        if direction:
            stmt = stmt.where(Trade.direction == direction)
        if entry_type:
            stmt = stmt.where(Trade.entry_type == entry_type)
        if status:
            stmt = stmt.where(Trade.status == status)
        # secondary sort on id: trade_date alone is a tie among every row for
        # the same day, and ties need a deterministic winner (the newest
        # row) rather than whatever order SQLite happens to return them in.
        stmt = stmt.order_by(desc(Trade.trade_date), desc(Trade.id)).limit(limit)
        rows = session.execute(stmt).scalars().all()
    return [_row_to_dict(t) for t in rows]


def get_today_trade() -> dict | None:
    today = now_ist().date()
    trades = get_live_trades(start=today, end=today, limit=1)
    return trades[0] if trades else None


_RESOLVED_STATUSES = {"TARGET_HIT", "STOP_HIT", "MANUAL_EXIT"}


_OUTCOME_LABELS = {
    "TARGET_HIT": "Target Hit",
    "STOP_HIT": "Stop-Loss Hit",
    "MANUAL_EXIT": "Session-End Exit",
}


def _fmt_time(ts: dt.datetime | None) -> str:
    return ts.strftime("%H:%M") if ts else "-"


def _fmt_price(v: float | None) -> str:
    return f"{v:.1f}" if v is not None else "-"


def _build_day_summary(trade: dict, structure: dict | None, entry: dict | None, outcome: dict | None) -> str:
    symbol = trade.get("symbol_label") or "NIFTY 50"

    if structure is None:
        return f"{symbol}: no 5m candle closed beyond the swing high/low tracked before {trade.get('swing_start_time') or '10:30'} with real displacement today - no setup."
    parts = [f"{symbol} broke the swing {structure['side']} with a {structure['bias'] or '-'} {structure['type'] or '-'} at {_fmt_time(structure['time'])}"]

    if entry is None:
        return ", ".join(parts) + ", but the session ended before the next candle opened - no trade taken."
    parts.append(
        f"entered at {_fmt_price(entry['price'])} (SL {_fmt_price(entry['stop_loss'])} / TP {_fmt_price(entry['target'])})"
    )

    if outcome is None:
        return ", ".join(parts) + " - still open."
    pnl = outcome.get("pnl_points")
    pnl_txt = f"{pnl:+.1f} pts" if pnl is not None else "-"
    parts.append(f"{outcome['hit']} at {_fmt_time(outcome['exit_time'])} - {outcome['result']} ({pnl_txt})")
    return ", ".join(parts) + "."


def get_pipeline_stage(trade: dict | None) -> dict:
    """How far today's setup has actually progressed through this
    strategy's 3 stages (Swing Break -> Entry -> Outcome - no separate
    "liquidity" stage here, unlike the sibling projects: the swing break
    itself is the first and only signal, immediately followed by entry at
    the next candle's open), the detail behind each stage, a plain-English
    one-line summary of the whole day, and the engine's own explanation for
    why it stopped where it did (TradeResult.notes, persisted as
    setup_notes) - drives the Overview page's pipeline view."""
    if trade is None:
        return {
            "reached": 0, "notes": None,
            "structure": None, "entry": None, "outcome": None,
            "summary": "No session data yet today.",
        }

    reached = 0
    structure = entry = outcome = None

    if trade.get("mss_choch_bos"):
        reached = 1
        structure = {
            "type": "Swing Break",
            "side": trade.get("liquidity_side"),
            "time": trade.get("structure_time"),
            "bias": "Bullish" if trade.get("direction") == "BUY" else ("Bearish" if trade.get("direction") == "SELL" else None),
        }

    if trade.get("entry_type"):
        reached = 2
        entry = {
            "type": trade.get("entry_type"),
            "price": trade.get("entry_price"),
            "target": trade.get("take_profit"),
            "stop_loss": trade.get("stop_loss"),
        }

    if trade.get("status") in _RESOLVED_STATUSES:
        reached = 3
        pnl = trade.get("pnl_points")
        outcome = {
            "hit": _OUTCOME_LABELS.get(trade["status"], trade["status"]),
            "result": "Win" if (pnl or 0) > 0 else "Loss",
            "status": trade.get("status"),
            "pnl_points": pnl,
            "pnl_amount": trade.get("pnl_amount"),
            "exit_time": trade.get("exit_time"),
        }

    return {
        "reached": reached,
        "notes": trade.get("setup_notes"),
        "structure": structure,
        "entry": entry,
        "outcome": outcome,
        "summary": _build_day_summary(trade, structure, entry, outcome),
    }


def get_live_stats() -> BacktestStats:
    return compute_stats(get_live_trades(limit=100_000))


def get_equity_curve(limit_days: int = 90) -> list[dict]:
    trades = get_live_trades(limit=100_000)
    resolved = sorted(
        (t for t in trades if t.get("status") in {"TARGET_HIT", "STOP_HIT", "MANUAL_EXIT"}),
        key=lambda t: t["trade_date"],
    )
    resolved = resolved[-limit_days:]
    curve = []
    equity = 0.0
    for t in resolved:
        equity += t["pnl_points"] or 0.0
        curve.append({"date": t["trade_date"].strftime("%Y-%m-%d") if hasattr(t["trade_date"], "strftime") else str(t["trade_date"]), "equity": round(equity, 1)})
    return curve


def get_heartbeat(day: dt.date | None = None) -> dict | None:
    day = day or now_ist().date()
    # Range, not exact equality: comparing a bare date against this
    # DateTime column via == was found to never match (confirmed on both
    # local SQLite and the deployed Turso DB) - this function silently
    # returned None for "today" every time before this fix.
    start = dt.datetime.combine(day, dt.time.min)
    end = dt.datetime.combine(day, dt.time.max)
    with get_session() as session:
        hb = session.execute(
            select(LiveHeartbeat)
            .where(LiveHeartbeat.trade_date >= start, LiveHeartbeat.trade_date <= end)
            .order_by(desc(LiveHeartbeat.id))
        ).scalars().first()
    if hb is None:
        return None
    return {"trade_date": hb.trade_date, "last_poll_at": hb.last_poll_at, "status": hb.status, "detail": hb.detail}


def get_recent_errors(limit: int = 100) -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(ErrorLog).order_by(desc(ErrorLog.created_at)).limit(limit)).scalars().all()
    return [{"level": r.level, "source": r.source, "message": r.message, "created_at": r.created_at} for r in rows]


def get_backtest_runs(limit: int = 25) -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(BacktestRun).order_by(desc(BacktestRun.created_at)).limit(limit)).scalars().all()
    return [
        {c.name: getattr(r, c.name) for c in r.__table__.columns}
        for r in rows
    ]


def get_backtest_run(run_id: int) -> dict | None:
    with get_session() as session:
        r = session.get(BacktestRun, run_id)
    if r is None:
        return None
    return {c.name: getattr(r, c.name) for c in r.__table__.columns}


def get_backtest_trades(run_id: int) -> list[dict]:
    with get_session() as session:
        rows = session.execute(
            select(Trade).where(Trade.backtest_run_id == run_id).order_by(Trade.trade_date)
        ).scalars().all()
    return [_row_to_dict(t) for t in rows]


def get_backtest_monthly(run_id: int) -> dict:
    trades = get_backtest_trades(run_id)
    return compute_stats(trades).monthly


def get_done_backtest_runs(limit: int = 25) -> list[dict]:
    return [r for r in get_backtest_runs(limit) if r["status"] == "DONE"]


def get_combined_trades(baseline_run_id: int | None, limit: int = 500) -> list[dict]:
    """All trades that make up the "track record" page: every day from the
    chosen backtest run (the pre-live history, if one is selected) followed
    by every live-monitored day since - one continuous, chronologically
    sorted log, each row tagged with its own `source` ("backtest"/"live")
    so the page can badge them distinctly."""
    baseline = get_backtest_trades(baseline_run_id) if baseline_run_id else []
    live = get_live_trades(limit=100_000)
    combined = baseline + live
    combined.sort(key=lambda t: (t["trade_date"], t["id"]))
    return combined[-limit:] if limit else combined


def get_combined_stats(baseline_run_id: int | None) -> dict:
    """Win rate / net P&L etc. computed three ways: over the backtest
    baseline alone, over live trades alone, and over both combined - so the
    Track Record page can show "how it did in backtesting vs how it's
    actually trading live" side by side as well as the headline combined
    number."""
    baseline = get_backtest_trades(baseline_run_id) if baseline_run_id else []
    live = get_live_trades(limit=100_000)
    return {
        "combined": compute_stats(baseline + live),
        "backtest": compute_stats(baseline),
        "live": compute_stats(live),
        "backtest_trade_count": len(baseline),
        "live_trade_count": len(live),
    }


def get_combined_equity_curve(baseline_run_id: int | None) -> tuple[list[dict], int]:
    """Cumulative-points equity curve spanning the backtest baseline (if
    any) followed by live trading, plus the index within that curve where
    live trading takes over (0 if the whole curve is live, len(curve) if
    there's no live data yet) - lets the page draw the backtest portion and
    the live portion of the same continuous line in different colors."""
    baseline = get_backtest_trades(baseline_run_id) if baseline_run_id else []
    live = get_live_trades(limit=100_000)

    def _resolved_sorted(trades: list[dict]) -> list[dict]:
        return sorted(
            (t for t in trades if t.get("status") in {"TARGET_HIT", "STOP_HIT", "MANUAL_EXIT"}),
            key=lambda t: t["trade_date"],
        )

    bt_resolved = _resolved_sorted(baseline)
    live_resolved = _resolved_sorted(live)

    tagged = [("backtest", t) for t in bt_resolved] + [("live", t) for t in live_resolved]
    curve: list[dict] = []
    equity = 0.0
    for source, t in tagged:
        equity += t["pnl_points"] or 0.0
        date = t["trade_date"]
        curve.append({
            "date": date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date),
            "equity": round(equity, 1),
            "source": source,
        })
    return curve, len(bt_resolved)


def _remove_snapshot_file(snapshot_path: str | None) -> None:
    if not snapshot_path:
        return
    try:
        os.remove(snapshot_path)
    except OSError:
        pass  # already gone, or path was never valid - not worth failing the delete over


def delete_trade(trade_id: int) -> bool:
    """Deletes a single trade row (live or backtest) and its snapshot image
    if any. Returns False if the id didn't exist."""
    with get_session() as session:
        trade = session.get(Trade, trade_id)
        if trade is None:
            return False
        _remove_snapshot_file(trade.snapshot_path)
        session.delete(trade)
    return True


def delete_all_live_trades() -> int:
    """Clears the entire live Trade Log. Returns the number of rows removed."""
    with get_session() as session:
        rows = session.execute(select(Trade).where(Trade.source == "live")).scalars().all()
        count = len(rows)
        for t in rows:
            _remove_snapshot_file(t.snapshot_path)
        session.execute(sa_delete(Trade).where(Trade.source == "live"))
    return count


def delete_backtest_run(run_id: int) -> bool:
    """Deletes a backtest run and all trades/snapshots attached to it."""
    with get_session() as session:
        run = session.get(BacktestRun, run_id)
        if run is None:
            return False
        trades = session.execute(select(Trade).where(Trade.backtest_run_id == run_id)).scalars().all()
        for t in trades:
            _remove_snapshot_file(t.snapshot_path)
        session.execute(sa_delete(Trade).where(Trade.backtest_run_id == run_id))
        session.delete(run)
    return True


def regenerate_snapshots(progress_cb: Callable[[int, int], None] | None = None) -> dict:
    """Re-renders every trade's snapshot chart under the collision-proof
    filename scheme (charts.render_trade_snapshot's run_tag) and updates
    each row's snapshot_path. Fixes the bug where two different runs
    sharing one (trade_date, symbol, entry_time) key silently overwrote
    each other's PNG on disk - a row's own pnl_points/status in the DB
    stayed correct, but the picture next to it could belong to an entirely
    different run (different SL/TP, different result).

    Rebuilds each chart from that row's OWN already-stored numbers (see
    mapping.row_to_trade_result) rather than re-running the strategy engine
    - so a regenerated chart can never disagree with the row it belongs to,
    even if strategy logic has changed since that row was first written.
    Safe to run repeatedly (fully idempotent - always re-derives fresh from
    the DB) and cheap enough to call after any bug affecting snapshots."""
    with get_session() as session:
        trades = session.execute(select(Trade).where(Trade.entry_time.is_not(None))).scalars().all()
        rows = [_row_to_dict(t) for t in trades]

    total = len(rows)
    regenerated = 0
    failed = 0
    skipped_no_data = 0

    for i, row in enumerate(rows, start=1):
        if progress_cb:
            progress_cb(i, total)
        try:
            day = row["trade_date"]
            day = day.date() if isinstance(day, dt.datetime) else day
            sd = get_session_data(row["symbol"], day, structure_interval="5m")
            if sd.fine.empty:
                skipped_no_data += 1
                continue

            result = row_to_trade_result(row)
            run_tag = f"bt{row['backtest_run_id']}" if row["source"] == "backtest" and row.get("backtest_run_id") else "live"
            new_path = charts.render_trade_snapshot(result, sd.fine, run_tag=run_tag)
            if new_path is None:
                failed += 1
                continue

            with get_session() as session:
                trade = session.get(Trade, row["id"])
                if trade is not None:
                    trade.snapshot_path = new_path
            regenerated += 1
        except Exception as exc:  # noqa: BLE001 - one bad row shouldn't kill the whole batch
            log_event("WARNING", "queries.regenerate_snapshots", f"Trade {row.get('id')}: {exc}")
            failed += 1

    return {"total": total, "regenerated": regenerated, "failed": failed, "skipped_no_data": skipped_no_data}


# --- Trader's Journal (live trades only - a journal is about the trader's
# own actually-taken trades, not backtested ones) --------------------------

def get_journal_trades(limit: int = 200) -> list[dict]:
    """Live trades where an entry was actually taken (excludes no-setup
    days - nothing to journal about those), newest first."""
    trades = get_live_trades(limit=100_000)
    taken = [t for t in trades if t.get("entry_price") is not None]
    return taken[:limit]


def update_trade_journal(trade_id: int, notes: str | None, rating: int | None, tags: str | None) -> dict | None:
    with get_session() as session:
        trade = session.get(Trade, trade_id)
        if trade is None:
            return None
        trade.journal_notes = notes or None
        trade.journal_rating = rating
        trade.journal_tags = tags or None
        session.flush()
        row = _row_to_dict(trade)
    return row


def get_journal_analytics() -> dict:
    """Aggregate self-review stats for the Journal page: streaks, best/worst
    trade, win rate by weekday, how much of the log has actually been
    journaled, and tag frequency across whatever tags have been entered."""
    taken = get_journal_trades(limit=100_000)
    resolved = [t for t in taken if t.get("status") in {"TARGET_HIT", "STOP_HIT", "MANUAL_EXIT"} and t.get("pnl_points") is not None]
    resolved_chrono = sorted(resolved, key=lambda t: t["trade_date"])

    weekday_counts = {i: {"wins": 0, "trades": 0} for i in range(7)}
    for t in resolved:
        wd = t["trade_date"].weekday() if hasattr(t["trade_date"], "weekday") else None
        if wd is None:
            continue
        weekday_counts[wd]["trades"] += 1
        if (t["pnl_points"] or 0) > 0:
            weekday_counts[wd]["wins"] += 1
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    win_rate_by_weekday = [
        {
            "day": weekday_names[i],
            "trades": weekday_counts[i]["trades"],
            "win_rate_pct": (100.0 * weekday_counts[i]["wins"] / weekday_counts[i]["trades"]) if weekday_counts[i]["trades"] else None,
        }
        for i in range(5)  # NSE only trades Mon-Fri
    ]

    tag_freq: dict[str, int] = {}
    ratings = []
    journaled_count = 0
    for t in taken:
        if t.get("journal_notes") or t.get("journal_rating") or t.get("journal_tags"):
            journaled_count += 1
        if t.get("journal_rating"):
            ratings.append(t["journal_rating"])
        for tag in (t.get("journal_tags") or "").split(","):
            tag = tag.strip()
            if tag:
                tag_freq[tag] = tag_freq.get(tag, 0) + 1

    cur_streak_kind, cur_streak_len = None, 0
    for t in reversed(resolved_chrono):
        is_win = (t["pnl_points"] or 0) > 0
        kind = "win" if is_win else "loss"
        if cur_streak_kind is None:
            cur_streak_kind, cur_streak_len = kind, 1
        elif kind == cur_streak_kind:
            cur_streak_len += 1
        else:
            break

    best_trade = max(resolved, key=lambda t: t["pnl_points"] or 0.0, default=None)
    worst_trade = min(resolved, key=lambda t: t["pnl_points"] or 0.0, default=None)

    return {
        "journaled_count": journaled_count,
        "total_taken": len(taken),
        "avg_rating": (sum(ratings) / len(ratings)) if ratings else None,
        "tag_freq": dict(sorted(tag_freq.items(), key=lambda kv: -kv[1])),
        "win_rate_by_weekday": win_rate_by_weekday,
        "current_streak_kind": cur_streak_kind,
        "current_streak_len": cur_streak_len,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
    }
