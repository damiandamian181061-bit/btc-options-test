"""The recorder: discovery, point-in-time snapshots, trades, retries, and a full run against the
fake server including a disconnect."""

import asyncio
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from optlab.domain.market import (
    ConnectionEventKind,
    DeliveryPrice,
    Direction,
    Greeks,
    Instrument,
    InstrumentKind,
    Ticker,
    Trade,
)
from optlab.market_data.recorder import (
    Recorder,
    RecorderConfig,
    next_delivery_check_ms,
    process_memory,
)
from optlab.market_data.sink import MarketDataSink
from optlab.market_data.universe import build_universe
from tests.market_data.fake_deribit import FakeDeribit
from tests.market_data.helpers import (
    ManualClock,
    MemoryStore,
    fixture_instruments,
    make_client,
    wait_until,
)

T = 1_794_500_040_000  # a minute boundary


class FakeClient:
    """Stands in for DeribitClient: serves fixture instruments, records subscriptions."""

    def __init__(self, sink: MarketDataSink, instruments: list[Instrument]) -> None:
        self.sink = sink
        self.instruments = instruments
        self.subscriptions: set[str] = set()
        self.messages = 0
        self.decode_errors = 0

    def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def wait_ready(self, timeout: float) -> None:
        pass

    async def get_instruments(self, currency: str, kind: str) -> list[Instrument]:
        return [
            i
            for i in self.instruments
            if i.settlement_currency == currency
            and (i.kind is InstrumentKind.OPTION) == (kind == "option")
        ]

    async def get_delivery_prices(
        self, index_name: str, currency: str, count: int = 10
    ) -> list[DeliveryPrice]:
        return [DeliveryPrice(index_name, date(2026, 11, 12), Decimal("70123.45"), currency)]

    async def set_subscriptions(self, desired: set[str]) -> object:
        self.subscriptions = set(desired)
        return None

    @property
    def connections(self) -> int:
        return 1

    @property
    def connected(self) -> int:
        return 1

    @property
    def channels(self) -> frozenset[str]:
        return frozenset(self.subscriptions)


def make_recorder(
    tmp_path: Path, clock: ManualClock, **config: object
) -> tuple[Recorder, MemoryStore, list[FakeClient]]:
    store = MemoryStore()
    clients: list[FakeClient] = []

    def factory(sink: MarketDataSink) -> FakeClient:
        clients.append(FakeClient(sink, fixture_instruments()))
        return clients[0]

    rec = Recorder(
        store,
        factory,
        RecorderConfig(data_dir=tmp_path, **config),  # type: ignore[arg-type]
        run_id="test",
        clock=clock,
    )
    return rec, store, clients


async def write_all(rec: Recorder) -> None:
    writer = asyncio.create_task(rec._writer_loop())
    await wait_until(lambda: not rec._jobs)
    writer.cancel()
    await asyncio.gather(writer, return_exceptions=True)


def tick(name: str, recv_ms: int, bid: float = 0.02) -> Ticker:
    return Ticker(
        instrument_name=name,
        exchange_ts_ms=recv_ms - 2,
        recv_ts_ms=recv_ms,
        state="open",
        bid=bid,
        bid_size=1.0,
        ask=bid + 0.001,
        ask_size=1.0,
        mark_price=bid + 0.0005,
        last_price=None,
        index_price=70_000.0,
        open_interest=1.0,
        estimated_delivery_price=70_000.0,
        mark_iv=0.4,
        greeks=Greeks(0.5, 0.0001, 40.0, -90.0, 7.0),
    )


async def test_discovery_stores_instruments_and_subscribes(tmp_path: Path) -> None:
    rec, store, clients = make_recorder(tmp_path, ManualClock(T - 30_000))
    assert await rec.discover()
    expected = build_universe(fixture_instruments())
    assert rec.universe == expected
    assert clients[0].subscriptions == expected.channels("100ms")
    await write_all(rec)
    assert set(store.instruments) == set(expected.instruments)


async def test_snapshot_holds_what_was_received_before_the_boundary(tmp_path: Path) -> None:
    rec, store, _ = make_recorder(tmp_path, ManualClock(T - 30_000))
    await rec.discover()
    rec.on_ticker(tick("BTC-20NOV26-70000-C", T - 1_000, bid=0.020))
    rec.on_ticker(tick("BTC-20NOV26-70000-C", T + 5, bid=0.025))  # crosses the boundary
    await write_all(rec)
    assert len(store.snapshots) == 1
    snap = store.snapshots[0]
    assert snap.ts_ms == T
    assert [(r.instrument_name, r.bid) for r in snap.options] == [("BTC-20NOV26-70000-C", 0.020)]
    rec.on_ticker(tick("BTC-PERPETUAL", T + 60_001, bid=70_000.0))  # next minute
    await write_all(rec)
    assert store.snapshots[1].options[0].bid == 0.025


