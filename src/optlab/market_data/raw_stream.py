"""The raw stream: every best bid/ask change and every option trade, in hourly Parquet files.

Layout: ``{root}/{stream}/{YYYY-MM-DD}/{stream}_{YYYY-MM-DD}T{HH}_{run}.parquet``, one file per
stream, UTC hour of receipt and recorder run (a restart within an hour starts a second file).
Files are zstd-compressed. While an hour is being written its file is named
``*.parquet.inprogress``; it is renamed to ``*.parquet`` once closed, so a finished name always
means a complete, readable file. A file left ``.inprogress`` by a crash cannot be read (Parquet
writes its footer last) and is renamed ``.incomplete`` at the next start.

Best bid/ask changes come from the 100 ms ticker channel: a row is written whenever an
instrument's bid, bid size, ask or ask size differs from the last row written for it.
"""

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from optlab.domain.clock import MS_PER_HOUR, MS_PER_MINUTE, floor_ms, ms_to_datetime
from optlab.domain.market import Ticker, Trade

logger = logging.getLogger(__name__)

QUOTES = "quotes"
TRADES = "trades"

QUOTE_SCHEMA = pa.schema(
    [
        pa.field("recv_ts_ms", pa.int64(), nullable=False),
        pa.field("exchange_ts_ms", pa.int64(), nullable=False),
        pa.field("instrument_name", pa.string(), nullable=False),
        pa.field("bid", pa.float64()),
        pa.field("bid_size", pa.float64(), nullable=False),
        pa.field("ask", pa.float64()),
        pa.field("ask_size", pa.float64(), nullable=False),
    ]
)

TRADE_SCHEMA = pa.schema(
    [
        pa.field("recv_ts_ms", pa.int64(), nullable=False),
        pa.field("exchange_ts_ms", pa.int64(), nullable=False),
        pa.field("instrument_name", pa.string(), nullable=False),
        pa.field("trade_id", pa.string(), nullable=False),
        pa.field("trade_seq", pa.int64()),
        pa.field("price", pa.float64(), nullable=False),
        pa.field("amount", pa.float64(), nullable=False),
        pa.field("direction", pa.string(), nullable=False),
        pa.field("iv", pa.float64()),
        pa.field("mark_price", pa.float64()),
        pa.field("index_price", pa.float64()),
        pa.field("tick_direction", pa.int8()),
        pa.field("block_trade_id", pa.string()),
        pa.field("liquidation", pa.string()),
        pa.field("combo_id", pa.string()),
    ]
)

INPROGRESS = ".inprogress"
INCOMPLETE = ".incomplete"


class BboChangeFilter:
    """Passes a ticker only when its best bid/ask (prices or sizes) changed."""

    def __init__(self) -> None:
        self._last: dict[str, tuple[float | None, float, float | None, float]] = {}

    def changed(self, t: Ticker) -> bool:
        bbo = (t.bid, t.bid_size, t.ask, t.ask_size)
        if self._last.get(t.instrument_name) == bbo:
            return False
        self._last[t.instrument_name] = bbo
        return True

    def forget(self, names: Iterable[str]) -> None:
        for name in names:
            self._last.pop(name, None)


def quote_record(t: Ticker) -> tuple[Any, ...]:
    return (t.recv_ts_ms, t.exchange_ts_ms, t.instrument_name, t.bid, t.bid_size, t.ask, t.ask_size)


def trade_record(t: Trade) -> tuple[Any, ...]:
    return (
        t.recv_ts_ms,
        t.exchange_ts_ms,
        t.instrument_name,
        t.trade_id,
        t.trade_seq,
        t.price,
        t.amount,
        t.direction.value,
        t.iv,
        t.mark_price,
        t.index_price,
        t.tick_direction,
        t.block_trade_id,
        t.liquidation,
        t.combo_id,
    )


@dataclass
class _OpenFile:
    hour_ms: int
    path: Path  # final name
    writer: pq.ParquetWriter
    rows: int = 0


