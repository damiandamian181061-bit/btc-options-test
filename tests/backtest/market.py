"""A small made-up BTC_USDC market in memory, served through the MarketDataSource protocol.

One Friday expiry (20 Nov 2026 08:00 UTC) with five strikes of calls and puts, its dated future,
the BTC_USDC perpetual and the btc_usdc index, one row per instrument per minute, and the
delivery price recorded at 08:01. Prices follow simple formulas so tests know what to expect.
Values are invented: no vendor data.
"""

import hashlib
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from optlab.domain.clock import MS_PER_DAY, MS_PER_MINUTE, ms_to_datetime
from optlab.market_data.view import INSTRUMENT_COLUMNS, QUALITY_COLUMNS
from tests.market_data.synthetic import utc_ms

EXPIRY = utc_ms(2026, 11, 20, 8)
LISTED = utc_ms(2026, 11, 1)
STRIKES = (86_000, 88_000, 90_000, 92_000, 94_000)
FUTURE = "BTC_USDC-20NOV26"
PERP = "BTC_USDC-PERPETUAL"
START = utc_ms(2026, 11, 19, 0)
END = utc_ms(2026, 11, 20, 9)


def option_name(strike: int, kind: str) -> str:
    return f"BTC_USDC-20NOV26-{strike}-{kind[0].upper()}"


def default_spot(ts_ms: int) -> float:
    """A made-up index that wanders between about 89,600 and 90,400."""
    m = (ts_ms - START) // MS_PER_MINUTE
    return 90_000.0 + ((m * 37) % 161 - 80) * 5.0


@dataclass
class FakeMarket:
    spot: Callable[[int], float] = default_spot
    start: int = START
    end: int = END
    step_minutes: int = 1
    size: float = 5.0  # top-of-book size on every option, both sides
    half_spread: float = 10.0
    perp_premium: float = 0.0005  # (perp mark - index) / index
    delivery_recorded: int = EXPIRY + MS_PER_MINUTE  # 08:01
    skips: list[tuple[int, int]] = field(default_factory=list)
    edit: Callable[[str, dict[str, Any]], dict[str, Any] | None] | None = None  # change rows

    def times(self) -> list[int]:
        return list(range(self.start, self.end, self.step_minutes * MS_PER_MINUTE))

    def mark(self, ts_ms: int, strike: int, kind: str) -> float:
        s = self.spot(ts_ms)
        intrinsic = max(0.0, s - strike if kind == "call" else strike - s)
        days = max(0.0, (EXPIRY - ts_ms) / MS_PER_DAY)
        return round(intrinsic + 600.0 * math.sqrt(days), 1)

    def build(self) -> "MemorySource":
        quotes: list[dict[str, Any]] = []
        unders: list[dict[str, Any]] = []
        for t in self.times():
            s = self.spot(t)
            if t < EXPIRY:
                for k in STRIKES:
                    for kind in ("call", "put"):
                        mark = self.mark(t, k, kind)
                        bid = round(mark - self.half_spread, 1)
                        delta = 0.5 + (s - k) / 8_000
                        delta = min(0.99, max(0.01, delta))
                        quotes.append({
                            "ts_ms": t, "instrument_name": option_name(k, kind),
                            "currency": "USDC", "bid": bid if bid > 0 else None,
                            "bid_size": self.size if bid > 0 else 0.0,
                            "ask": round(mark + self.half_spread, 1), "ask_size": self.size,
                            "mark_price": mark, "mark_iv": 0.45, "bid_iv": 0.44, "ask_iv": 0.46,
                            "delta": delta if kind == "call" else delta - 1.0,
                            "gamma": 0.0001, "vega": 30.0, "theta": -80.0, "rho": 0.0,
                            "open_interest": 10.0, "underlying_price": s + 20.0,
                            "underlying_index": FUTURE, "index_price": s, "interest_rate": 0.0,
                            "last_price": None, "state": "open",
                            "exchange_ts_ms": t - 300, "recv_ts_ms": t - 200,
                        })  # fmt: skip
            perp_mark = s * (1 + self.perp_premium)
            under_rows: tuple[tuple[str, str, float | None], ...] = (
                ("btc_usdc", "index", None),
                (FUTURE, "future", s + 20.0),
                (PERP, "perpetual", perp_mark),
            )
            for name, kind, umark in under_rows:
                unders.append({
                    "ts_ms": t, "instrument_name": name, "kind": kind, "currency": "USDC",
                    "bid": None if umark is None else umark - 5.0,
                    "bid_size": None if umark is None else 3.0,
                    "ask": None if umark is None else umark + 5.0,
                    "ask_size": None if umark is None else 3.0,
                    "mark_price": umark, "last_price": None, "index_price": s,
                    "open_interest": None, "current_funding": None, "funding_8h": None,
                    "interest_value": None, "estimated_delivery_price": None,
                    "exchange_ts_ms": t - 300, "recv_ts_ms": t - 200,
                })  # fmt: skip
        if self.edit is not None:
            quotes = [r for r in (self.edit("option_quotes", q) for q in quotes) if r]
            unders = [r for r in (self.edit("underlying_ticks", u) for u in unders) if r]
        instruments = []
        for i, k in enumerate(STRIKES):
            for kind in ("call", "put"):
                instruments.append(_instrument(option_name(k, kind), 100 + 2 * i, "option",
                                               kind, float(k), EXPIRY, 0.01))  # fmt: skip
        instruments.append(_instrument(FUTURE, 90, "future", None, None, EXPIRY, 0.001))
        instruments.append(_instrument(PERP, 91, "perpetual", None, None, None, 0.001))
        delivery = round(self.spot(EXPIRY), 2)
        prices = [{
            "index_name": "btc_usdc", "delivery_date": ms_to_datetime(EXPIRY).date(),
            "delivery_price": f"{delivery:.2f}", "currency": "USDC",
            "recorded_ms": self.delivery_recorded,
        }]  # fmt: skip
        quality = [
            {"check_name": "partial_snapshot", "scope": "global", "subject": "",
             "start_ts_ms": s, "end_ts_ms": e, "minutes": (e - s) // MS_PER_MINUTE,
             "skip": True, "detail": "test"}
            for s, e in self.skips
        ]  # fmt: skip
        return MemorySource(
            quotes=pa.Table.from_pylist(quotes),
            unders=pa.Table.from_pylist(unders),
            instruments=pa.Table.from_pylist(instruments).select(list(INSTRUMENT_COLUMNS)),
            prices=pa.Table.from_pylist(prices),
            quality=pa.Table.from_pylist(quality, schema=_QUALITY_SCHEMA),
        )


