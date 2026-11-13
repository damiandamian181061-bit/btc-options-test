"""The latest state of every instrument, and the one-minute snapshots taken from it.

The recorder keeps the newest ticker per instrument and the newest value per price index. At
each minute boundary ``t`` it writes one row per instrument from that state, following these
rules:

- point in time: the snapshot is taken the moment the first message received after ``t``
  arrives (or a timer fires, if the feed is quiet), so it holds exactly what had been received
  by ``t``;
- no stale rows: an instrument whose stream dropped is removed from the state until its
  connection is back, and anything not updated for ``max_age_ms`` is skipped, so a dead feed
  shows up as a gap rather than as repeated old prices;
- expired instruments are not written after their expiry.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from optlab.domain.market import IndexPrice, InstrumentKind, Ticker
from optlab.market_data.rows import (
    OptionQuoteRow,
    UnderlyingTickRow,
    index_row,
    option_quote_row,
    underlying_row,
)
from optlab.market_data.universe import Universe


@dataclass
class LatestState:
    tickers: dict[str, Ticker] = field(default_factory=dict)
    indexes: dict[str, IndexPrice] = field(default_factory=dict)

    def update_ticker(self, ticker: Ticker) -> None:
        self.tickers[ticker.instrument_name] = ticker

    def update_index(self, price: IndexPrice) -> None:
        self.indexes[price.index_name] = price

    def drop(self, keys: Iterable[str]) -> None:
        for key in keys:
            self.tickers.pop(key, None)
            self.indexes.pop(key, None)


@dataclass(frozen=True)
class Snapshot:
    ts_ms: int
    options: list[OptionQuoteRow]
    underlyings: list[UnderlyingTickRow]


def take_snapshot(ts_ms: int, universe: Universe, state: LatestState, max_age_ms: int) -> Snapshot:
    """One row per recorded instrument and index with fresh data at ``ts_ms``."""

    def fresh(recv_ts_ms: int) -> bool:
        return recv_ts_ms <= ts_ms and ts_ms - recv_ts_ms <= max_age_ms

    options: list[OptionQuoteRow] = []
    underlyings: list[UnderlyingTickRow] = []
    for name, instrument in universe.instruments.items():
        ticker = state.tickers.get(name)
        if ticker is None or not fresh(ticker.recv_ts_ms):
            continue
        if instrument.expiry_ms is not None and instrument.expiry_ms < ts_ms:
            continue
        if instrument.kind is InstrumentKind.OPTION:
            options.append(option_quote_row(ts_ms, instrument, ticker))
        else:
            underlyings.append(underlying_row(ts_ms, instrument, ticker))
    for index_name, currency in universe.index_currencies().items():
        price = state.indexes.get(index_name)
        if price is not None and fresh(price.recv_ts_ms):
            underlyings.append(index_row(ts_ms, price, currency))
    return Snapshot(ts_ms=ts_ms, options=options, underlyings=underlyings)
