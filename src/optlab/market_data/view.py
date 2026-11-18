"""Point-in-time market data: ``MarketDataView(source, as_of=t)`` returns only what was known at t.

Every read of market data goes through this view (CLAUDE.md). It works over two sources with
the same tables and columns:

- ``ParquetSource``: exported days under ``data/exports/v1`` (backtests, on the laptop);
- ``DatabaseSource``: the live database (paper trading, the status page).

When a row counts as known, for each table:

| table            | known from                                                        |
| ---------------- | ----------------------------------------------------------------- |
| option_quotes    | ``ts``: the snapshot minute, built only from data received by then  |
| underlying_ticks | ``ts``, the same                                                   |
| option_trades    | ``recv_ts_ms``: when the recorder received it (not the trade time)  |
| instruments      | ``first_seen_ms``: when discovery first listed it                   |
| delivery_prices  | ``recorded_ms``: when the recorder fetched it                       |
| data_quality     | ``start_ts``; a period still open at t is cut off at t               |

Instruments are returned without the columns that change later (last seen, active, delisted).
Data-quality findings come without their detection time and hour, and a finding still open at
t loses its end and its detail, which would say how long it lasted.

Times are UTC milliseconds: every timestamp column ``x`` is returned as an int64 column
``x_ms``. Tables are sorted the same way by both sources, so they compare equal.

``snapshot_times`` lists the snapshot minutes of a period, leaving out the minutes the hourly
checks marked to skip (missing or partial snapshots, stale index). A live system would see the
same problems in the data at that minute, so skipping them does not look ahead.
"""

import bisect
import json
from collections import OrderedDict
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import psycopg
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from psycopg import sql

from optlab.domain.clock import MS_PER_DAY, MS_PER_MINUTE, floor_ms, ms_to_datetime
from optlab.market_data.export import (
    EXPORT_DIR,
    MANIFESTS,
    TEXT_TYPES,
    arrow_schema,
    table_columns,
)

# Canonical sort order of what the view returns (strings sort by bytes).
ORDER = {
    "option_quotes": ("ts_ms", "instrument_name"),
    "underlying_ticks": ("ts_ms", "instrument_name"),
    "option_trades": ("recv_ts_ms", "trade_id"),
    "instruments": ("instrument_name",),
    "delivery_prices": ("recorded_ms", "index_name", "delivery_date"),
    "data_quality": ("start_ts_ms", "check_name", "subject"),
}
INSTRUMENT_COLUMNS = (
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
)
QUALITY_COLUMNS = (
    "check_name",
    "scope",
    "subject",
    "start_ts_ms",
    "end_ts_ms",
    "minutes",
    "skip",
    "detail",
)


def normalize(table: pa.Table) -> pa.Table:
    """Timestamp columns ``x`` become int64 UTC milliseconds named ``x_ms``."""
    fields: list[pa.Field] = []
    arrays: list[Any] = []
    for f, column in zip(table.schema, table.columns, strict=True):
        if pa.types.is_timestamp(f.type):
            micros = pc.cast(pc.cast(column, pa.timestamp("us", tz="UTC")), pa.int64())
            arrays.append(pc.divide(micros, 1000))
            fields.append(pa.field(f.name + "_ms", pa.int64(), nullable=f.nullable))
        else:
            arrays.append(column)
            fields.append(f)
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def sort_table(table: pa.Table, name: str) -> pa.Table:
    if table.num_rows == 0:
        return table
    return table.take(pc.sort_indices(table, [(k, "ascending") for k in ORDER[name]]))


def between(table: pa.Table, column: str, start_ms: int, end_ms: int) -> pa.Table:
    """Rows with ``start_ms <= column <= end_ms``."""
    col = table.column(column)
    return table.filter(pc.and_(pc.greater_equal(col, start_ms), pc.less_equal(col, end_ms)))


class MarketDataSource(Protocol):
    """Time-range reads. Only ``MarketDataView`` should call these; it caps every range at t."""

    def snapshot_times(self, start_ms: int, end_ms: int) -> list[int]: ...

    def option_quotes(self, start_ms: int, end_ms: int) -> pa.Table: ...

    def underlying_ticks(self, start_ms: int, end_ms: int) -> pa.Table: ...

    def option_trades(self, start_ms: int, end_ms: int) -> pa.Table: ...

    def instruments(self, as_of_ms: int) -> pa.Table: ...

    def delivery_prices(self, as_of_ms: int) -> pa.Table: ...

    def quality(self, start_ms: int, end_ms: int) -> pa.Table: ...

    def skip_periods(self, start_ms: int, end_ms: int) -> list[tuple[int, int]]: ...


