"""The schema and the store against a real TimescaleDB (skipped without one)."""

from datetime import date
from decimal import Decimal

import psycopg
import pytest
from alembic.script import ScriptDirectory

from optlab.domain.market import (
    ConnectionEvent,
    ConnectionEventKind,
    DeliveryPrice,
    Direction,
    Greeks,
    IndexPrice,
    Ticker,
    Trade,
)
from optlab.market_data.rows import option_trade_row
from optlab.market_data.snapshot import LatestState, take_snapshot
from optlab.market_data.store import SCHEMA_REVISION, RecorderStore, sqlalchemy_url
from optlab.market_data.universe import build_universe
from tests.conftest import REPO_ROOT, alembic_config
from tests.market_data.helpers import fixture_instruments

pytestmark = pytest.mark.db

T = 1_794_500_040_000


def test_sqlalchemy_url_uses_the_psycopg_driver() -> None:
    assert sqlalchemy_url("postgresql://u:p@db:5432/x") == "postgresql+psycopg://u:p@db:5432/x"
    assert sqlalchemy_url("postgres://u:p@db/x") == "postgresql+psycopg://u:p@db/x"


def test_code_expects_the_latest_migration() -> None:
    scripts = ScriptDirectory.from_config(alembic_config("postgresql://unused"))
    assert scripts.get_heads() == [SCHEMA_REVISION]


def test_schema_has_hypertables_compressed_after_7_days(database_url: str) -> None:
    with psycopg.connect(database_url) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert {
            "instruments",
            "option_quotes",
            "underlying_ticks",
            "option_trades",
            "delivery_prices",
            "connection_events",
        } <= tables
        hypertables: dict[str, bool] = dict(
            conn.execute(
                "SELECT hypertable_name, compression_enabled "
                "FROM timescaledb_information.hypertables"
            ).fetchall()
        )
        assert hypertables == {
            "option_quotes": True,
            "underlying_ticks": True,
            "vol_expiries": True,  # Phase 3 surface tables (0004)
            "vol_surface": True,
        }
        policies = conn.execute(
            "SELECT hypertable_name, config->>'compress_after' FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_compression' ORDER BY 1"
        ).fetchall()
        assert policies == [
            ("option_quotes", "7 days"),
            ("underlying_ticks", "7 days"),
            ("vol_expiries", "7 days"),
            ("vol_surface", "7 days"),
        ]
        segment = conn.execute(
            "SELECT attname FROM timescaledb_information.compression_settings "
            "WHERE hypertable_name = 'option_quotes' AND segmentby_column_index = 1"
        ).fetchone()
        assert segment == ("instrument_name",)


async def test_schema_revision_is_checked(database_url: str) -> None:
    store = RecorderStore(database_url)
    try:
        assert await store.schema_revision() == SCHEMA_REVISION
    finally:
        await store.close()


def snapshot_fixture() -> tuple[object, int]:
    universe = build_universe(fixture_instruments())
    state = LatestState()
    for name in ("BTC-20NOV26-70000-C", "BTC_USDC-20NOV26-70000-C", "BTC-PERPETUAL"):
        state.update_ticker(
            Ticker(
                instrument_name=name,
                exchange_ts_ms=T - 1_200,
                recv_ts_ms=T - 1_000,
                state="open",
                bid=None if "USDC" in name else 0.027,
                bid_size=0.0 if "USDC" in name else 22.0,
                ask=0.0285,
                ask_size=15.5,
                mark_price=0.0278,
                last_price=None,
                index_price=70_500.25,
                open_interest=321.4,
                estimated_delivery_price=70_500.25,
                mark_iv=None if name == "BTC-PERPETUAL" else 0.4125,
                greeks=None if name == "BTC-PERPETUAL" else Greeks(0.55, 4e-05, 41.2, -98.7, 7.1),
                underlying_index=None if name == "BTC-PERPETUAL" else "BTC-20NOV26",
                current_funding=3.5e-05 if name == "BTC-PERPETUAL" else None,
            )
        )
    state.update_index(IndexPrice("btc_usd", 70_500.25, T - 900, T - 800))
    return take_snapshot(T, universe, state, 300_000), T


async def test_snapshots_are_copied_once(database_url: str) -> None:
    snapshot, _ = snapshot_fixture()
    store = RecorderStore(database_url)
    try:
        assert await store.write_snapshot(snapshot) == (2, 2)  # type: ignore[arg-type]
        assert await store.write_snapshot(snapshot) == (0, 0)  # type: ignore[arg-type]
        count, newest = await store.option_rows_since(T - 60_000)
        assert count == 2
        assert newest is not None
    finally:
        await store.close()
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT instrument_name, currency, bid, bid_size, mark_iv, delta, recv_ts_ms "
            "FROM option_quotes ORDER BY 1"
        ).fetchall()
        assert rows == [
            ("BTC-20NOV26-70000-C", "BTC", 0.027, 22.0, 0.4125, 0.55, T - 1_000),
            ("BTC_USDC-20NOV26-70000-C", "USDC", None, 0.0, 0.4125, 0.55, T - 1_000),
        ]
        under = conn.execute(
            "SELECT instrument_name, kind, currency, index_price, current_funding "
            "FROM underlying_ticks ORDER BY 1"
        ).fetchall()
        assert under == [
            ("BTC-PERPETUAL", "perpetual", "USD", 70_500.25, 3.5e-05),
            ("btc_usd", "index", "USD", 70_500.25, None),
        ]


