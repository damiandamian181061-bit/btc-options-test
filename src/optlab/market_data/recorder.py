"""The recorder: Deribit public data into TimescaleDB snapshots and the raw Parquet stream.

What it does, in order:

1. Discovers the instruments to record (``universe``), stores them in ``instruments`` and
   subscribes to their channels. Discovery repeats every 10 minutes and just after 08:00 UTC.
2. Keeps the latest ticker per instrument and index in memory and, at each minute boundary,
   writes one row per instrument to ``option_quotes`` or ``underlying_ticks`` (see
   ``snapshot`` for the point-in-time and staleness rules).
3. Writes option trades to ``option_trades`` every few seconds (stored once per trade_id).
4. Fetches delivery prices at start-up and several times after each 08:00 UTC expiry.
5. Records every connect, disconnect and reconnect in ``connection_events``.
6. Writes every best bid/ask change and every option trade to hourly Parquet files.
7. Once a minute, logs a status line and stores it in ``recorder_status``: connections, message
   rate, memory, and the bytes of raw files written so far for the UTC day. A day passing the
   raw-stream warning level (1 GB, decided 14 Nov) logs a warning once; nothing is thinned or
   dropped.

Database writes go through one queue and one writer task. If the database is unreachable, the
writer retries with backoff while new work queues up (up to ``max_pending_jobs``; beyond that
the oldest work is dropped and logged), so a short database outage loses nothing.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from optlab.domain.clock import (
    MS_PER_DAY,
    MS_PER_MINUTE,
    MS_PER_SECOND,
    Clock,
    SystemClock,
    floor_ms,
    ms_to_datetime,
)
from optlab.domain.market import (
    ConnectionEvent,
    ConnectionEventKind,
    DeliveryPrice,
    IndexPrice,
    Instrument,
    InstrumentKind,
    Ticker,
    Trade,
)
from optlab.market_data.raw_stream import (
    QUOTE_SCHEMA,
    QUOTES,
    TRADE_SCHEMA,
    TRADES,
    BboChangeFilter,
    HourlyParquetStream,
    quarantine_unfinished,
    quote_record,
    trade_record,
)
from optlab.market_data.rows import OptionTradeRow, RecorderStatusRow, option_trade_row
from optlab.market_data.sink import MarketDataSink
from optlab.market_data.snapshot import LatestState, Snapshot, take_snapshot
from optlab.market_data.universe import (
    DISCOVERY_INTERVAL_MS,
    DISCOVERY_QUERIES,
    Universe,
    build_universe,
    next_discovery_ms,
)

logger = logging.getLogger(__name__)

RECENT_TRADE_IDS = 50_000

# Delivery prices are checked at these UTC times of day (after the 08:00 expiry).
DELIVERY_CHECKS_MS = tuple(8 * 3_600_000 + m * MS_PER_MINUTE for m in (1, 5, 15, 30))


class RecorderStoreLike(Protocol):
    async def write_snapshot(self, snapshot: Snapshot) -> tuple[int, int]: ...

    async def write_trades(self, rows: Sequence[OptionTradeRow]) -> int: ...

    async def upsert_instruments(self, instruments: Sequence[Instrument], seen_ms: int) -> None: ...

    async def deactivate_instruments(self, names: Sequence[str], delisted_ms: int) -> None: ...

    async def write_delivery_prices(
        self, prices: Sequence[DeliveryPrice], recorded_ms: int
    ) -> int: ...

    async def write_connection_events(self, events: Sequence[ConnectionEvent]) -> None: ...

    async def write_status(self, row: RecorderStatusRow) -> None: ...

    async def reset(self) -> None: ...


class MarketDataClient(Protocol):
    messages: int
    decode_errors: int

    def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def wait_ready(self, timeout: float) -> None: ...

    async def get_instruments(self, currency: str, kind: str) -> list[Instrument]: ...

    async def get_delivery_prices(
        self, index_name: str, currency: str, count: int = 10
    ) -> list[DeliveryPrice]: ...

    async def set_subscriptions(self, desired: set[str]) -> object: ...

    @property
    def connections(self) -> int: ...

    @property
    def connected(self) -> int: ...

    @property
    def channels(self) -> frozenset[str]: ...


@dataclass(frozen=True)
class RecorderConfig:
    data_dir: Path
    ticker_interval: str = "100ms"
    snapshot_interval_ms: int = MS_PER_MINUTE
    max_age_ms: int = 5 * MS_PER_MINUTE
    discovery_interval_ms: int = DISCOVERY_INTERVAL_MS
    trade_flush_s: float = 5.0
    raw_flush_check_s: float = 10.0
    status_log_s: float = 60.0
    delivery_backfill: int = 30
    max_pending_jobs: int = 240
    unknown_trade_max_age_ms: int = 15 * MS_PER_MINUTE
    min_option_fraction: float = 0.5  # refuse a discovery that loses more than half the options
    raw_daily_warn_bytes: int = 1_000_000_000  # warning level only: data is never dropped

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"


@dataclass
class _Job:
    name: str
    run: Callable[[RecorderStoreLike], Awaitable[object]]


@dataclass
class RecorderStats:
    snapshots: int = 0
    option_rows: int = 0
    underlying_rows: int = 0
    trades: int = 0
    last_snapshot_ms: int | None = None
    last_option_rows: int = 0
    dropped_jobs: int = 0
    failed_writes: int = 0
    discoveries: int = 0
    refused_discoveries: int = 0
    extra: dict[str, int] = field(default_factory=dict)


class Recorder(MarketDataSink):
    """Runs until cancelled. Construct the client with this recorder as its sink."""

    def __init__(
        self,
        store: RecorderStoreLike,
        client_factory: Callable[[MarketDataSink], MarketDataClient],
        config: RecorderConfig,
        *,
        run_id: str,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._clock = clock or SystemClock()
        self.run_id = run_id
        self.universe = Universe()
        self._state = LatestState()
        self._bbo = BboChangeFilter()
        self._next_snapshot_ms = (
            floor_ms(self._clock.now_ms(), config.snapshot_interval_ms)
            + config.snapshot_interval_ms
        )
        self._trade_rows: list[OptionTradeRow] = []
        self._unknown_trades: list[Trade] = []
        # Trade ids already kept, so a trade sent twice is written once to the raw stream too
        # (the database also skips duplicates by trade_id).
        self._recent_trades: set[str] = set()
        self._recent_trade_order: deque[str] = deque()
        self._jobs: deque[_Job] = deque()
        self._job_ready = asyncio.Event()
        self._refusals = 0
        self._started_ms = self._clock.now_ms()
        self._raw_warned: set[date] = set()
        self.stats = RecorderStats()
        self.raw_quotes = HourlyParquetStream(config.raw_dir, QUOTES, QUOTE_SCHEMA, run_id)
        self.raw_trades = HourlyParquetStream(config.raw_dir, TRADES, TRADE_SCHEMA, run_id)
        self._client = client_factory(self)

    # ----------------------------------------------------------------- sink callbacks

    def on_ticker(self, ticker: Ticker) -> None:
        self._roll(ticker.recv_ts_ms)
        if ticker.instrument_name not in self.universe.instruments:
            return
        self._state.update_ticker(ticker)
        if self._bbo.changed(ticker):
            self.raw_quotes.append(quote_record(ticker))

    def on_index_price(self, price: IndexPrice) -> None:
        self._roll(price.recv_ts_ms)
        self._state.update_index(price)

    def on_trades(self, trades: list[Trade]) -> None:
        for trade in trades:
            instrument = self.universe.instruments.get(trade.instrument_name)
            if instrument is None:
                # Maybe listed since the last discovery; checked again after the next one.
                if len(self._unknown_trades) < 10_000:
                    self._unknown_trades.append(trade)
                continue
            self._keep_trade(instrument, trade)

    def on_connection_event(self, event: ConnectionEvent) -> None:
        self._enqueue(_Job("connection event", lambda s: s.write_connection_events([event])))

    def on_stream_lost(self, keys: frozenset[str]) -> None:
        self._state.drop(keys)
        self._bbo.forget(keys)

    # ----------------------------------------------------------------- main loop

    async def run(self) -> None:
        for path in quarantine_unfinished(self._config.raw_dir):
            logger.warning("raw file left by a crash renamed to %s", path)
        self._event(ConnectionEventKind.STARTED, f"run {self.run_id}")
        writer = asyncio.create_task(self._writer_loop(), name="db-writer")
        tasks: list[asyncio.Task[None]] = []
        try:
            self._client.start()
            await self._discover_until_success()
            tasks = [
                asyncio.create_task(coro, name=name)
                for name, coro in (
                    ("snapshot-timer", self._snapshot_timer()),
                    ("discovery", self._discovery_loop()),
                    ("delivery-prices", self._delivery_loop()),
                    ("trade-flush", self._trade_flush_loop()),
                    ("raw-flush", self._raw_flush_loop()),
                    ("status", self._status_loop()),
                )
            ]
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._shutdown(writer)

    async def _shutdown(self, writer: asyncio.Task[None]) -> None:
        await self._client.stop()
        self._flush_trades()
        self._event(ConnectionEventKind.STOPPED, f"run {self.run_id}")
        try:
            await asyncio.wait_for(self._drain(), timeout=15)
        except TimeoutError:
            logger.error("shutdown: %d database jobs not written", len(self._jobs))
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        now = self._clock.now_ms()
        for stream in (self.raw_quotes, self.raw_trades):
            try:
                await asyncio.to_thread(stream.flush, now, close_all=True)
            except Exception:
                logger.exception("shutdown: closing %s raw files failed", stream.stream)

    async def _drain(self) -> None:
        while self._jobs:
            await self._clock.sleep(0.05)

    # ----------------------------------------------------------------- snapshots

    def _roll(self, now_ms: int) -> None:
        """Take the snapshot for the minute boundary just crossed, before applying new data."""
        if now_ms < self._next_snapshot_ms:
            return
        interval = self._config.snapshot_interval_ms
        boundary = floor_ms(now_ms, interval)
        self._next_snapshot_ms = boundary + interval
        snapshot = take_snapshot(boundary, self.universe, self._state, self._config.max_age_ms)
        self.stats.snapshots += 1
        self.stats.last_snapshot_ms = boundary
        self.stats.last_option_rows = len(snapshot.options)
        if not snapshot.options and not snapshot.underlyings:
            logger.warning("snapshot %d: no fresh data, nothing written", boundary)
            return
        self._enqueue(_Job("snapshot", self._snapshot_writer(snapshot)))

    def _snapshot_writer(
        self, snapshot: Snapshot
    ) -> Callable[[RecorderStoreLike], Awaitable[object]]:
        async def write(store: RecorderStoreLike) -> None:
            options, underlyings = await store.write_snapshot(snapshot)
            self.stats.option_rows += options
            self.stats.underlying_rows += underlyings

        return write

    async def _snapshot_timer(self) -> None:
        """Rolls the snapshot when the feed is too quiet to do it."""
        while True:
            wait_ms = self._next_snapshot_ms + 50 - self._clock.now_ms()
            if wait_ms > 0:
                await self._clock.sleep(wait_ms / MS_PER_SECOND)
            self._roll(self._clock.now_ms())

    # ----------------------------------------------------------------- discovery

    async def discover(self) -> bool:
        """Refresh the universe and subscriptions. False if the result looked wrong."""
        found: list[Instrument] = []
        for currency, kind in DISCOVERY_QUERIES:
            found.extend(await self._client.get_instruments(currency, kind))
        universe = build_universe(found)
        before = len(self.universe.options)
        after = len(universe.options)
        if after == 0 or (
            before and after < self._config.min_option_fraction * before and self._refusals < 2
        ):
            self._refusals += 1
            self.stats.refused_discoveries += 1
            logger.error("discovery refused: %d options found, %d before", after, before)
            return False
        self._refusals = 0
        added, removed = self.universe.diff(universe)
        now = self._clock.now_ms()
        self.universe = universe
        self._state.drop(removed)
        self._bbo.forget(frozenset(removed))
        instruments = list(universe.instruments.values())
        self._enqueue(_Job("instruments", lambda s: s.upsert_instruments(instruments, now)))
        if removed:
            gone = sorted(removed)
            self._enqueue(_Job("delisted", lambda s: s.deactivate_instruments(gone, now)))
        await self._client.set_subscriptions(universe.channels(self._config.ticker_interval))
        self._recheck_unknown_trades(now)
        self.stats.discoveries += 1
        logger.info(
            "discovery: %d options, %d underlyings (+%d -%d); %d channels on %d connections",
            after,
            len(universe.underlyings),
            len(added),
            len(removed),
            len(self._client.channels),
            self._client.connections,
        )
        return True

    async def _discover_until_success(self) -> None:
        delay = 2.0
        while True:
            try:
                await self._client.wait_ready(timeout=30)
                if await self.discover():
                    return
            except Exception as exc:
                logger.error("first discovery failed (%r); retrying in %.0fs", exc, delay)
            await self._clock.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def _discovery_loop(self) -> None:
        while True:
            now = self._clock.now_ms()
            await self._sleep_until(next_discovery_ms(now, self._config.discovery_interval_ms))
            for attempt in range(3):
                try:
                    if await self.discover():
                        break
                except Exception as exc:
                    logger.error("discovery failed (%r), attempt %d", exc, attempt + 1)
                await self._clock.sleep(30)

    def _recheck_unknown_trades(self, now_ms: int) -> None:
        pending, self._unknown_trades = self._unknown_trades, []
        for trade in pending:
            instrument = self.universe.instruments.get(trade.instrument_name)
            if instrument is not None:
                self._keep_trade(instrument, trade)
            elif now_ms - trade.recv_ts_ms < self._config.unknown_trade_max_age_ms:
                self._unknown_trades.append(trade)

    # ----------------------------------------------------------------- trades and delivery

    def _keep_trade(self, instrument: Instrument, trade: Trade) -> None:
        if instrument.kind is not InstrumentKind.OPTION or trade.trade_id in self._recent_trades:
            return
        self._recent_trades.add(trade.trade_id)
        self._recent_trade_order.append(trade.trade_id)
        if len(self._recent_trade_order) > RECENT_TRADE_IDS:
            self._recent_trades.discard(self._recent_trade_order.popleft())
        self._trade_rows.append(option_trade_row(instrument, trade))
        self.raw_trades.append(trade_record(trade))

    def _flush_trades(self) -> None:
        if not self._trade_rows:
            return
        rows, self._trade_rows = self._trade_rows, []

        async def write(store: RecorderStoreLike) -> None:
            self.stats.trades += await store.write_trades(rows)

        self._enqueue(_Job("trades", write))

    async def _trade_flush_loop(self) -> None:
        while True:
            await self._clock.sleep(self._config.trade_flush_s)
            self._flush_trades()

    async def fetch_delivery_prices(self, count: int) -> None:
        now = self._clock.now_ms()
        for index_name, currency in sorted(self.universe.index_currencies().items()):
            prices = await self._client.get_delivery_prices(index_name, currency, count)
            self._enqueue(_Job("delivery prices", self._delivery_writer(prices, now)))

    @staticmethod
    def _delivery_writer(
        prices: list[DeliveryPrice], now_ms: int
    ) -> Callable[[RecorderStoreLike], Awaitable[object]]:
        async def write(store: RecorderStoreLike) -> None:
            await store.write_delivery_prices(prices, now_ms)

        return write

    async def _delivery_loop(self) -> None:
        try:
            await self.fetch_delivery_prices(self._config.delivery_backfill)
        except Exception as exc:
            logger.error("delivery price backfill failed (%r)", exc)
        while True:
            await self._sleep_until(next_delivery_check_ms(self._clock.now_ms()))
            try:
                await self.fetch_delivery_prices(5)
            except Exception as exc:
                logger.error("delivery price check failed (%r)", exc)

    # ----------------------------------------------------------------- raw stream

    async def _raw_flush_loop(self) -> None:
        while True:
            await self._clock.sleep(self._config.raw_flush_check_s)
            now = self._clock.now_ms()
            for stream in (self.raw_quotes, self.raw_trades):
                if stream.due(now):
                    try:
                        await asyncio.to_thread(stream.flush, now)
                    except Exception:
                        logger.exception("writing %s raw files failed", stream.stream)

    # ----------------------------------------------------------------- database writer

    def _enqueue(self, job: _Job) -> None:
        self._jobs.append(job)
        while len(self._jobs) > self._config.max_pending_jobs:
            dropped = self._jobs.popleft()
            self.stats.dropped_jobs += 1
            logger.error("database backlog full: dropped a %s write", dropped.name)
        self._job_ready.set()

    async def _writer_loop(self) -> None:
        while True:
            while not self._jobs:
                self._job_ready.clear()
                await self._job_ready.wait()
            job = self._jobs[0]
            delay = 1.0
            while True:
                try:
                    await job.run(self._store)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.stats.failed_writes += 1
                    logger.error(
                        "database write (%s) failed: %r; retry in %.0fs", job.name, exc, delay
                    )
                    await self._store.reset()
                    await self._clock.sleep(delay)
                    delay = min(delay * 2, 30.0)
            if self._jobs and self._jobs[0] is job:
                self._jobs.popleft()

    # ----------------------------------------------------------------- helpers

    def _event(self, kind: ConnectionEventKind, detail: str) -> None:
        self.on_connection_event(
            ConnectionEvent(ts_ms=self._clock.now_ms(), source="recorder", kind=kind, detail=detail)
        )

    async def _sleep_until(self, ts_ms: int) -> None:
        wait_ms = ts_ms - self._clock.now_ms()
        if wait_ms > 0:
            await self._clock.sleep(wait_ms / MS_PER_SECOND)

    async def _status_loop(self) -> None:
        last_messages = self._client.messages
        while True:
            await self._clock.sleep(self._config.status_log_s)
            messages = self._client.messages
            rate = (messages - last_messages) / self._config.status_log_s
            last_messages = messages
            await self.report_status(rate)

    async def report_status(self, msgs_per_s: float) -> RecorderStatusRow:
        """Log the status line, warn about raw volume, and queue the recorder_status row."""
        now = self._clock.now_ms()
        rss, peak = process_memory()
        raw_day = ms_to_datetime(now).date()
        raw_bytes = await asyncio.to_thread(raw_bytes_for_day, self._config.raw_dir, raw_day)
        logger.info(
            "status: %d/%d connections up, %d channels, %.0f msg/s, last snapshot %d option "
            "rows, %d db jobs waiting, raw buffered %d quotes %d trades, decode errors %d, "
            "memory %s (peak %s), raw files today %.0f MB",
            self._client.connected,
            self._client.connections,
            len(self._client.channels),
            msgs_per_s,
            self.stats.last_option_rows,
            len(self._jobs),
            self.raw_quotes.buffered,
            self.raw_trades.buffered,
            self._client.decode_errors,
            _mib(rss),
            _mib(peak),
            raw_bytes / 1e6,
        )
        if raw_bytes >= self._config.raw_daily_warn_bytes and raw_day not in self._raw_warned:
            self._raw_warned.add(raw_day)
            logger.warning(
                "raw stream: %s has reached %.2f GB, above the %.2f GB daily warning level; "
                "all data is kept",
                raw_day,
                raw_bytes / 1e9,
                self._config.raw_daily_warn_bytes / 1e9,
            )
        row = RecorderStatusRow(
            ts=ms_to_datetime(now),
            run_id=self.run_id,
            started_ms=self._started_ms,
            rss_bytes=rss,
            peak_rss_bytes=peak,
            connections_up=self._client.connected,
            connections=self._client.connections,
            channels=len(self._client.channels),
            msgs_per_s=msgs_per_s,
            last_option_rows=self.stats.last_option_rows,
            db_jobs_waiting=len(self._jobs),
            decode_errors=self._client.decode_errors,
            raw_day=raw_day,
            raw_bytes_day=raw_bytes,
        )
        self._enqueue(_Job("status", lambda s: s.write_status(row)))
        return row


def _mib(nbytes: int | None) -> str:
    return "?" if nbytes is None else f"{nbytes / 2**20:.0f} MiB"


def process_memory(status_path: Path = Path("/proc/self/status")) -> tuple[int | None, int | None]:
    """This process's resident memory now and at its peak, in bytes (None if unknown).

    Read from Linux's ``VmRSS`` and ``VmHWM`` lines, which are in kB.
    """
    try:
        text = status_path.read_text(encoding="ascii")
    except OSError:
        return None, None
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("VmRSS", "VmHWM"):
            values[key] = int(rest.split()[0]) * 1024
    return values.get("VmRSS"), values.get("VmHWM")


def raw_bytes_for_day(raw_dir: Path, day: date) -> int:
    """Bytes on disk of every raw file (finished or in progress) for one UTC day."""
    total = 0
    for stream in (QUOTES, TRADES):
        folder = raw_dir / stream / day.isoformat()
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            try:
                total += path.stat().st_size
            except FileNotFoundError:  # renamed between listing and stat
                continue
    return total


def next_delivery_check_ms(now_ms: int) -> int:
    """The next UTC time (after ``now_ms``) at which to fetch delivery prices."""
    day = floor_ms(now_ms, MS_PER_DAY)
    for offset in (*DELIVERY_CHECKS_MS, *(MS_PER_DAY + c for c in DELIVERY_CHECKS_MS)):
        if day + offset > now_ms:
            return day + offset
    raise AssertionError("unreachable")  # pragma: no cover