async def test_lost_streams_are_left_out_until_data_returns(tmp_path: Path) -> None:
    rec, store, _ = make_recorder(tmp_path, ManualClock(T - 30_000))
    await rec.discover()
    rec.on_ticker(tick("BTC-20NOV26-70000-C", T - 1_000))
    rec.on_ticker(tick("BTC-20NOV26-70000-P", T - 1_000))
    rec.on_stream_lost(frozenset({"BTC-20NOV26-70000-P"}))
    rec.on_ticker(tick("BTC-PERPETUAL", T + 1))
    await write_all(rec)
    assert [r.instrument_name for r in store.snapshots[0].options] == ["BTC-20NOV26-70000-C"]


async def test_trades_for_new_listings_are_kept_until_discovery_finds_them(
    tmp_path: Path,
) -> None:
    clock = ManualClock(T - 30_000)
    rec, store, clients = make_recorder(tmp_path, clock)
    full = clients[0].instruments
    clients[0].instruments = [i for i in full if i.name != "BTC-ODDNAME"]
    await rec.discover()

    def trade(trade_id: str, name: str) -> Trade:
        return Trade(trade_id, name, T - 10, T - 5, 0.01, 1.0, Direction.SELL, iv=0.5)

    rec.on_trades(
        [
            trade("1", "BTC-20NOV26-70000-C"),
            trade("2", "BTC-ODDNAME"),  # listed after the last discovery
            trade("3", "ETH_USDC-20NOV26-2000-C"),  # never recorded
            trade("4", "BTC-PERPETUAL"),  # not an option
        ]
    )
    rec._flush_trades()
    await write_all(rec)
    assert [r.trade_id for r in store.trades] == ["1"]
    clients[0].instruments = full
    await rec.discover()
    rec._flush_trades()
    await write_all(rec)
    assert [r.trade_id for r in store.trades] == ["1", "2"]
    assert store.trades[1].currency == "BTC"
    clock.advance(16 * 60)
    await rec.discover()
    assert rec._unknown_trades == []  # the ETH trade is forgotten after 15 minutes


async def test_a_discovery_losing_most_options_is_refused_twice(tmp_path: Path) -> None:
    rec, _, clients = make_recorder(tmp_path, ManualClock(T - 30_000))
    await rec.discover()
    full = rec.universe
    clients[0].instruments = [i for i in clients[0].instruments if i.name != "BTC-ODDNAME"]
    clients[0].instruments = [
        i for i in clients[0].instruments if i.kind is not InstrumentKind.OPTION
    ] + [next(i for i in clients[0].instruments if i.kind is InstrumentKind.OPTION)]
    assert not await rec.discover()
    assert not await rec.discover()
    assert rec.universe == full
    assert await rec.discover()  # a third identical answer is believed
    assert len(rec.universe.options) == 1


async def test_writes_are_retried_after_database_errors(tmp_path: Path) -> None:
    rec, store, _ = make_recorder(tmp_path, ManualClock(T - 30_000))
    await rec.discover()
    store.fail_next = 2
    await write_all(rec)
    assert store.resets == 2
    assert len(store.instruments) == 9
    assert rec.stats.failed_writes == 2


async def test_backlog_is_capped_by_dropping_the_oldest(tmp_path: Path) -> None:
    rec, _, _ = make_recorder(tmp_path, ManualClock(T - 30_000), max_pending_jobs=3)
    for i in range(5):
        rec._event(ConnectionEventKind.STARTED, f"event {i}")
    assert len(rec._jobs) == 3
    assert rec.stats.dropped_jobs == 2


def test_delivery_checks_follow_the_0800_expiry() -> None:
    day = 1_794_441_600_000  # 2026-11-12 00:00 UTC
    hour8 = day + 8 * 3_600_000
    assert next_delivery_check_ms(day) == hour8 + 60_000
    assert next_delivery_check_ms(hour8 + 60_000) == hour8 + 5 * 60_000
    assert next_delivery_check_ms(hour8 + 30 * 60_000) == hour8 + 86_400_000 + 60_000


