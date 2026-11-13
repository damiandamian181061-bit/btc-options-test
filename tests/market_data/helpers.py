"""Small test doubles shared by the market-data tests."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

import msgspec

from optlab.domain.market import (
    ConnectionEvent,
    DeliveryPrice,
    IndexPrice,
    Instrument,
    Ticker,
    Trade,
)
from optlab.market_data.deribit.client import DeribitClient
from optlab.market_data.deribit.convert import instrument_from_wire
from optlab.market_data.deribit.wire import INSTRUMENTS
from optlab.market_data.rows import OptionTradeRow
from optlab.market_data.snapshot import Snapshot
from tests.market_data.fake_deribit import load_instruments


def fixture_instruments() -> list[Instrument]:
    """Every instrument in the fixture file that the adapter can represent."""
    out: list[Instrument] = []
    for items in load_instruments().values():
        for w in INSTRUMENTS.decode(msgspec.json.encode(items)):
            instrument = instrument_from_wire(w)
            if instrument is not None:
                out.append(instrument)
    return out


class ManualClock:
    """A clock tests move by hand. ``sleep`` advances time and yields to the loop."""

    def __init__(self, now_ms: int = 1_794_500_000_000) -> None:
        self._now_ms = now_ms
        self._mono = 1000.0

    def now_ms(self) -> int:
        return self._now_ms

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now_ms += round(seconds * 1000)
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(max(seconds, 0.0))
        await asyncio.sleep(0)


@dataclass
class RecordingSink:
    tickers: list[Ticker] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    indexes: list[IndexPrice] = field(default_factory=list)
    events: list[ConnectionEvent] = field(default_factory=list)
    lost: list[frozenset[str]] = field(default_factory=list)

    def on_ticker(self, ticker: Ticker) -> None:
        self.tickers.append(ticker)

    def on_trades(self, trades: list[Trade]) -> None:
        self.trades.extend(trades)

    def on_index_price(self, price: IndexPrice) -> None:
        self.indexes.append(price)

    def on_connection_event(self, event: ConnectionEvent) -> None:
        self.events.append(event)

    def on_stream_lost(self, keys: frozenset[str]) -> None:
        self.lost.append(keys)

    def kinds(self, source: str | None = None) -> list[str]:
        return [e.kind.value for e in self.events if source is None or e.source == source]


@dataclass
class MemoryStore:
    """Records what the recorder writes. ``fail_next`` makes the next N writes raise."""

    snapshots: list[Snapshot] = field(default_factory=list)
    trades: list[OptionTradeRow] = field(default_factory=list)
    instruments: dict[str, Instrument] = field(default_factory=dict)
    delisted: list[str] = field(default_factory=list)
    delivery: list[DeliveryPrice] = field(default_factory=list)
    events: list[ConnectionEvent] = field(default_factory=list)
    fail_next: int = 0
    resets: int = 0

    def _maybe_fail(self) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("database unavailable")

    async def write_snapshot(self, snapshot: Snapshot) -> tuple[int, int]:
        self._maybe_fail()
        self.snapshots.append(snapshot)
        return len(snapshot.options), len(snapshot.underlyings)

    async def write_trades(self, rows: Sequence[OptionTradeRow]) -> int:
        self._maybe_fail()
        known = {r.trade_id for r in self.trades}
        new = [r for r in rows if r.trade_id not in known]
        self.trades.extend(new)
        return len(new)

    async def upsert_instruments(self, instruments: Sequence[Instrument], seen_ms: int) -> None:
        self._maybe_fail()
        self.instruments.update({i.name: i for i in instruments})

    async def deactivate_instruments(self, names: Sequence[str], delisted_ms: int) -> None:
        self._maybe_fail()
        self.delisted.extend(names)

    async def write_delivery_prices(self, prices: Sequence[DeliveryPrice], recorded_ms: int) -> int:
        self._maybe_fail()
        self.delivery.extend(prices)
        return len(prices)

    async def write_connection_events(self, events: Sequence[ConnectionEvent]) -> None:
        self._maybe_fail()
        self.events.extend(events)

    async def reset(self) -> None:
        self.resets += 1


async def wait_until(predicate: object, timeout: float = 5.0) -> None:
    async def poll() -> None:
        while not predicate():  # type: ignore[operator]
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


def make_client(sink: object, url: str, **kwargs: object) -> DeribitClient:
    """A client tuned for fast tests: tiny backoff, short timeouts."""
    from optlab.market_data.deribit.connection import Backoff

    defaults: dict[str, object] = {
        "backoff": Backoff(initial_s=0.01, max_s=0.05, jitter=0.0),
        "call_timeout_s": 2.0,
    }
    defaults.update(kwargs)
    return DeribitClient(sink, url=url, **defaults)  # type: ignore[arg-type]
