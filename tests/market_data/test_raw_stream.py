"""Hourly zstd Parquet files: one per hour, renamed when complete, readable, crash-safe names."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from optlab.domain.clock import MS_PER_HOUR, datetime_to_ms
from optlab.domain.market import Direction, Ticker, Trade
from optlab.market_data.raw_stream import (
    INCOMPLETE,
    INPROGRESS,
    QUOTE_SCHEMA,
    TRADE_SCHEMA,
    BboChangeFilter,
    HourlyParquetStream,
    quarantine_unfinished,
    quote_record,
    trade_record,
)

HOUR = datetime_to_ms(datetime(2026, 11, 12, 18, tzinfo=UTC))


def tick(recv_ms: int, bid: float | None = 0.02, ask: float = 0.03, name: str = "A") -> Ticker:
    return Ticker(
        instrument_name=name,
        exchange_ts_ms=recv_ms - 3,
        recv_ts_ms=recv_ms,
        state="open",
        bid=bid,
        bid_size=0.0 if bid is None else 1.5,
        ask=ask,
        ask_size=2.5,
        mark_price=0.025,
        last_price=None,
        index_price=None,
        open_interest=None,
        estimated_delivery_price=None,
    )


def test_bbo_filter_passes_only_changes() -> None:
    f = BboChangeFilter()
    t = tick(HOUR)
    assert f.changed(t)
    assert not f.changed(replace(t, recv_ts_ms=HOUR + 100, mark_price=0.026))  # mark only
    assert f.changed(replace(t, ask_size=3.0))
    assert f.changed(tick(HOUR, name="B"))  # another instrument
    f.forget({"A"})
    assert f.changed(replace(t, ask_size=3.0))  # first after a stream loss is always written


def test_rows_go_to_one_file_per_hour_and_files_close_when_the_hour_ends(tmp_path: Path) -> None:
    stream = HourlyParquetStream(tmp_path, "quotes", QUOTE_SCHEMA, "run1")
    for i in range(5):
        stream.append(quote_record(tick(HOUR + i * 1000)))
    stream.append(quote_record(tick(HOUR + MS_PER_HOUR + 5, bid=None)))
    stream.flush(HOUR + MS_PER_HOUR + 10)  # hour 18 is over: its file closes; 19 stays open
    day = tmp_path / "quotes" / "2026-11-12"
    done = day / "quotes_2026-11-12T18_run1.parquet"
    assert done.exists()
    assert (day / ("quotes_2026-11-12T19_run1.parquet" + INPROGRESS)).exists()
    table = pq.read_table(done)
    assert table.num_rows == 5
    assert table.schema.equals(QUOTE_SCHEMA)
    assert pq.ParquetFile(done).metadata.row_group(0).column(0).compression == "ZSTD"
    stream.flush(HOUR + MS_PER_HOUR + 20, close_all=True)
    last = pq.read_table(day / "quotes_2026-11-12T19_run1.parquet").to_pylist()
    assert last[0]["bid"] is None
    assert last[0]["bid_size"] == 0.0
    assert stream.rows_written == 6
    assert stream.files_closed == 2


def test_several_flushes_append_row_groups_to_the_same_hour(tmp_path: Path) -> None:
    stream = HourlyParquetStream(tmp_path, "quotes", QUOTE_SCHEMA, "run1")
    for batch in range(3):
        for i in range(4):
            stream.append(quote_record(tick(HOUR + batch * 60_000 + i)))
        stream.flush(HOUR + batch * 60_000 + 10)
    stream.flush(HOUR + MS_PER_HOUR, close_all=False)
    path = tmp_path / "quotes" / "2026-11-12" / "quotes_2026-11-12T18_run1.parquet"
    assert pq.ParquetFile(path).metadata.num_row_groups == 3
    assert pq.read_table(path).num_rows == 12


def test_trades_file_round_trips(tmp_path: Path) -> None:
    stream = HourlyParquetStream(tmp_path, "trades", TRADE_SCHEMA, "r")
    trade = Trade(
        trade_id="USDC-1",
        instrument_name="BTC_USDC-20NOV26-70000-C",
        exchange_ts_ms=HOUR + 1,
        recv_ts_ms=HOUR + 2,
        price=2000.0,
        amount=0.05,
        direction=Direction.BUY,
        trade_seq=3,
        iv=0.43,
        tick_direction=0,
    )
    stream.append(trade_record(trade))
    stream.flush(HOUR + 3, close_all=True)
    rows = pq.read_table(next((tmp_path / "trades").rglob("*.parquet"))).to_pylist()
    assert rows == [
        {
            "recv_ts_ms": HOUR + 2,
            "exchange_ts_ms": HOUR + 1,
            "instrument_name": "BTC_USDC-20NOV26-70000-C",
            "trade_id": "USDC-1",
            "trade_seq": 3,
            "price": 2000.0,
            "amount": 0.05,
            "direction": "buy",
            "iv": 0.43,
            "mark_price": None,
            "index_price": None,
            "tick_direction": 0,
            "block_trade_id": None,
            "liquidation": None,
            "combo_id": None,
        }
    ]


def test_flush_is_due_when_buffer_is_full_old_or_an_hour_ended(tmp_path: Path) -> None:
    stream = HourlyParquetStream(
        tmp_path, "quotes", QUOTE_SCHEMA, "r", max_buffer_rows=3, max_buffer_age_ms=60_000
    )
    assert not stream.due(HOUR)
    stream.append(quote_record(tick(HOUR)))
    assert not stream.due(HOUR + 1_000)
    assert stream.due(HOUR + 60_000)  # old
    assert stream.due(HOUR + MS_PER_HOUR)  # hour ended
    stream.append(quote_record(tick(HOUR + 1)))
    stream.append(quote_record(tick(HOUR + 2)))
    assert stream.due(HOUR + 3)  # full


def test_files_left_unfinished_by_a_crash_are_renamed(tmp_path: Path) -> None:
    stream = HourlyParquetStream(tmp_path, "quotes", QUOTE_SCHEMA, "crashed")
    stream.append(quote_record(tick(HOUR)))
    stream.flush(HOUR + 1)  # written but never closed: no Parquet footer
    moved = quarantine_unfinished(tmp_path)
    assert [p.name for p in moved] == [f"quotes_2026-11-12T18_crashed.parquet{INCOMPLETE}"]
    assert not list(tmp_path.rglob(f"*{INPROGRESS}"))
    assert quarantine_unfinished(tmp_path / "missing") == []