async def test_full_run_against_the_fake_server(tmp_path: Path) -> None:
    store = MemoryStore()
    async with FakeDeribit() as server:

        def factory(sink: MarketDataSink):
            return make_client(sink, server.url, max_channels_per_connection=6)

        config = RecorderConfig(
            data_dir=tmp_path, snapshot_interval_ms=200, trade_flush_s=0.05, raw_flush_check_s=0.05
        )
        rec = Recorder(store, factory, config, run_id="e2e")
        task = asyncio.create_task(rec.run())
        try:
            await wait_until(lambda: len(store.snapshots) >= 2 and len(store.trades) == 2)
            await wait_until(lambda: len(store.delivery) == 6)  # 3 days x 2 indexes
            # 13 channels at most 6 per connection: 3 connections. Cut them all.
            assert server.accepted_connections == 3
            await server.drop_all()
            await wait_until(
                lambda: [e.kind.value for e in store.events].count("disconnected") == 3
            )
            await wait_until(lambda: server.accepted_connections == 6)
            snapshots_before = len(store.snapshots)
            await wait_until(lambda: len(store.snapshots) >= snapshots_before + 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    snap = store.snapshots[-1]
    assert {r.instrument_name for r in snap.options} == {
        "BTC-20NOV26-70000-C",
        "BTC_USDC-20NOV26-70000-C",
    }
    assert {r.instrument_name for r in snap.underlyings} == {
        "BTC-PERPETUAL",
        "BTC_USDC-PERPETUAL",
        "BTC-27NOV26",
        "btc_usd",
        "btc_usdc",
    }
    linear = next(r for r in snap.options if r.instrument_name == "BTC_USDC-20NOV26-70000-C")
    assert (linear.currency, linear.bid, linear.ask, linear.mark_iv) == (
        "USDC",
        None,
        2000.0,
        0.4125,
    )
    assert {(t.trade_id, t.currency) for t in store.trades} == {
        ("400000001", "BTC"),
        ("USDC-100000001", "USDC"),
    }
    assert len(store.instruments) == 9
    kinds = [(e.source, e.kind.value) for e in store.events]
    assert kinds[0] == ("recorder", "started")
    assert kinds[-1] == ("recorder", "stopped")
    assert ("deribit-ws-0", "disconnected") in kinds
    assert ("deribit-ws-1", "reconnect_scheduled") in kinds
    quotes = list((tmp_path / "raw" / "quotes").rglob("*.parquet"))
    trades = list((tmp_path / "raw" / "trades").rglob("*.parquet"))
    assert len(quotes) >= 1
    assert len(trades) >= 1
    assert not list(tmp_path.rglob("*.inprogress"))  # closed cleanly on shutdown
    names = set(pq.read_table(quotes[0]).column("instrument_name").to_pylist())
    assert "BTC-PERPETUAL" in names
    assert sum(pq.read_table(p).num_rows for p in trades) == 2


async def test_status_row_carries_memory_and_raw_bytes_for_the_day(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = ManualClock(T)
    rec, store, _ = make_recorder(tmp_path, clock, raw_daily_warn_bytes=1_000)
    day = "2026-11-12"  # T is 2026-11-12 in UTC
    for stream, size in (("quotes", 700), ("trades", 200)):
        folder = tmp_path / "raw" / stream / day
        folder.mkdir(parents=True)
        (folder / f"{stream}_{day}T10_x.parquet").write_bytes(b"x" * size)
    (tmp_path / "raw" / "quotes" / "2026-11-11").mkdir()
    (tmp_path / "raw" / "quotes" / "2026-11-11" / "old.parquet").write_bytes(b"x" * 5_000)

    with caplog.at_level(logging.WARNING):
        row = await rec.report_status(12.0)
    assert row.raw_day == date(2026, 11, 12)
    assert row.raw_bytes_day == 900  # only today's files count
    assert row.run_id == "test"
    assert row.started_ms == T
    assert row.msgs_per_s == 12.0
    assert row.rss_bytes is None or row.rss_bytes > 1_000_000
    assert not [r for r in caplog.records if "warning level" in r.getMessage()]

    (tmp_path / "raw" / "trades" / day / "more.parquet.inprogress").write_bytes(b"x" * 200)
    with caplog.at_level(logging.WARNING):
        row = await rec.report_status(12.0)
        await rec.report_status(12.0)  # warned once per day, not every minute
    assert row.raw_bytes_day == 1_100
    warnings = [r for r in caplog.records if "warning level" in r.getMessage()]
    assert len(warnings) == 1
    assert "all data is kept" in warnings[0].getMessage()
    await write_all(rec)
    assert [s.raw_bytes_day for s in store.status] == [900, 1_100, 1_100]


def test_process_memory_reads_rss_and_peak(tmp_path: Path) -> None:
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmHWM:\t  425000 kB\nVmRSS:\t  387000 kB\n", encoding="ascii")
    assert process_memory(status) == (387_000 * 1024, 425_000 * 1024)
    assert process_memory(tmp_path / "missing") == (None, None)