_QUALITY_SCHEMA = pa.schema([
    ("check_name", pa.string()), ("scope", pa.string()), ("subject", pa.string()),
    ("start_ts_ms", pa.int64()), ("end_ts_ms", pa.int64()), ("minutes", pa.int32()),
    ("skip", pa.bool_()), ("detail", pa.string()),
])  # fmt: skip
assert tuple(_QUALITY_SCHEMA.names) == QUALITY_COLUMNS


def _instrument(
    name: str,
    exchange_id: int,
    kind: str,
    option_type: str | None,
    strike: float | None,
    expiry_ms: int | None,
    min_amount: float,
) -> dict[str, Any]:
    return {
        "instrument_name": name, "exchange_instrument_id": exchange_id, "kind": kind,
        "book": "linear", "base_currency": "BTC", "quote_currency": "USDC",
        "settlement_currency": "USDC", "counter_currency": "USDC", "price_index": "btc_usdc",
        "option_type": option_type, "strike": strike, "expiry_ms": expiry_ms,
        "contract_size": 1.0 if kind == "option" else 0.001, "tick_size": 5.0,
        "min_trade_amount": min_amount, "settlement_period": "week",
        "creation_ms": LISTED, "first_seen_ms": LISTED,
    }  # fmt: skip


def _between(table: pa.Table, column: str, start: int, end: int) -> pa.Table:
    col = table.column(column)
    return table.filter(pc.and_(pc.greater_equal(col, start), pc.less_equal(col, end)))


class MemorySource:
    """The MarketDataSource protocol over in-memory tables (the view caps every read at t)."""

    def __init__(
        self,
        *,
        quotes: pa.Table,
        unders: pa.Table,
        instruments: pa.Table,
        prices: pa.Table,
        quality: pa.Table,
    ) -> None:
        self._quotes = quotes
        self._unders = unders
        self._instruments = instruments
        self._prices = prices
        self._quality = quality

    def snapshot_times(self, start_ms: int, end_ms: int) -> list[int]:
        ts = _between(self._quotes, "ts_ms", start_ms, end_ms).column("ts_ms")
        under = _between(self._unders, "ts_ms", start_ms, end_ms).column("ts_ms")
        return sorted(set(ts.to_pylist()) | set(under.to_pylist()))

    def option_quotes(self, start_ms: int, end_ms: int) -> pa.Table:
        return _between(self._quotes, "ts_ms", start_ms, end_ms)

    def underlying_ticks(self, start_ms: int, end_ms: int) -> pa.Table:
        return _between(self._unders, "ts_ms", start_ms, end_ms)

    def option_trades(self, start_ms: int, end_ms: int) -> pa.Table:
        return pa.table({"recv_ts_ms": pa.array([], pa.int64())})

    def instruments(self, as_of_ms: int) -> pa.Table:
        table = self._instruments
        return table.filter(pc.less_equal(table.column("first_seen_ms"), as_of_ms))

    def delivery_prices(self, as_of_ms: int) -> pa.Table:
        return self._prices.filter(pc.less_equal(self._prices.column("recorded_ms"), as_of_ms))

    def quality(self, start_ms: int, end_ms: int) -> pa.Table:
        return _between(self._quality, "start_ts_ms", start_ms, end_ms)

    def skip_periods(self, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
        rows = self._quality.filter(self._quality.column("skip")).to_pylist()
        return [(r["start_ts_ms"], r["end_ts_ms"]) for r in rows
                if r["end_ts_ms"] > start_ms and r["start_ts_ms"] <= end_ms]  # fmt: skip

    def dataset_versions(self, first: date, last: date) -> dict[date, str]:
        h = hashlib.sha256()
        for t in (self._quotes, self._unders, self._instruments, self._prices, self._quality):
            h.update(repr(t.to_pylist()).encode())
        return {first: h.hexdigest()}


def days(ts: Iterable[int]) -> set[date]:
    return {ms_to_datetime(t).date() for t in ts}