# ------------------------------------------------------------------------------------ view


class MarketDataView:
    """What was known at ``as_of`` (UTC ms), and nothing after it."""

    def __init__(self, source: MarketDataSource, as_of: int, *, max_age_ms: int = 5 * 60_000):
        self.source = source
        self.as_of = as_of
        self.max_age_ms = max_age_ms

    def snapshot_ms(self) -> int | None:
        """The latest snapshot minute at or before ``as_of`` and at most ``max_age_ms`` old."""
        times = self.source.snapshot_times(self.as_of - self.max_age_ms, self.as_of)
        return times[-1] if times else None

    def option_chain(self) -> pa.Table:
        """Every option row of the latest snapshot (empty if there is none)."""
        ts = self.snapshot_ms()
        if ts is None:
            return self.source.option_quotes(self.as_of + 1, self.as_of)  # empty, right schema
        return self.source.option_quotes(ts, ts)

    def underlyings(self) -> pa.Table:
        """Indexes, perpetuals and futures at the latest snapshot."""
        ts = self.snapshot_ms()
        if ts is None:
            return self.source.underlying_ticks(self.as_of + 1, self.as_of)
        return self.source.underlying_ticks(ts, ts)

    def option_history(self, since_ms: int) -> pa.Table:
        return self.source.option_quotes(since_ms, self.as_of)

    def underlying_history(self, since_ms: int) -> pa.Table:
        return self.source.underlying_ticks(since_ms, self.as_of)

    def trades(self, since_ms: int) -> pa.Table:
        """Trades received between ``since_ms`` and ``as_of``."""
        return self.source.option_trades(since_ms, self.as_of)

    def instruments(self) -> pa.Table:
        return self.source.instruments(self.as_of)

    def delivery_prices(self) -> pa.Table:
        return self.source.delivery_prices(self.as_of)

    def quality(self, since_ms: int) -> pa.Table:
        """Findings that began between ``since_ms`` and ``as_of``; open ones cut off at as_of."""
        table = self.source.quality(since_ms, self.as_of)
        if table.num_rows == 0:
            return table
        # A finding still covering the current minute may go on: whether the next minute is
        # affected is not known yet, so its end becomes the end of the current minute.
        cut = floor_ms(self.as_of, MS_PER_MINUTE) + MS_PER_MINUTE
        end = table.column("end_ts_ms")
        is_open = pc.greater_equal(end, cut)
        start = table.column("start_ts_ms")
        new_end = pc.if_else(is_open, cut, end)
        minutes = pc.cast(pc.divide(pc.subtract(new_end, start), MS_PER_MINUTE), pa.int32())
        detail = pc.if_else(is_open, "still open", table.column("detail"))
        for name, values in (("end_ts_ms", new_end), ("minutes", minutes), ("detail", detail)):
            i = table.schema.get_field_index(name)
            table = table.set_column(i, table.schema.field(i), values)
        return table


def snapshot_times(
    source: MarketDataSource, start_ms: int, end_ms: int, *, skip_flagged: bool = True
) -> Iterator[int]:
    """Snapshot minutes in ``[start_ms, end_ms]``, without those marked to skip."""
    times = source.snapshot_times(start_ms, end_ms)
    periods = sorted(source.skip_periods(start_ms, end_ms)) if skip_flagged else []
    for t in times:
        if any(s <= t < e for s, e in periods):
            continue
        yield t


# ------------------------------------------------------------------------------------ Parquet


