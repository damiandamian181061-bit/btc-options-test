"""``MarketState``: what the backtest knows at one snapshot minute, read through MarketDataView.

Everything comes from ``MarketDataView(source, as_of=t)``, so nothing after t is visible
(CLAUDE.md). Prices become exact Decimals (``optlab.domain.money.dec``); Greeks and IVs stay
floats, in Deribit's units. Only the main book's options (BTC_USDC, quoted in USDC) are loaded;
underlyings (indexes, futures, perpetuals) are all loaded.

Quote objects are built on first use: a strategy that looks at a few options doesn't pay for
the whole chain.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from optlab.domain.clock import ms_to_datetime
from optlab.domain.money import dec
from optlab.market_data.view import MarketDataView

INDEX = "btc_usdc"
OPTION_CURRENCY = "USDC"
_OPTION_COLUMNS = (
    "instrument_name", "bid", "bid_size", "ask", "ask_size", "mark_price", "mark_iv", "delta",
    "gamma", "vega", "theta", "underlying_price", "underlying_index", "index_price",
)  # fmt: skip
_UNDERLYING_COLUMNS = (
    "instrument_name", "kind", "bid", "bid_size", "ask", "ask_size", "mark_price",
    "index_price", "estimated_delivery_price",
)  # fmt: skip


def _d(value: float | None) -> Decimal | None:
    return None if value is None else dec(value)


@dataclass(frozen=True, slots=True)
class InstrumentInfo:
    name: str
    kind: str  # option, future or perpetual
    book: str
    price_index: str
    min_trade_amount: Decimal
    option_type: str | None = None
    strike: Decimal | None = None
    expiry_ms: int | None = None

    @property
    def expiry_date(self) -> date | None:
        return None if self.expiry_ms is None else ms_to_datetime(self.expiry_ms).date()


@dataclass(frozen=True, slots=True)
class Quote:
    """One instrument at the snapshot minute. A missing side is None, never 0."""

    name: str
    bid: Decimal | None
    bid_size: Decimal
    ask: Decimal | None
    ask_size: Decimal
    mark: Decimal | None
    index_price: Decimal | None
    # Options (Deribit's values as recorded):
    mark_iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None  # USD per vol point, per BTC
    theta: float | None = None
    underlying_price: Decimal | None = None  # the expiry's future mark (the forward)
    underlying_index: str | None = None
    # Futures: Deribit's running estimate of the delivery price.
    estimated_delivery_price: Decimal | None = None

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


class _Lazy(Mapping[str, Quote]):
    """Rows of one snapshot by instrument name, turned into Quotes on first access."""

    def __init__(self, table: pa.Table, columns: tuple[str, ...], option: bool) -> None:
        self._cols: dict[str, list[Any]] = {
            c: table.column(c).to_pylist() for c in columns if c in table.column_names
        }
        names = self._cols["instrument_name"]
        self._index = {n: i for i, n in enumerate(names)}
        self._option = option
        self._built: dict[str, Quote] = {}

    def __getitem__(self, name: str) -> Quote:
        quote = self._built.get(name)
        if quote is None:
            i = self._index[name]
            c = self._cols
            if self._option:
                quote = Quote(
                    name=name,
                    bid=_d(c["bid"][i]),
                    bid_size=dec(c["bid_size"][i] or 0.0),
                    ask=_d(c["ask"][i]),
                    ask_size=dec(c["ask_size"][i] or 0.0),
                    mark=_d(c["mark_price"][i]),
                    index_price=_d(c["index_price"][i]),
                    mark_iv=c["mark_iv"][i],
                    delta=c["delta"][i],
                    gamma=c["gamma"][i],
                    vega=c["vega"][i],
                    theta=c["theta"][i],
                    underlying_price=_d(c["underlying_price"][i]),
                    underlying_index=c["underlying_index"][i],
                )
            else:
                quote = Quote(
                    name=name,
                    bid=_d(c["bid"][i]),
                    bid_size=dec(c["bid_size"][i] or 0.0),
                    ask=_d(c["ask"][i]),
                    ask_size=dec(c["ask_size"][i] or 0.0),
                    mark=_d(c["mark_price"][i]),
                    index_price=_d(c["index_price"][i]),
                    estimated_delivery_price=_d(c["estimated_delivery_price"][i]),
                )
            self._built[name] = quote
        return quote

    def __iter__(self) -> Iterator[str]:
        return iter(self._index)

    def __len__(self) -> int:
        return len(self._index)


class InstrumentCache:
    """Instruments known at t, rebuilt only when the listing changes (it only grows)."""

    def __init__(self) -> None:
        self._key: tuple[int, int] | None = None
        self._infos: dict[str, InstrumentInfo] = {}

    def get(self, table: pa.Table) -> dict[str, InstrumentInfo]:
        first_seen = table.column("first_seen_ms")
        key = (table.num_rows, int(pc.max(first_seen).as_py() or 0) if table.num_rows else 0)
        if key != self._key:
            infos: dict[str, InstrumentInfo] = {}
            for row in table.to_pylist():
                strike = row["strike"]
                infos[row["instrument_name"]] = InstrumentInfo(
                    name=row["instrument_name"],
                    kind=row["kind"],
                    book=row["book"],
                    price_index=row["price_index"],
                    min_trade_amount=dec(row["min_trade_amount"]),
                    option_type=row["option_type"],
                    strike=None if strike is None else dec(strike),
                    expiry_ms=row["expiry_ms"],
                )
            self._infos = infos
            self._key = key
        return self._infos


@dataclass(frozen=True)
class MarketState:
    ts_ms: int
    index: Decimal | None  # btc_usdc
    options: Mapping[str, Quote]  # BTC_USDC options of the snapshot
    underlyings: Mapping[str, Quote]  # indexes, futures and perpetuals
    instruments: Mapping[str, InstrumentInfo]
    delivery_prices: Mapping[tuple[str, date], Decimal]  # known at t
    view: MarketDataView  # for strategies that need history (always capped at t)
    flagged: bool = False  # a minute the data checks marked to skip

    def quote(self, name: str) -> Quote | None:
        if name in self.options:
            return self.options[name]
        return self.underlyings.get(name)

    def expiry_future(self, expiry_ms: int, book: str = "linear") -> str | None:
        """The dated future an expiry's options settle through (from Deribit's metadata)."""
        for info in self.instruments.values():
            if info.kind == "future" and info.book == book and info.expiry_ms == expiry_ms:
                return info.name
        return None

    def delivery_price(self, expiry_ms: int, index: str = INDEX) -> Decimal | None:
        return self.delivery_prices.get((index, ms_to_datetime(expiry_ms).date()))


def load_market_state(
    view: MarketDataView, instruments: InstrumentCache, *, flagged: bool = False
) -> MarketState:
    chain = view.option_chain()
    chain = chain.filter(pc.equal(chain.column("currency"), OPTION_CURRENCY))
    under = view.underlyings()
    unders = _Lazy(under, _UNDERLYING_COLUMNS, option=False)
    index_quote = unders.get(INDEX)
    prices = view.delivery_prices()
    delivery = {
        (n, d): Decimal(p)
        for n, d, p in zip(
            prices.column("index_name").to_pylist(),
            prices.column("delivery_date").to_pylist(),
            prices.column("delivery_price").to_pylist(),
            strict=True,
        )
    }
    return MarketState(
        ts_ms=view.as_of,
        index=None if index_quote is None else index_quote.index_price,
        options=_Lazy(chain, _OPTION_COLUMNS, option=True),
        underlyings=unders,
        instruments=instruments.get(view.instruments()),
        delivery_prices=delivery,
        view=view,
        flagged=flagged,
    )
