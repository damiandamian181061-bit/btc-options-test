"""The surface worker against a database: storage, daily closes, and the leakage test."""

from datetime import date
from typing import Any

import psycopg
import pytest
from psycopg import sql

from optlab.domain.clock import MS_PER_MINUTE
from optlab.domain.market import Book
from optlab.market_data.rows import OptionQuoteRow, UnderlyingTickRow
from optlab.market_data.view import DatabaseSource
from optlab.volatility import store
from optlab.volatility.features import compute_features
from optlab.volatility.realised import CloseSource
from optlab.volatility.surface import Bucket, build_surface
from optlab.volatility.worker import (
    CloseCache,
    Minute,
    SurfaceWorker,
    close_day,
    compute_minute,
    load_closes,
    summary,
)
from tests.market_data.synthetic import (
    OPTIONS,
    copy_rows,
    populate,
    quote_row,
    spot,
    underlying_rows,
    utc_ms,
)
from tests.market_data.test_view import START, T, build, change_everything_after
from tests.volatility.helpers import T0, make_chain, make_futures

M = MS_PER_MINUTE
BOOKS = (Book.LINEAR, Book.INVERSE)


def test_close_day_turns_at_0800_utc() -> None:
    assert close_day(utc_ms(2026, 11, 14, 7, 59)) == date(2026, 11, 13)
    assert close_day(utc_ms(2026, 11, 14, 8, 0)) == date(2026, 11, 14)


def _rows(minute: Minute) -> tuple[object, ...]:
    """Everything the worker would store for a minute, as plain values."""
    surfaces = [r for s in minute.surfaces.values() for r in store.expiry_rows(s)]
    points = [r for s in minute.surfaces.values() for r in store.surface_rows(s)]
    feats = [store.feature_row(f, 0) for f in minute.features.values()]
    return (minute.ts_ms, minute.option_rows, surfaces, points, feats)


def _count(conn: psycopg.Connection[Any], table: str) -> int:
    row = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.db
def test_save_a_minute_once(database_url: str) -> None:
    chain = make_chain(days=(9.0, 37.0))
    surface = build_surface(chain, make_futures(chain), Book.LINEAR, T0, index_price=65_000.0)
    feats = compute_features(surface, [], 65_000.0, None)
    with store.connect(database_url) as conn:
        assert store.last_minute(conn) is None
        n = store.save_minute(conn, [surface], [feats], computed_ms=T0 + 5_000)
        assert n == (2, 35, 1)  # 2 expiries; 7 tenors x 5 buckets; 1 feature row
        store.save_minute(conn, [surface], [feats], computed_ms=T0 + 9_000)  # again: no change
        assert [_count(conn, t) for t in ("vol_expiries", "vol_surface", "features")] == [2, 35, 1]
        assert store.last_minute(conn) == T0
        row = conn.execute(
            "SELECT iv, extrapolated FROM vol_surface WHERE tenor_days = 30 AND bucket = 'atm'"
        ).fetchone()
        assert row is not None
        assert row[0] == pytest.approx(surface.iv(30, Bucket.ATM))
        assert row[1] is False


@pytest.mark.db
def test_closes_from_delivery_prices_and_our_own_0800_snapshot(database_url: str) -> None:
    populate(database_url, START, START + 30 * M)  # 13 Nov 22:00-22:30: no 08:00 snapshot yet
    a = utc_ms(2026, 11, 14, 7, 58)
    populate_more(database_url, a, a + 5 * M)  # 07:58-08:02 on 14 Nov
    source = DatabaseSource(database_url)
    try:
        before = load_closes(source, T, BOOKS)
        # 13 Nov's delivery price (fetched 13 Nov 08:01) is known at T; nothing else.
        assert [(c.day, c.price, c.source) for c in before[Book.LINEAR]] == [
            (date(2026, 11, 13), 90120.10, CloseSource.DELIVERY)
        ]
        after = load_closes(source, utc_ms(2026, 11, 14, 8, 2), BOOKS)
        last = after[Book.INVERSE][-1]
        assert last.day == date(2026, 11, 14)
        assert last.source == CloseSource.INDEX
        assert last.price == spot(utc_ms(2026, 11, 14, 8))
    finally:
        source.close()


def populate_more(url: str, start: int, end: int) -> None:
    """More minutes in an already populated database (no second copy of the reference rows)."""
    with psycopg.connect(url) as conn:
        minutes = range(start, end, M)
        copy_rows(
            conn,
            "option_quotes",
            OptionQuoteRow._fields,
            (quote_row(m, o) for m in minutes for o in OPTIONS if m <= o.expiry_ms),
        )
        copy_rows(
            conn,
            "underlying_ticks",
            UnderlyingTickRow._fields,
            (r for m in minutes for r in underlying_rows(m)),
        )


@pytest.mark.db
@pytest.mark.parametrize("t", [START + 11 * M, T, START + 120 * M])
def test_surface_and_features_never_use_data_after_t(
    database_url: str, second_database_url: str, t: int
) -> None:
    """Leakage test: change, delete and add every kind of row after t in a second copy of the
    market; the minute computed at t is identical."""
    build(database_url)
    build(second_database_url)
    change_everything_after(second_database_url, t)
    a, b = DatabaseSource(database_url), DatabaseSource(second_database_url)
    try:
        ma = compute_minute(a, t, CloseCache(BOOKS))
        mb = compute_minute(b, t, CloseCache(BOOKS))
        assert ma is not None
        assert mb is not None
        assert ma.ts_ms <= t
        assert any(f.smile is not None for f in ma.surfaces[Book.LINEAR].fits)  # a real fit
        assert _rows(ma) == _rows(mb)
        # The changes are real: just after t the two differ.
        later_a = compute_minute(a, t + 10 * M, CloseCache(BOOKS))
        later_b = compute_minute(b, t + 10 * M, CloseCache(BOOKS))
        assert later_a is not None
        assert later_b is not None
        assert _rows(later_a) != _rows(later_b)
    finally:
        a.close()
        b.close()


@pytest.mark.db
def test_worker_processes_each_new_minute_once(database_url: str) -> None:
    populate(database_url, START, START + 20 * M)
    source = DatabaseSource(database_url)
    conn = store.connect(database_url)
    try:
        worker = SurfaceWorker(source=source, conn=conn, closes=CloseCache(BOOKS))
        first = worker.step(START + 5 * M + 30_000)
        assert first is not None
        assert first.ts_ms == START + 5 * M
        assert worker.step(START + 5 * M + 40_000) is None  # same minute: nothing new
        second = worker.step(START + 7 * M + 1_000)
        assert second is not None
        assert second.ts_ms == START + 7 * M
        n = conn.execute("SELECT count(DISTINCT ts) FROM features").fetchone()
        assert n is not None
        assert n[0] == 2
        assert store.last_minute(conn) == START + 7 * M
        assert summary(second)[0].startswith("surface at 2026-11-13 22:07 UTC")
    finally:
        conn.close()
        source.close()
