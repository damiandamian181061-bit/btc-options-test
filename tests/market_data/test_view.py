"""MarketDataView: point in time over Parquet days and the database, with the leakage test.

Leakage test: two copies of the same market, the second with every row after t changed,
deleted or added to. Everything the view returns at t must be identical for both, over both
sources. The same views over Parquet and over the database must also agree exactly.
"""

from datetime import date
from pathlib import Path

import psycopg
import pyarrow as pa
import pytest

from optlab.domain.clock import MS_PER_MINUTE, ms_to_datetime
from optlab.market_data.export import EXPORT_DIR, export_day
from optlab.market_data.quality import load_quality_config, run_checks
from optlab.market_data.view import (
    DatabaseSource,
    MarketDataSource,
    MarketDataView,
    ParquetSource,
    normalize,
    snapshot_times,
    view_tables,
)
from tests.conftest import REPO_ROOT
from tests.market_data.synthetic import INSTRUMENT_COLUMNS, OPTIONS, copy_rows, populate, utc_ms

M = MS_PER_MINUTE
START = utc_ms(2026, 11, 13, 22)
END = utc_ms(2026, 11, 14, 1)
NOW = utc_ms(2026, 11, 14, 1, 31)
T = START + 90 * M + 30_000  # 23:30:30, between two snapshots
TIMES = [START, START + 11 * M, T, START + 119 * M, START + 120 * M, END - M]
TIME_COLUMNS = {
    "option_chain": "ts_ms",
    "underlyings": "ts_ms",
    "option_history": "ts_ms",
    "underlying_history": "ts_ms",
    "trades": "recv_ts_ms",
    "instruments": "first_seen_ms",
    "delivery_prices": "recorded_ms",
    "quality": "start_ts_ms",
}


def build(url: str) -> None:
    """The shared market: a gap at 22:10-22:11 and a skip period open across T."""
    populate(url, START, END)
    t = ms_to_datetime
    with psycopg.connect(url) as conn:
        conn.execute(
            "DELETE FROM option_quotes WHERE ts IN (%s, %s)", (t(START + 10 * M), t(START + 11 * M))
        )
        run_checks(conn, load_quality_config(REPO_ROOT / "configs"), NOW)
        conn.execute(
            "INSERT INTO data_quality (hour_ts, check_name, scope, subject, start_ts, end_ts, "
            "minutes, skip, detail, detected_ms) VALUES (%s, 'stale_index', 'index', 'btc_usd', "
            "%s, %s, 15, true, 'missing (first of 15 minutes)', %s)",
            (t(START + 60 * M), t(START + 85 * M), t(START + 100 * M), NOW),
        )


def change_everything_after(url: str, t_ms: int) -> None:
    """Change, remove and add rows that became known after t_ms (the view must not notice)."""
    t = ms_to_datetime(t_ms)
    later = ms_to_datetime(t_ms + 5 * M)
    with psycopg.connect(url) as conn:
        conn.execute("UPDATE option_quotes SET bid = bid * 3, mark_iv = 0.99 WHERE ts > %s", (t,))
        conn.execute(
            "DELETE FROM option_quotes WHERE ts > %s AND instrument_name LIKE '%%-C'", (t,)
        )
        conn.execute("UPDATE underlying_ticks SET index_price = 1 WHERE ts > %s", (t,))
        conn.execute("DELETE FROM underlying_ticks WHERE ts = %s", (later,))
        conn.execute("UPDATE option_trades SET price = 123 WHERE recv_ts_ms > %s", (t_ms,))
        # Traded before t but received after it: not known at t.
        conn.execute(
            "INSERT INTO option_trades (trade_id, ts, instrument_name, currency, price, amount, "
            "direction, exchange_ts_ms, recv_ts_ms) VALUES ('LATE', %s, %s, 'BTC', 0.05, 1, "
            "'buy', %s, %s)",
            (ms_to_datetime(t_ms - 1000), OPTIONS[0].name, t_ms - 1000, t_ms + 2000),
        )
        # A listing after t, and later changes to rows listed before it.
        row = list(conn.execute("SELECT * FROM instruments LIMIT 1").fetchone() or ())
        new = dict(zip(INSTRUMENT_COLUMNS, row[: len(INSTRUMENT_COLUMNS)], strict=True))
        new.update(instrument_name="BTC-27NOV26-200000-C", first_seen_ms=t_ms + 1, strike=2e5)
        copy_rows(conn, "instruments", INSTRUMENT_COLUMNS, [tuple(new.values())])
        conn.execute(
            "UPDATE instruments SET last_seen_ms = %s, is_active = false, delisted_ms = %s",
            (t_ms + 10 * M, t_ms + 10 * M),
        )
        conn.execute(
            "INSERT INTO option_quotes (ts, instrument_name, currency, bid_size, ask_size, state, "
            "exchange_ts_ms, recv_ts_ms) VALUES (%s, 'BTC-27NOV26-200000-C', 'BTC', 0, 0, 'open', "
            "%s, %s)",
            (later, t_ms + 5 * M, t_ms + 5 * M),
        )
        conn.execute(
            "INSERT INTO delivery_prices VALUES ('btc_usd', '2026-11-15', 1, 'USD', %s)",
            (t_ms + 1,),
        )
        conn.execute("DELETE FROM delivery_prices WHERE recorded_ms > %s", (t_ms + 2,))
        # A finding open at t gets a different end and detail; later findings change or go.
        conn.execute(
            "UPDATE data_quality SET end_ts = end_ts + interval '40 minutes', detail = 'other' "
            "WHERE start_ts <= %s AND end_ts > %s",
            (t, t),
        )
        conn.execute("UPDATE data_quality SET skip = NOT skip WHERE start_ts > %s", (t,))


