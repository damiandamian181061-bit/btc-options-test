"""PostgreSQL/TimescaleDB writes for the recorder.

Bulk rows (snapshots and trades) go in with COPY into a temporary staging table, then one
``INSERT ... ON CONFLICT DO NOTHING`` moves them into the real table. That keeps COPY's speed
and makes every write idempotent: a snapshot minute written twice (for example by two recorder
runs overlapping during a deploy) or a trade seen twice is stored once.

The schema itself is created by the Alembic migrations in ``infra/migrations``; the recorder
refuses to start unless its own tables are exactly as this code expects
(``optlab.market_data.schema``), whatever the revision.
"""

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql

from optlab.domain.clock import ms_to_datetime
from optlab.domain.market import ConnectionEvent, DeliveryPrice, Instrument
from optlab.market_data.rows import (
    OptionQuoteRow,
    OptionTradeRow,
    RecorderStatusRow,
    UnderlyingTickRow,
)
from optlab.market_data.snapshot import Snapshot

logger = logging.getLogger(__name__)

# The newest migration this code ships. Services don't require it: each checks only that the
# tables it uses are as expected (optlab.market_data.schema), so a migration that only adds
# tables needs no restart of the recorder, the backup job or the paper service.
SCHEMA_REVISION = "0005"

_INSTRUMENT_COLUMNS = (
    "instrument_name",
    "exchange_instrument_id",
    "kind",
    "book",
    "base_currency",
    "quote_currency",
    "settlement_currency",
    "counter_currency",
    "price_index",
    "option_type",
    "strike",
    "expiry_ms",
    "contract_size",
    "tick_size",
    "min_trade_amount",
    "settlement_period",
    "creation_ms",
    "first_seen_ms",
    "last_seen_ms",
)


def sqlalchemy_url(database_url: str) -> str:
    """The same URL with SQLAlchemy's psycopg 3 driver name (used by Alembic)."""
    for prefix in ("postgresql://", "postgres://"):
        if database_url.startswith(prefix):
            return "postgresql+psycopg://" + database_url[len(prefix) :]
    return database_url


