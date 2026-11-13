"""The interface between an exchange adapter and whatever consumes its market data."""

from typing import Protocol

from optlab.domain.market import ConnectionEvent, IndexPrice, Ticker, Trade


class MarketDataSink(Protocol):
    """Receives typed market data. Called on the event loop; must not block."""

    def on_ticker(self, ticker: Ticker) -> None: ...

    def on_trades(self, trades: list[Trade]) -> None: ...

    def on_index_price(self, price: IndexPrice) -> None: ...

    def on_connection_event(self, event: ConnectionEvent) -> None: ...

    def on_stream_lost(self, keys: frozenset[str]) -> None:
        """The streams for these instruments or indexes stopped (their connection dropped)."""
        ...