class ParquetSource:
    """Exported days under ``root`` (``data/exports/v1``), a few days cached in memory."""

    def __init__(self, root: Path, *, cache_days: int = 4) -> None:
        self.root = root
        self._cache: OrderedDict[tuple[str, date], pa.Table] = OrderedDict()
        self._snapshots: dict[date, list[int]] = {}
        self._cache_days = cache_days

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> "ParquetSource":
        return cls(data_dir / EXPORT_DIR)

    def days(self) -> list[date]:
        folder = self.root / MANIFESTS
        if not folder.is_dir():
            return []
        return sorted(date.fromisoformat(p.stem) for p in folder.glob("*.json"))

    def dataset_versions(self, first: date, last: date) -> dict[date, str]:
        """Each exported day's dataset version (from its manifest), for the run record."""
        out: dict[date, str] = {}
        for day in self.days():
            if first <= day <= last:
                manifest = json.loads((self.root / MANIFESTS / f"{day}.json").read_text())
                out[day] = manifest["dataset_version"]
        return out

    def _day(self, table: str, day: date) -> pa.Table:
        key = (table, day)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        folder = self.root / table / f"date={day.isoformat()}"
        files = sorted(folder.rglob("*.parquet")) if folder.is_dir() else []
        data = pa.concat_tables([pq.read_table(f) for f in files]) if files else self._empty(table)
        data = sort_table(normalize(data), table)
        self._cache[key] = data
        while len(self._cache) > self._cache_days * len(ORDER):
            self._cache.popitem(last=False)
        return data

    def _empty(self, table: str) -> pa.Table:
        for day in reversed(self.days()):
            folder = self.root / table / f"date={day.isoformat()}"
            for f in sorted(folder.rglob("*.parquet")) if folder.is_dir() else []:
                return pq.read_schema(f).empty_table()
        raise FileNotFoundError(f"no {table} files under {self.root}")

    def _range(self, table: str, column: str, start_ms: int, end_ms: int) -> pa.Table:
        if end_ms < start_ms:
            return between(self._day(table, self._any_day()), column, 1, 0)
        first = ms_to_datetime(floor_ms(start_ms, MS_PER_DAY)).date()
        last = ms_to_datetime(floor_ms(end_ms, MS_PER_DAY)).date()
        days = [d for d in self.days() if first <= d <= last]
        if not days:
            return between(self._day(table, self._any_day()), column, 1, 0)
        parts = [between(self._day(table, d), column, start_ms, end_ms) for d in days]
        return sort_table(pa.concat_tables(parts), table) if len(parts) > 1 else parts[0]

    def _any_day(self) -> date:
        days = self.days()
        if not days:
            raise FileNotFoundError(f"no exported days under {self.root}")
        return days[-1]

    def snapshot_times(self, start_ms: int, end_ms: int) -> list[int]:
        out: list[int] = []
        first = ms_to_datetime(floor_ms(start_ms, MS_PER_DAY)).date()
        last = ms_to_datetime(floor_ms(max(end_ms, start_ms), MS_PER_DAY)).date()
        for day in self.days():
            if not first <= day <= last:
                continue
            if day not in self._snapshots:
                ts = self._day("option_quotes", day).column("ts_ms")
                self._snapshots[day] = sorted(pc.unique(ts).to_pylist())
            times = self._snapshots[day]
            out.extend(
                times[bisect.bisect_left(times, start_ms) : bisect.bisect_right(times, end_ms)]
            )
        return out

    def option_quotes(self, start_ms: int, end_ms: int) -> pa.Table:
        return self._range("option_quotes", "ts_ms", start_ms, end_ms)

    def underlying_ticks(self, start_ms: int, end_ms: int) -> pa.Table:
        return self._range("underlying_ticks", "ts_ms", start_ms, end_ms)

    def option_trades(self, start_ms: int, end_ms: int) -> pa.Table:
        # Files are split by trade time, so a trade received just after midnight sits in the
        # previous day's file: read one day further back, then filter on receive time.
        table = self._range("option_trades", "recv_ts_ms", start_ms - MS_PER_DAY, end_ms)
        return table.filter(pc.greater_equal(table.column("recv_ts_ms"), start_ms))

    def instruments(self, as_of_ms: int) -> pa.Table:
        day = ms_to_datetime(floor_ms(as_of_ms, MS_PER_DAY)).date()
        if day in self.days():
            table = self._day("instruments", day)
        else:
            table = self._day("instruments", self._any_day()).slice(0, 0)
        table = table.filter(pc.less_equal(table.column("first_seen_ms"), as_of_ms))
        return table.select(list(INSTRUMENT_COLUMNS))

    def delivery_prices(self, as_of_ms: int) -> pa.Table:
        days = [d for d in self.days() if d <= ms_to_datetime(as_of_ms).date()]
        if not days:
            return self._day("delivery_prices", self._any_day()).slice(0, 0)
        table = pa.concat_tables([self._day("delivery_prices", d) for d in days])
        table = table.filter(pc.less_equal(table.column("recorded_ms"), as_of_ms))
        return sort_table(table, "delivery_prices")

    def quality(self, start_ms: int, end_ms: int) -> pa.Table:
        table = self._range("data_quality", "start_ts_ms", start_ms, end_ms)
        return table.select(list(QUALITY_COLUMNS))

    def skip_periods(self, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
        table = self._range("data_quality", "start_ts_ms", start_ms - MS_PER_DAY, end_ms)
        table = table.filter(
            pc.and_(table.column("skip"), pc.greater(table.column("end_ts_ms"), start_ms))
        )
        return list(
            zip(
                table.column("start_ts_ms").to_pylist(),
                table.column("end_ts_ms").to_pylist(),
                strict=True,
            )
        )


# ------------------------------------------------------------------------------------ database


class DatabaseSource:
    """The live database. Opens one connection and keeps it."""

    def __init__(self, database_url: str) -> None:
        self._conn = psycopg.connect(
            database_url,
            autocommit=True,
            application_name="optlab-view",
            options="-c default_transaction_read_only=on",
        )
        self._columns: dict[str, list[tuple[str, str, bool]]] = {}

    def close(self) -> None:
        self._conn.close()

    def _query(self, table: str, where: str, params: dict[str, Any]) -> pa.Table:
        if table not in self._columns:
            self._columns[table] = table_columns(self._conn, table)
        columns = self._columns[table]
        cols = sql.SQL(", ").join(
            sql.SQL("{}::text").format(sql.Identifier(n)) if t in TEXT_TYPES else sql.Identifier(n)
            for n, t, _ in columns
        )
        query = sql.SQL("SELECT {} FROM {} WHERE {}").format(
            cols, sql.Identifier(table), sql.SQL(where)
        )
        rows = self._conn.execute(query, params).fetchall()
        schema = arrow_schema(columns)
        data = list(zip(*rows, strict=True)) if rows else [()] * len(schema)
        arrays = [pa.array(list(c), type=f.type) for c, f in zip(data, schema, strict=True)]
        return sort_table(normalize(pa.Table.from_arrays(arrays, schema=schema)), table)

    @staticmethod
    def _ts(start_ms: int, end_ms: int) -> dict[str, Any]:
        return {"start": ms_to_datetime(start_ms), "end": ms_to_datetime(end_ms)}

    def snapshot_times(self, start_ms: int, end_ms: int) -> list[int]:
        rows = self._conn.execute(
            "SELECT DISTINCT utc_ms(ts) FROM option_quotes WHERE ts >= %(start)s AND ts <= %(end)s "
            "ORDER BY 1",
            self._ts(start_ms, end_ms),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def option_quotes(self, start_ms: int, end_ms: int) -> pa.Table:
        return self._query(
            "option_quotes", "ts >= %(start)s AND ts <= %(end)s", self._ts(start_ms, end_ms)
        )

    def underlying_ticks(self, start_ms: int, end_ms: int) -> pa.Table:
        return self._query(
            "underlying_ticks", "ts >= %(start)s AND ts <= %(end)s", self._ts(start_ms, end_ms)
        )

    def option_trades(self, start_ms: int, end_ms: int) -> pa.Table:
        return self._query(
            "option_trades",
            "recv_ts_ms >= %(a)s AND recv_ts_ms <= %(b)s",
            {"a": start_ms, "b": end_ms},
        )

    def instruments(self, as_of_ms: int) -> pa.Table:
        day = floor_ms(as_of_ms, MS_PER_DAY)
        table = self._query(
            "instruments",
            "first_seen_ms <= %(t)s AND (delisted_ms IS NULL OR delisted_ms >= %(day)s)",
            {"t": as_of_ms, "day": day},
        )
        return table.select(list(INSTRUMENT_COLUMNS))

    def delivery_prices(self, as_of_ms: int) -> pa.Table:
        return self._query("delivery_prices", "recorded_ms <= %(t)s", {"t": as_of_ms})

    def quality(self, start_ms: int, end_ms: int) -> pa.Table:
        table = self._query(
            "data_quality",
            "start_ts >= %(start)s AND start_ts <= %(end)s",
            self._ts(start_ms, end_ms),
        )
        return table.select(list(QUALITY_COLUMNS))

    def skip_periods(self, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
        rows = self._conn.execute(
            "SELECT utc_ms(start_ts), utc_ms(end_ts) FROM data_quality "
            "WHERE skip AND start_ts <= %(end)s AND end_ts > %(start)s ORDER BY 1",
            self._ts(start_ms, end_ms),
        ).fetchall()
        return [(int(a), int(b)) for a, b in rows]


def view_tables(view: MarketDataView, since_ms: int) -> dict[str, pa.Table]:
    """Everything a view returns, by name (used by the leakage and consistency tests)."""
    return {
        "option_chain": view.option_chain(),
        "underlyings": view.underlyings(),
        "option_history": view.option_history(since_ms),
        "underlying_history": view.underlying_history(since_ms),
        "trades": view.trades(since_ms),
        "instruments": view.instruments(),
        "delivery_prices": view.delivery_prices(),
        "quality": view.quality(since_ms),
    }