async def test_trades_are_stored_once_per_trade_id(database_url: str) -> None:
    universe = build_universe(fixture_instruments())
    instrument = universe.instruments["BTC_USDC-20NOV26-70000-C"]
    trade = Trade("USDC-1", instrument.name, T, T + 3, 2000.0, 0.05, Direction.BUY, 3, 0.43)
    store = RecorderStore(database_url)
    try:
        assert await store.write_trades([option_trade_row(instrument, trade)]) == 1
        again = option_trade_row(instrument, trade)
        other = option_trade_row(
            instrument, Trade("USDC-2", instrument.name, T, T, 1.0, 1.0, Direction.SELL)
        )
        assert await store.write_trades([again, other]) == 1
    finally:
        await store.close()
    with psycopg.connect(database_url) as conn:
        assert conn.execute(
            "SELECT trade_id, currency, iv FROM option_trades ORDER BY 1"
        ).fetchall() == [
            ("USDC-1", "USDC", 0.43),
            ("USDC-2", "USDC", None),
        ]


async def test_instruments_are_upserted_and_delisted(database_url: str) -> None:
    instruments = list(build_universe(fixture_instruments()).instruments.values())
    store = RecorderStore(database_url)
    try:
        await store.upsert_instruments(instruments, seen_ms=T)
        await store.upsert_instruments(instruments, seen_ms=T + 600_000)
        await store.deactivate_instruments(["BTC-ODDNAME"], delisted_ms=T + 700_000)
    finally:
        await store.close()
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM instruments").fetchone() == (9,)
        odd = conn.execute(
            "SELECT kind, book, strike, option_type, expiry_ms, first_seen_ms, last_seen_ms, "
            "is_active, delisted_ms FROM instruments WHERE instrument_name = 'BTC-ODDNAME'"
        ).fetchone()
        assert odd == (
            "option",
            "inverse",
            85000.0,
            "put",
            1798185600000,
            T,
            T + 600_000,
            False,
            T + 700_000,
        )
        perp = conn.execute(
            "SELECT kind, expiry_ms, quote_currency FROM instruments "
            "WHERE instrument_name = 'BTC-PERPETUAL'"
        ).fetchone()
        assert perp == ("perpetual", None, "USD")


async def test_delivery_prices_keep_exact_decimals(database_url: str) -> None:
    prices = [
        DeliveryPrice("btc_usdc", date(2026, 11, 12), Decimal("70123.45"), "USDC"),
        DeliveryPrice("btc_usd", date(2026, 11, 12), Decimal("70123.44"), "USD"),
    ]
    store = RecorderStore(database_url)
    try:
        assert await store.write_delivery_prices(prices, T) == 2
        assert await store.write_delivery_prices(prices, T + 1) == 0
    finally:
        await store.close()
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT index_name, delivery_price, currency FROM delivery_prices ORDER BY 1"
        ).fetchall()
        assert rows == [
            ("btc_usd", Decimal("70123.44"), "USD"),
            ("btc_usdc", Decimal("70123.45"), "USDC"),
        ]


async def test_connection_events_are_recorded(database_url: str) -> None:
    store = RecorderStore(database_url)
    try:
        await store.write_connection_events(
            [
                ConnectionEvent(T, "deribit-ws-0", ConnectionEventKind.DISCONNECTED, "code 4000"),
                ConnectionEvent(T + 1, "deribit-ws-0", ConnectionEventKind.CONNECTED),
            ]
        )
    finally:
        await store.close()
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT ts_ms, source, event, detail FROM connection_events ORDER BY id"
        ).fetchall()
        assert rows == [
            (T, "deribit-ws-0", "disconnected", "code 4000"),
            (T + 1, "deribit-ws-0", "connected", ""),
        ]


GAP_SQL = (REPO_ROOT / "infra" / "sql" / "gap_check.sql").read_text(encoding="utf-8")


def fill_minutes(conn: psycopg.Connection[tuple[object, ...]], hole_minutes: int) -> None:
    """One row a minute for the last 25 hours, with a hole of ``hole_minutes`` 12 hours ago."""
    conn.execute(
        """
        INSERT INTO option_quotes (ts, instrument_name, currency, bid_size, ask_size, state,
                                   exchange_ts_ms, recv_ts_ms)
        SELECT g, 'BTC-X', 'BTC', 0, 0, 'open', 0, 0
        FROM generate_series(date_trunc('minute', now()) - interval '25 hours',
                             date_trunc('minute', now()), interval '1 minute') AS g
        WHERE g < date_trunc('minute', now()) - interval '12 hours'
           OR g >= date_trunc('minute', now()) - interval '12 hours'
                   + make_interval(mins => %s)
        """,
        (hole_minutes,),
    )


@pytest.mark.parametrize(("hole", "gate", "gaps"), [(0, "PASS", 0), (4, "PASS", 0), (5, "FAIL", 1)])
def test_gap_check_query(database_url: str, hole: int, gate: str, gaps: int) -> None:
    with psycopg.connect(database_url) as conn:
        fill_minutes(conn, hole)
        cur = conn.execute(GAP_SQL)
        names = [d.name for d in cur.description or []]
        result = dict(zip(names, cur.fetchone() or (), strict=True))
    assert result["gate"] == gate
    assert result["gaps_over_5_minutes"] == gaps
    assert result["minutes_present"] == 24 * 60 + 1 - hole
    if hole:
        assert result["breaks"] != "none"
    else:
        assert result["breaks"] == "none"