class RecorderStore:
    """One database connection, used by one writer task at a time."""

    def __init__(self, dsn: str, *, application_name: str = "optlab-recorder") -> None:
        self._dsn = dsn
        self._application_name = application_name
        self._conn: psycopg.AsyncConnection[Any] | None = None

    async def connect(self) -> None:
        await self._connection()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def reset(self) -> None:
        """Drop the connection after an error; the next call reconnects."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except psycopg.Error:
                logger.debug("closing a broken connection failed", exc_info=True)

    async def _connection(self) -> psycopg.AsyncConnection[Any]:
        if self._conn is None or self._conn.closed:
            self._conn = await psycopg.AsyncConnection.connect(
                self._dsn, application_name=self._application_name, connect_timeout=10
            )
        return self._conn

    # ----------------------------------------------------------------- reads

    async def schema_revision(self) -> str | None:
        conn = await self._connection()
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute("SELECT to_regclass('alembic_version') IS NOT NULL")
            exists = await cur.fetchone()
            if not exists or not exists[0]:
                return None
            await cur.execute("SELECT version_num FROM alembic_version")
            row = await cur.fetchone()
            return None if row is None else str(row[0])

    async def option_rows_since(self, since_ms: int) -> tuple[int, datetime | None]:
        """Number of option_quotes rows with ``ts`` at or after ``since_ms``, and the newest ts."""
        conn = await self._connection()
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*), max(ts) FROM option_quotes WHERE ts >= %s",
                (ms_to_datetime(since_ms),),
            )
            row = await cur.fetchone()
        if row is None:
            return 0, None
        return int(row[0]), row[1]

    # ----------------------------------------------------------------- bulk writes

    async def write_snapshot(self, snapshot: Snapshot) -> tuple[int, int]:
        """Rows inserted into (option_quotes, underlying_ticks)."""
        options = await self._copy_insert("option_quotes", OptionQuoteRow._fields, snapshot.options)
        underlyings = await self._copy_insert(
            "underlying_ticks", UnderlyingTickRow._fields, snapshot.underlyings
        )
        return options, underlyings

    async def write_trades(self, rows: Sequence[OptionTradeRow]) -> int:
        """Trades inserted; trades already stored (same trade_id) are skipped."""
        return await self._copy_insert("option_trades", OptionTradeRow._fields, rows)

    async def _copy_insert(
        self, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]
    ) -> int:
        if not rows:
            return 0
        conn = await self._connection()
        stage = sql.Identifier(f"stage_{table}")
        target = sql.Identifier(table)
        cols = sql.SQL(", ").join(map(sql.Identifier, columns))
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                sql.SQL(
                    "CREATE TEMP TABLE IF NOT EXISTS {} (LIKE {} INCLUDING DEFAULTS) "
                    "ON COMMIT DELETE ROWS"
                ).format(stage, target)
            )
            async with cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(stage, cols)) as copy:
                for row in rows:
                    await copy.write_row(row)
            await cur.execute(
                sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {} ON CONFLICT DO NOTHING").format(
                    target, cols, cols, stage
                )
            )
            return max(cur.rowcount, 0)

    # ----------------------------------------------------------------- small writes

    async def upsert_instruments(self, instruments: Iterable[Instrument], seen_ms: int) -> None:
        rows = [
            (
                i.name,
                i.exchange_id,
                i.kind.value,
                i.book.value,
                i.base_currency,
                i.quote_currency,
                i.settlement_currency,
                i.counter_currency,
                i.price_index,
                None if i.option_type is None else i.option_type.value,
                i.strike,
                i.expiry_ms,
                i.contract_size,
                i.tick_size,
                i.min_trade_amount,
                i.settlement_period,
                i.creation_ms,
                seen_ms,
                seen_ms,
            )
            for i in instruments
        ]
        if not rows:
            return
        cols = sql.SQL(", ").join(map(sql.Identifier, _INSTRUMENT_COLUMNS))
        placeholders = sql.SQL(", ").join(sql.Placeholder() * len(_INSTRUMENT_COLUMNS))
        updates = sql.SQL(", ").join(
            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
            for c in _INSTRUMENT_COLUMNS
            if c not in ("instrument_name", "first_seen_ms")
        )
        query = sql.SQL(
            "INSERT INTO instruments ({}) VALUES ({}) ON CONFLICT (instrument_name) "
            "DO UPDATE SET {}, is_active = true, delisted_ms = NULL"
        ).format(cols, placeholders, updates)
        conn = await self._connection()
        async with conn.transaction(), conn.cursor() as cur:
            await cur.executemany(query, rows)

    async def deactivate_instruments(self, names: Iterable[str], delisted_ms: int) -> None:
        names = list(names)
        if not names:
            return
        conn = await self._connection()
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                "UPDATE instruments SET is_active = false, delisted_ms = %s "
                "WHERE instrument_name = ANY(%s) AND is_active",
                (delisted_ms, names),
            )

    async def write_delivery_prices(self, prices: Iterable[DeliveryPrice], recorded_ms: int) -> int:
        rows = [(p.index_name, p.delivery_date, p.price, p.currency, recorded_ms) for p in prices]
        if not rows:
            return 0
        conn = await self._connection()
        inserted = 0
        async with conn.transaction(), conn.cursor() as cur:
            for row in rows:
                await cur.execute(
                    "INSERT INTO delivery_prices "
                    "(index_name, delivery_date, delivery_price, currency, recorded_ms) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    row,
                )
                inserted += max(cur.rowcount, 0)
        return inserted

    async def write_status(self, row: RecorderStatusRow) -> None:
        cols = sql.SQL(", ").join(map(sql.Identifier, RecorderStatusRow._fields))
        placeholders = sql.SQL(", ").join(sql.Placeholder() * len(RecorderStatusRow._fields))
        conn = await self._connection()
        async with conn.transaction(), conn.cursor() as cur:
            query = sql.SQL("INSERT INTO recorder_status ({}) VALUES ({}) ON CONFLICT DO NOTHING")
            await cur.execute(query.format(cols, placeholders), row)

    async def write_connection_events(self, events: Sequence[ConnectionEvent]) -> None:
        if not events:
            return
        conn = await self._connection()
        async with conn.transaction(), conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO connection_events (ts, ts_ms, source, event, detail) "
                "VALUES (%s, %s, %s, %s, %s)",
                [
                    (ms_to_datetime(e.ts_ms), e.ts_ms, e.source, e.kind.value, e.detail)
                    for e in events
                ],
            )