def export(url: str, root: Path) -> ParquetSource:
    for day in (date(2026, 11, 13), date(2026, 11, 14)):
        export_day(url, day, root / EXPORT_DIR, now_ms=NOW)
    return ParquetSource.from_data_dir(root)


def assert_same(a: dict[str, pa.Table], b: dict[str, pa.Table], label: str) -> None:
    assert a.keys() == b.keys()
    for name in a:
        assert a[name].schema.equals(b[name].schema), f"{label} {name}: schema"
        assert a[name].equals(b[name]), f"{label} {name}: {a[name].num_rows} vs {b[name].num_rows}"


def test_timestamps_become_utc_milliseconds() -> None:
    table = pa.table(
        {"ts": pa.array([ms_to_datetime(1_794_607_200_123)], pa.timestamp("us", tz="UTC"))}
    )
    out = normalize(table)
    assert out.schema.names == ["ts_ms"]
    assert out.column("ts_ms").to_pylist() == [1_794_607_200_123]


@pytest.mark.db
def test_parquet_and_database_views_agree_and_never_go_past_t(
    database_url: str, tmp_path: Path
) -> None:
    build(database_url)
    parquet = export(database_url, tmp_path)
    db = DatabaseSource(database_url)
    try:
        for t in TIMES:
            since = START - 10 * M
            from_db = view_tables(MarketDataView(db, t), since)
            from_files = view_tables(MarketDataView(parquet, t), since)
            assert_same(from_db, from_files, f"t={t}")
            for name, table in from_db.items():
                times = table.column(TIME_COLUMNS[name]).to_pylist()
                assert all(x <= t for x in times), f"{name} returned a row after t"
        chain = MarketDataView(db, T).option_chain()
        assert set(chain.column("ts_ms").to_pylist()) == {START + 90 * M}
        assert chain.num_rows == len(OPTIONS)
        # The finding open at T is cut off at T, its length and detail hidden.
        quality = MarketDataView(db, T).quality(START)
        open_rows = [r for r in quality.to_pylist() if r["detail"] == "still open"]
        assert [(r["end_ts_ms"], r["minutes"]) for r in open_rows] == [(START + 91 * M, 6)]
    finally:
        db.close()


@pytest.mark.db
@pytest.mark.parametrize("t", [START + 11 * M, T, START + 120 * M])
def test_changing_every_row_after_t_changes_nothing_returned_at_t(
    database_url: str, second_database_url: str, tmp_path: Path, t: int
) -> None:
    build(database_url)
    build(second_database_url)
    change_everything_after(second_database_url, t)
    sources: dict[str, tuple[MarketDataSource, MarketDataSource]] = {
        "database": (DatabaseSource(database_url), DatabaseSource(second_database_url)),
        "parquet": (
            export(database_url, tmp_path / "a"),
            export(second_database_url, tmp_path / "b"),
        ),
    }
    try:
        for label, (a, b) in sources.items():
            since = START - 10 * M
            assert_same(
                view_tables(MarketDataView(a, t), since),
                view_tables(MarketDataView(b, t), since),
                label,
            )
            # The changes are real: just after t the two differ.
            later = t + 10 * M
            assert (
                not MarketDataView(a, later)
                .option_chain()
                .equals(MarketDataView(b, later).option_chain())
            )
    finally:
        for source in sources["database"]:
            assert isinstance(source, DatabaseSource)
            source.close()


@pytest.mark.db
def test_snapshot_times_leave_out_gaps_and_skip_periods(database_url: str, tmp_path: Path) -> None:
    build(database_url)
    parquet = export(database_url, tmp_path)
    db = DatabaseSource(database_url)
    try:
        for source in (db, parquet):
            every = list(snapshot_times(source, START, END - M, skip_flagged=False))
            usable = list(snapshot_times(source, START, END - M))
            assert len(every) == 180 - 2  # 22:10 and 22:11 were never recorded
            assert START + 10 * M not in every
            assert START + 85 * M in every
            skipped = {START + m * M for m in range(85, 100)}
            assert set(every) - set(usable) == skipped
            assert MarketDataView(source, START + 11 * M + 30_000).snapshot_ms() == START + 9 * M
            assert MarketDataView(source, START - M).option_chain().num_rows == 0
    finally:
        db.close()