class HourlyParquetStream:
    """Buffers records in memory and writes them to one Parquet file per UTC hour.

    ``append`` is cheap and runs on the event loop. ``flush`` does the file work and is meant to
    run in a worker thread; it writes buffered rows as row groups and closes files whose hour
    has passed. The first field of every record must be the receive time in UTC ms.
    """

    def __init__(
        self,
        root: Path,
        stream: str,
        schema: pa.Schema,
        run_id: str,
        *,
        compression_level: int = 6,
        max_buffer_rows: int = 200_000,
        max_buffer_age_ms: int = 10 * MS_PER_MINUTE,
    ) -> None:
        self.root = root
        self.stream = stream
        self.schema = schema
        self.run_id = run_id
        self.compression_level = compression_level
        self.max_buffer_rows = max_buffer_rows
        self.max_buffer_age_ms = max_buffer_age_ms
        self._rows: list[tuple[Any, ...]] = []
        self._oldest_ms: int | None = None
        self._lock = threading.Lock()  # guards _rows between the loop and the flush thread
        self._io_lock = threading.Lock()  # one flush at a time
        self._open: dict[int, _OpenFile] = {}
        self.rows_written = 0
        self.files_closed = 0

    def append(self, record: tuple[Any, ...]) -> None:
        with self._lock:
            if self._oldest_ms is None:
                self._oldest_ms = record[0]
            self._rows.append(record)

    @property
    def buffered(self) -> int:
        return len(self._rows)

    def due(self, now_ms: int) -> bool:
        """True when a flush is needed: buffer full or old, or an hour to close."""
        if len(self._rows) >= self.max_buffer_rows:
            return True
        if self._oldest_ms is not None and now_ms - self._oldest_ms >= self.max_buffer_age_ms:
            return True
        current_hour = floor_ms(now_ms, MS_PER_HOUR)
        if any(h < current_hour for h in self._open):
            return True
        return self._oldest_ms is not None and self._oldest_ms < current_hour

    def flush(self, now_ms: int, *, close_all: bool = False) -> None:
        """Write buffered rows and close finished hours (all files if ``close_all``)."""
        with self._io_lock:
            with self._lock:
                rows, self._rows, self._oldest_ms = self._rows, [], None
            by_hour: dict[int, list[tuple[Any, ...]]] = {}
            for row in rows:
                by_hour.setdefault(floor_ms(row[0], MS_PER_HOUR), []).append(row)
            for hour_ms, hour_rows in sorted(by_hour.items()):
                self._write(hour_ms, hour_rows)
            current_hour = floor_ms(now_ms, MS_PER_HOUR)
            for hour_ms in sorted(self._open):
                if close_all or hour_ms < current_hour:
                    self._close(hour_ms)

    def _write(self, hour_ms: int, rows: list[tuple[Any, ...]]) -> None:
        open_file = self._open.get(hour_ms) or self._open_file(hour_ms)
        columns = list(zip(*rows, strict=True))
        table = pa.Table.from_arrays(
            [pa.array(col, type=f.type) for col, f in zip(columns, self.schema, strict=True)],
            schema=self.schema,
        )
        open_file.writer.write_table(table)
        open_file.rows += len(rows)
        self.rows_written += len(rows)

    def _open_file(self, hour_ms: int) -> _OpenFile:
        hour = ms_to_datetime(hour_ms)
        day = hour.strftime("%Y-%m-%d")
        folder = self.root / self.stream / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{self.stream}_{day}T{hour:%H}_{self.run_id}.parquet"
        writer = pq.ParquetWriter(
            str(path) + INPROGRESS,
            self.schema,
            compression="zstd",
            compression_level=self.compression_level,
            use_dictionary=True,
            write_statistics=True,
        )
        open_file = _OpenFile(hour_ms=hour_ms, path=path, writer=writer)
        self._open[hour_ms] = open_file
        return open_file

    def _close(self, hour_ms: int) -> None:
        open_file = self._open.pop(hour_ms)
        open_file.writer.close()
        Path(str(open_file.path) + INPROGRESS).rename(open_file.path)
        self.files_closed += 1
        logger.info("closed %s (%d rows)", open_file.path.name, open_file.rows)


def quarantine_unfinished(root: Path) -> list[Path]:
    """Rename files a crash left ``.inprogress`` to ``.incomplete``; return the new paths."""
    moved: list[Path] = []
    if not root.exists():
        return moved
    for path in sorted(root.rglob(f"*.parquet{INPROGRESS}")):
        target = path.with_name(path.name.removesuffix(INPROGRESS) + INCOMPLETE)
        path.rename(target)
        moved.append(target)
        logger.warning("unfinished raw file from a previous run: %s", target.name)
    return moved
