"""Deribit public market data over several WebSocket connections.

This is the exchange adapter's front door. Callers get typed records (``Instrument``,
``Ticker``, ``Trade``...) through a ``MarketDataSink`` and typed method results; Deribit's JSON
never leaves ``optlab.market_data.deribit``.

Channels are spread over connections of at most ``max_channels_per_connection`` each (Deribit
allows 32 connections per IP and warns that too many channels on one connection end in
``connection_too_slow``). A new connection opens only when the existing ones are full.
"""

import asyncio
import logging
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

import msgspec
from websockets.exceptions import ConnectionClosed

from optlab.domain.clock import Clock, SystemClock
from optlab.domain.market import ConnectionEvent, DeliveryPrice, Instrument
from optlab.market_data.deribit import channels as ch
from optlab.market_data.deribit import wire
from optlab.market_data.deribit.connection import (
    PUBLIC_WS_URL,
    Backoff,
    DeribitConnection,
    DeribitError,
    NotConnectedError,
)
from optlab.market_data.deribit.convert import (
    delivery_prices_from_wire,
    index_price_from_wire,
    instrument_from_wire,
    ticker_from_wire,
    trade_from_wire,
)
from optlab.market_data.deribit.rate_limit import RateLimits
from optlab.market_data.sink import MarketDataSink

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubscriptionChange:
    added: int
    removed: int


