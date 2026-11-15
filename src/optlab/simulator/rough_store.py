"""ROUGH (Phase 2B): the paper service's tables (migration 0003), written and read here.

Writes go through one read-write connection in autocommit mode, each change in its own
transaction, so a restart never leaves half a spread: an entry (spread, both fills and the
day's decision) or a close (both fills and the spread's new status) is stored whole or not at
all. Cash is never stored as a running balance: it is the starting capital plus the sum of
every fill's cash change, read back in exact decimals.

The page's reads use the status page's read-only connection.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from optlab.domain.clock import ms_to_datetime
from optlab.ledger.rough import Fill, SpreadLegs, cash_of


@dataclass(frozen=True)
class OpenSpread:
    id: int
    legs: SpreadLegs
    entry_credit: Decimal  # per BTC, before fees
    max_loss: Decimal  # USDC
    opened_ms: int


@dataclass(frozen=True)
class LedgerState:
    cash: Decimal
    open: list[OpenSpread]


@dataclass(frozen=True)
class DecisionRow:
    day: date
    ts_ms: int
    decided_ms: int
    iv30: float | None
    rv30: float | None
    index_price: float | None
    trend_average: float | None
    side: str | None
    short_legs: int
    passed: int
    action: str  # "entered" or "no trade"
    reason: str


def connect(database_url: str) -> psycopg.Connection[Any]:
    return psycopg.connect(database_url, autocommit=True, application_name="optlab-paper-rough")


def load_state(conn: psycopg.Connection[Any], starting_cash: Decimal) -> LedgerState:
    row = conn.execute("SELECT coalesce(sum(cash), 0)::text FROM rough_fills").fetchone()
    cash = starting_cash + Decimal(row[0] if row else "0")
    rows = conn.execute(
        "SELECT id, side, expiry_ms, short_instrument, long_instrument, short_strike::text, "
        "long_strike::text, amount::text, entry_credit::text, max_loss::text, opened_ms "
        "FROM rough_spreads WHERE status = 'open' ORDER BY id"
    ).fetchall()
    spreads = [
        OpenSpread(
            id=int(r[0]),
            legs=SpreadLegs(
                r[1], int(r[2]), r[3], r[4], Decimal(r[5]), Decimal(r[6]), Decimal(r[7])
            ),
            entry_credit=Decimal(r[8]),
            max_loss=Decimal(r[9]),
            opened_ms=int(r[10]),
        )
        for r in rows
    ]
    return LedgerState(cash, spreads)


def has_decision(conn: psycopg.Connection[Any], day: date) -> bool:
    return (
        conn.execute("SELECT 1 FROM rough_decisions WHERE day = %s", (day,)).fetchone() is not None
    )


_DECISION = (
    "INSERT INTO rough_decisions (day, ts_ms, decided_ms, iv30, rv30, index_price, trend_average, "
    "side, short_legs, passed, action, reason, spread_id) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (day) DO NOTHING "
    "RETURNING day"
)


def _decision_params(d: DecisionRow, spread_id: int | None) -> tuple[Any, ...]:
    return (
        d.day, d.ts_ms, d.decided_ms, d.iv30, d.rv30, d.index_price, d.trend_average, d.side,
        d.short_legs, d.passed, d.action, d.reason[:2000], spread_id,
    )  # fmt: skip


class AlreadyDecidedError(RuntimeError):
    """The day already has a decision (the transaction is rolled back)."""


def save_no_trade(conn: psycopg.Connection[Any], d: DecisionRow) -> bool:
    return conn.execute(_DECISION, _decision_params(d, None)).fetchone() is not None


def _insert_fills(
    conn: psycopg.Connection[Any], spread_id: int, fills: tuple[Fill, ...], ts_ms: int, now_ms: int
) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO rough_fills (spread_id, ts_ms, recorded_ms, instrument_name, kind, "
            "direction, amount, price, fee, cash, currency) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    spread_id,
                    ts_ms,
                    now_ms,
                    f.instrument_name,
                    f.kind,
                    f.direction,
                    f.amount,
                    f.price,
                    f.fee,
                    f.cash,
                    f.currency,
                )
                for f in fills
            ],
        )


def save_entry(
    conn: psycopg.Connection[Any],
    d: DecisionRow,
    legs: SpreadLegs,
    *,
    short_delta: float,
    entry_credit: Decimal,
    credit_after_fees: Decimal,
    max_loss: Decimal,
    thesis: str,
    fills: tuple[Fill, ...],
) -> int:
    """Store the spread, its entry fills and the day's decision together; return the id."""
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO rough_spreads (side, expiry_ms, short_instrument, long_instrument, "
            "short_strike, long_strike, short_delta, amount, currency, entry_credit, "
            "credit_after_fees, max_loss, opened_ms, thesis, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open') RETURNING id",
            (
                legs.side,
                legs.expiry_ms,
                legs.short_name,
                legs.long_name,
                legs.short_strike,
                legs.long_strike,
                short_delta,
                legs.amount,
                fills[0].currency,
                entry_credit,
                credit_after_fees,
                max_loss,
                d.ts_ms,
                thesis,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("INSERT ... RETURNING id returned no row")
        spread_id = int(row[0])
        _insert_fills(conn, spread_id, fills, d.ts_ms, d.decided_ms)
        if conn.execute(_DECISION, _decision_params(d, spread_id)).fetchone() is None:
            raise AlreadyDecidedError(f"{d.day} already has a decision")
    return spread_id


def save_close(
    conn: psycopg.Connection[Any],
    spread: OpenSpread,
    *,
    status: str,  # "closed" or "settled"
    reason: str,
    exit_price: Decimal,  # per BTC: the buy-back price, or the payoff owed at delivery
    delivery_price: Decimal | None,
    thesis_met: bool,
    fills: tuple[Fill, ...],
    ts_ms: int,
    now_ms: int,
) -> Decimal | None:
    """Close an open spread with its fills; return its P&L, or None if it was not open."""
    with conn.transaction():
        row = conn.execute(
            "SELECT coalesce(sum(cash), 0)::text FROM rough_fills WHERE spread_id = %s",
            (spread.id,),
        ).fetchone()
        pnl = Decimal(row[0] if row else "0") + cash_of(fills)
        updated = conn.execute(
            "UPDATE rough_spreads SET status = %s, exit_reason = %s, closed_ms = %s, "
            "exit_price = %s, delivery_price = %s, pnl = %s, thesis_met = %s "
            "WHERE id = %s AND status = 'open' RETURNING id",
            (status, reason, ts_ms, exit_price, delivery_price, pnl, thesis_met, spread.id),
        ).fetchone()
        if updated is None:
            return None
        _insert_fills(conn, spread.id, fills, ts_ms, now_ms)
    return pnl


def save_equity(
    conn: psycopg.Connection[Any],
    ts_ms: int,
    cash: Decimal,
    open_value: Decimal,
    open_spreads: int,
    open_max_loss: Decimal,
) -> None:
    conn.execute(
        "INSERT INTO rough_equity (ts, cash, open_value, equity, open_spreads, open_max_loss) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (ts) DO NOTHING",
        (ms_to_datetime(ts_ms), cash, open_value, cash + open_value, open_spreads, open_max_loss),
    )


def heartbeat(
    conn: psycopg.Connection[Any],
    run_id: str,
    started_ms: int,
    now_ms: int,
    *,
    snapshot_ms: int | None = None,
    skipped: bool = False,
    note: str = "",
) -> None:
    """Record that this run is alive; count each processed (or skipped) snapshot minute."""
    processed = snapshot_ms is not None
    conn.execute(
        "INSERT INTO rough_runs (run_id, started_ms, last_ms, last_snapshot_ms, minutes, skipped, "
        "note) VALUES (%(r)s, %(s)s, %(n)s, %(t)s, %(m)s, %(k)s, %(note)s) "
        "ON CONFLICT (run_id) DO UPDATE SET last_ms = %(n)s, "
        "last_snapshot_ms = coalesce(%(t)s, rough_runs.last_snapshot_ms), "
        "minutes = rough_runs.minutes + %(m)s, skipped = rough_runs.skipped + %(k)s, "
        "note = %(note)s",
        {
            "r": run_id,
            "s": started_ms,
            "n": now_ms,
            "t": snapshot_ms,
            "m": int(processed and not skipped),
            "k": int(processed and skipped),
            "note": note[:500],
        },
    )


# ------------------------------------------------------------------------------------ page reads


@dataclass(frozen=True)
class PaperPage:
    runs: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    spreads: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    equity: list[tuple[int, Decimal, Decimal]]  # (snapshot minute, equity, cash)


def _dicts(conn: psycopg.Connection[Any], query: str, params: Any = None) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return list(cur.execute(query, params))


def page_data(conn: psycopg.Connection[Any]) -> PaperPage:
    """Everything the page shows, newest first (the equity series oldest first)."""
    return PaperPage(
        runs=_dicts(
            conn,
            "SELECT run_id, started_ms, last_ms, last_snapshot_ms, minutes, skipped, note "
            "FROM rough_runs ORDER BY started_ms DESC LIMIT 20",
        ),
        decisions=_dicts(
            conn,
            "SELECT day, ts_ms, action, reason, iv30, rv30, index_price, trend_average, side, "
            "short_legs, passed, spread_id FROM rough_decisions ORDER BY day DESC",
        ),
        spreads=_dicts(
            conn,
            "SELECT id, side, expiry_ms, short_instrument, long_instrument, short_delta, amount, "
            "entry_credit, credit_after_fees, max_loss, opened_ms, status, exit_reason, closed_ms, "
            "exit_price, delivery_price, pnl, thesis, thesis_met "
            "FROM rough_spreads ORDER BY id DESC",
        ),
        fills=_dicts(
            conn,
            "SELECT id, spread_id, ts_ms, instrument_name, kind, direction, amount, price, fee, "
            "cash, currency FROM rough_fills ORDER BY id DESC LIMIT 500",
        ),
        equity=[
            (int(t), Decimal(e), Decimal(c))
            for t, e, c in conn.execute(
                "SELECT utc_ms(ts), equity::text, cash::text FROM rough_equity ORDER BY ts"
            ).fetchall()
        ],
    )
