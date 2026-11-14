"""A small made-up market written straight into a test database.

Two expiries, both books, puts and calls, a dated future, both indexes, a trade every few
minutes, delivery prices and connection events. Prices follow simple deterministic formulas,
so tests can change rows and know what to expect. Values are invented: no vendor data.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import sql

from optlab.domain.clock import (
    MS_PER_DAY,
    MS_PER_HOUR,
    MS_PER_MINUTE,
    datetime_to_ms,
    ms_to_datetime,
)
from optlab.market_data.rows import OptionQuoteRow, OptionTradeRow, UnderlyingTickRow


def utc_ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return datetime_to_ms(datetime(year, month, day, hour, minute, tzinfo=UTC))


EXPIRY_NEAR = utc_ms(2026, 11, 20, 8)
EXPIRY_FAR = utc_ms(2026, 11, 27, 8)
LISTED = utc_ms(2026, 11, 1)


@dataclass(frozen=True)
class Option:
    name: str
    book: str  # linear or inverse
    option_type: str
    strike: float
    expiry_ms: int

    @property
    def currency(self) -> str:
        return "USDC" if self.book == "linear" else "BTC"


def _options() -> list[Option]:
    out: list[Option] = []
    for expiry, label in ((EXPIRY_NEAR, "20NOV26"), (EXPIRY_FAR, "27NOV26")):
        for strike in (86_000, 88_000, 90_000, 92_000, 94_000):
            for kind, letter in (("put", "P"), ("call", "C")):
                out.append(
                    Option(f"BTC-{label}-{strike}-{letter}", "inverse", kind, strike, expiry)
                )
                out.append(
                    Option(f"BTC_USDC-{label}-{strike}-{letter}", "linear", kind, strike, expiry)
                )
    return out


OPTIONS = _options()
FUTURE = "BTC_USDC-27NOV26"
INDEXES = ("btc_usd", "btc_usdc")


def spot(ts_ms: int) -> float:
    """A made-up BTC price that moves every minute."""
    return 90_000.0 + ((ts_ms // MS_PER_MINUTE) % 97) * 7.5


def instrument_rows() -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for i, o in enumerate(OPTIONS):
        linear = o.book == "linear"
        rows.append(
            (
                o.name,
                1000 + i,
                "option",
                o.book,
                "BTC",
                o.currency,
                o.currency,
                "USDC" if linear else "USD",
                "btc_usdc" if linear else "btc_usd",
                o.option_type,
                float(o.strike),
                o.expiry_ms,
                0.01 if linear else 1.0,
                5.0 if linear else 0.0001,
                0.01 if linear else 0.1,
                "week",
                LISTED,
                LISTED,
                LISTED,
            )
        )
    rows.append(
        (
            FUTURE, 999, "future", "linear", "BTC", "USDC", "USDC", "USDC", "btc_usdc",
            None, None, EXPIRY_FAR, 0.001, 2.5, 0.001, "week", LISTED, LISTED, LISTED,
        )
    )  # fmt: skip
    return rows


def _delta(o: Option, s: float) -> float:
    d = 0.5 + (s - o.strike) / 20_000
    d = min(0.98, max(0.02, d))
    return d if o.option_type == "call" else d - 1.0


def quote_row(ts_ms: int, o: Option) -> OptionQuoteRow:
    s = spot(ts_ms)
    intrinsic = max(0.0, (s - o.strike) if o.option_type == "call" else (o.strike - s))
    value_usd = intrinsic + 900.0 + ((ts_ms // MS_PER_MINUTE) % 5) * 3
    spread = 0.04 if o.book == "linear" else 0.06
    mid = value_usd if o.book == "linear" else value_usd / s
    tick = 5.0 if o.book == "linear" else 0.0001
    bid = round(mid * (1 - spread / 2) / tick) * tick
    ask = round(mid * (1 + spread / 2) / tick) * tick
    return OptionQuoteRow(
        ts=ms_to_datetime(ts_ms),
        instrument_name=o.name,
        currency=o.currency,
        bid=bid,
        bid_size=1.5 if o.book == "inverse" else 0.4,
        ask=ask,
        ask_size=2.0 if o.book == "inverse" else 0.3,
        mark_price=mid,
        mark_iv=0.45 + (o.strike - 90_000) / 1_000_000,
        bid_iv=0.44,
        ask_iv=0.46,
        delta=_delta(o, s),
        gamma=0.00001,
        vega=40.0,
        theta=-90.0,
        rho=1.0,
        open_interest=10.0,
        underlying_price=s + 50.0,
        underlying_index=FUTURE if o.book == "linear" else "SYN.BTC-27NOV26",
        index_price=s,
        interest_rate=0.0,
        last_price=None,
        state="open",
        exchange_ts_ms=ts_ms - 400,
        recv_ts_ms=ts_ms - 350,
    )


def underlying_rows(ts_ms: int) -> list[UnderlyingTickRow]:
    s = spot(ts_ms)
    rows = [
        UnderlyingTickRow(
            ms_to_datetime(ts_ms), name, "index", "USD" if name == "btc_usd" else "USDC",
            None, None, None, None, None, None, s, None, None, None, None, None,
            ts_ms - 500, ts_ms - 450,
        )
        for name in INDEXES
    ]  # fmt: skip
    rows.append(
        UnderlyingTickRow(
            ms_to_datetime(ts_ms), FUTURE, "future", "USDC", s + 45, 1.2, s + 55, 0.8, s + 50,
            s + 50, s, 100.0, None, None, None, s, ts_ms - 300, ts_ms - 250,
        )
    )  # fmt: skip
    return rows


def trade_row(ts_ms: int, o: Option, seq: int) -> OptionTradeRow:
    q = quote_row(ts_ms, o)
    return OptionTradeRow(
        trade_id=f"T{seq}",
        ts=ms_to_datetime(ts_ms + 1234),
        instrument_name=o.name,
        currency=o.currency,
        price=q.ask or 0.0,
        amount=0.1 * (1 + seq % 3),
        direction="buy" if seq % 2 else "sell",
        iv=0.45,
        mark_price=q.mark_price,
        index_price=q.index_price,
        trade_seq=seq,
        tick_direction=seq % 4,
        block_trade_id=None,
        liquidation=None,
        combo_id=None,
        exchange_ts_ms=ts_ms + 1234,
        recv_ts_ms=ts_ms + 1300,
    )


def copy_rows(
    conn: psycopg.Connection[Any], table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]
) -> None:
    cols = sql.SQL(", ").join(map(sql.Identifier, columns))
    with (
        conn.cursor() as cur,
        cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(table), cols)) as copy,
    ):
        for row in rows:
            copy.write_row(row)


INSTRUMENT_COLUMNS = (
    "instrument_name", "exchange_instrument_id", "kind", "book", "base_currency",
    "quote_currency", "settlement_currency", "counter_currency", "price_index", "option_type",
    "strike", "expiry_ms", "contract_size", "tick_size", "min_trade_amount", "settlement_period",
    "creation_ms", "first_seen_ms", "last_seen_ms",
)  # fmt: skip


def populate(database_url: str, start_ms: int, end_ms: int) -> None:
    """Write one snapshot per minute in [start_ms, end_ms), plus trades and reference rows."""
    with psycopg.connect(database_url) as conn:
        copy_rows(conn, "instruments", INSTRUMENT_COLUMNS, instrument_rows())
        minutes = range(start_ms, end_ms, MS_PER_MINUTE)
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
        copy_rows(
            conn,
            "option_trades",
            OptionTradeRow._fields,
            (
                trade_row(m, OPTIONS[(m // MS_PER_MINUTE) % len(OPTIONS)], m // MS_PER_MINUTE)
                for m in minutes
                if (m // MS_PER_MINUTE) % 7 == 0
            ),
        )
        day = start_ms - start_ms % MS_PER_DAY
        for d in range(day, end_ms, MS_PER_DAY):
            known = d + 8 * MS_PER_HOUR + 60_000  # fetched at 08:01
            day_ = ms_to_datetime(d).date()
            conn.execute(
                "INSERT INTO delivery_prices VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)",
                ("btc_usd", day_, "90123.4567", "USD", known,
                 "btc_usdc", day_, "90120.10", "USDC", known),
            )  # fmt: skip
        conn.execute(
            "INSERT INTO connection_events (ts, ts_ms, source, event, detail) VALUES "
            "(%s, %s, 'recorder', 'started', 'run test'), "
            "(%s, %s, 'deribit-ws-1', 'disconnected', 'connection closed without a close frame')",
            (
                ms_to_datetime(start_ms),
                start_ms,
                ms_to_datetime(start_ms + 90_000),
                start_ms + 90_000,
            ),
        )