class DeribitClient:
    """A pool of public Deribit connections feeding one sink."""

    def __init__(
        self,
        sink: MarketDataSink,
        *,
        url: str = PUBLIC_WS_URL,
        max_channels_per_connection: int = 400,
        max_connections: int = 8,
        heartbeat_s: int = 10,
        silence_timeout_s: float | None = None,
        call_timeout_s: float = 15.0,
        backoff: Backoff | None = None,
        clock: Clock | None = None,
        limits: RateLimits | None = None,
    ) -> None:
        if max_channels_per_connection < 1 or not 1 <= max_connections <= 16:
            raise ValueError("need 1+ channels per connection and 1 to 16 connections")
        self._sink = sink
        self._url = url
        self._max_channels = max_channels_per_connection
        self._max_connections = max_connections
        self._heartbeat_s = heartbeat_s
        self._silence_timeout_s = silence_timeout_s
        self._call_timeout_s = call_timeout_s
        self._backoff = backoff or Backoff()
        self._clock = clock or SystemClock()
        self._limits = limits or RateLimits(self._clock)
        self._connections: list[DeribitConnection] = []
        self._assignment: dict[str, DeribitConnection] = {}
        self._unknown_channels: set[str] = set()
        self.messages = 0
        self.decode_errors = 0

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if not self._connections:
            self._new_connection()

    async def stop(self) -> None:
        await asyncio.gather(*(c.stop() for c in self._connections), return_exceptions=True)

    async def wait_ready(self, timeout: float) -> None:
        """Wait until at least one connection is up."""
        self.start()
        waits = [asyncio.create_task(c.wait_connected()) for c in self._connections]
        try:
            done, _ = await asyncio.wait(
                waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                raise NotConnectedError(f"no Deribit connection within {timeout:.0f}s")
        finally:
            for w in waits:
                w.cancel()

    @property
    def channels(self) -> frozenset[str]:
        return frozenset(self._assignment)

    @property
    def connections(self) -> int:
        return len(self._connections)

    @property
    def connected(self) -> int:
        return sum(c.is_connected for c in self._connections)

    # ----------------------------------------------------------------- requests

    async def _call(self, method: str, params: dict[str, Any]) -> msgspec.Raw:
        for conn in self._connections:
            if conn.is_connected:
                return await conn.call(method, params, self._call_timeout_s)
        await self.wait_ready(self._call_timeout_s)
        return await self._call(method, params)

    async def get_instruments(self, currency: str, kind: str) -> list[Instrument]:
        """Active instruments settled in ``currency`` of ``kind`` (``option`` or ``future``)."""
        raw = await self._call(
            "public/get_instruments", {"currency": currency, "kind": kind, "expired": False}
        )
        out: list[Instrument] = []
        for w in wire.INSTRUMENTS.decode(raw):
            instrument = instrument_from_wire(w)
            if instrument is not None:
                out.append(instrument)
        return out

    async def get_delivery_prices(
        self, index_name: str, currency: str, count: int = 10
    ) -> list[DeliveryPrice]:
        """The latest ``count`` daily delivery prices of an index, newest first."""
        raw = await self._call(
            "public/get_delivery_prices", {"index_name": index_name, "count": count}
        )
        return delivery_prices_from_wire(wire.DELIVERY_PRICES.decode(raw), index_name, currency)

    # ----------------------------------------------------------------- subscriptions

    async def set_subscriptions(self, desired: Collection[str]) -> SubscriptionChange:
        """Subscribe to exactly ``desired``: add what's missing, drop what's no longer wanted."""
        wanted = set(desired)
        for channel in wanted:
            if not channel.startswith(ch.PUBLIC_PREFIXES):
                raise ValueError(f"not a public market-data channel: {channel}")
        removals: dict[DeribitConnection, list[str]] = {}
        for channel in [c for c in self._assignment if c not in wanted]:
            removals.setdefault(self._assignment.pop(channel), []).append(channel)
        additions: dict[DeribitConnection, list[str]] = {}
        load = Counter(self._assignment.values())
        for channel in sorted(c for c in wanted if c not in self._assignment):
            conn = self._pick_connection(load)
            self._assignment[channel] = conn
            load[conn] += 1
            additions.setdefault(conn, []).append(channel)
        await asyncio.gather(
            *(conn.unsubscribe(chs) for conn, chs in removals.items()),
            *(self._subscribe(conn, chs) for conn, chs in additions.items()),
        )
        return SubscriptionChange(
            added=sum(map(len, additions.values())), removed=sum(map(len, removals.values()))
        )

    def _pick_connection(self, load: Counter[DeribitConnection]) -> DeribitConnection:
        open_ = [c for c in self._connections if load[c] < self._max_channels]
        if open_:
            return min(open_, key=lambda c: load[c])
        if len(self._connections) < self._max_connections:
            return self._new_connection()
        logger.warning("all %d connections are full; overloading one", len(self._connections))
        return min(self._connections, key=lambda c: load[c])

    async def _subscribe(self, conn: DeribitConnection, channels: list[str]) -> None:
        try:
            await conn.subscribe(channels)
        except (DeribitError, ConnectionClosed, TimeoutError, OSError) as exc:
            # The channels stay with the connection and are subscribed when it reconnects.
            logger.warning("%s: subscribe deferred to reconnect (%r)", conn.name, exc)

    def _new_connection(self) -> DeribitConnection:
        conn = DeribitConnection(
            f"deribit-ws-{len(self._connections)}",
            url=self._url,
            on_notification=self._dispatch,
            on_event=self._on_event,
            on_lost=self._on_lost,
            on_rejected=self._on_rejected,
            limits=self._limits,
            heartbeat_s=self._heartbeat_s,
            silence_timeout_s=self._silence_timeout_s,
            backoff=self._backoff,
            clock=self._clock,
        )
        self._connections.append(conn)
        conn.start()
        return conn

    # ----------------------------------------------------------------- callbacks

    def _dispatch(self, channel: str, data: msgspec.Raw, recv_ms: int) -> None:
        self.messages += 1
        try:
            if channel.startswith(ch.TICKER_PREFIX):
                self._sink.on_ticker(ticker_from_wire(wire.TICKER.decode(data), recv_ms))
            elif channel.startswith(ch.TRADES_PREFIX):
                trades = [trade_from_wire(w, recv_ms) for w in wire.TRADES.decode(data)]
                self._sink.on_trades([t for t in trades if t is not None])
            elif channel.startswith(ch.PRICE_INDEX_PREFIX):
                self._sink.on_index_price(
                    index_price_from_wire(wire.PRICE_INDEX.decode(data), recv_ms)
                )
            elif channel not in self._unknown_channels:
                self._unknown_channels.add(channel)
                logger.error("message on unknown channel %s", channel)
        except msgspec.DecodeError as exc:
            self.decode_errors += 1
            if self.decode_errors <= 10 or self.decode_errors % 1000 == 0:
                logger.error(
                    "cannot decode %s (%s); total decode errors %d",
                    channel,
                    exc,
                    self.decode_errors,
                )

    def _on_event(self, event: ConnectionEvent) -> None:
        self._sink.on_connection_event(event)

    def _on_lost(self, _conn: str, channels: frozenset[str]) -> None:
        keys = {ch.stream_key(c) for c in channels}
        self._sink.on_stream_lost(frozenset(k for k in keys if k is not None))

    def _on_rejected(self, conn_name: str, channels: frozenset[str]) -> None:
        for channel in channels:
            conn = self._assignment.get(channel)
            if conn is not None and conn.name == conn_name:
                del self._assignment[channel]
