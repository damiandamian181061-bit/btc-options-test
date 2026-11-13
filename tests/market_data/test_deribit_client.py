"""The connection pool: typed results, channel spreading, dispatch to the sink, stream loss."""

from decimal import Decimal

import pytest

from optlab.domain.market import Book, InstrumentKind
from tests.market_data.fake_deribit import FakeDeribit
from tests.market_data.helpers import RecordingSink, make_client, wait_until

SESSION_CHANNELS = {
    "ticker.BTC-20NOV26-70000-C.100ms",
    "ticker.BTC_USDC-20NOV26-70000-C.100ms",
    "ticker.BTC-PERPETUAL.100ms",
    "ticker.BTC_USDC-PERPETUAL.100ms",
    "ticker.BTC-27NOV26.100ms",
    "deribit_price_index.btc_usd",
    "deribit_price_index.btc_usdc",
    "trades.option.BTC.100ms",
    "trades.option.USDC.100ms",
}


async def test_get_instruments_returns_typed_instruments() -> None:
    sink = RecordingSink()
    async with FakeDeribit() as server:
        client = make_client(sink, server.url)
        try:
            await client.wait_ready(5)
            options = await client.get_instruments("USDC", "option")
            futures = await client.get_instruments("BTC", "future")
        finally:
            await client.stop()
    assert {i.name for i in options} == {
        "BTC_USDC-20NOV26-70000-C",
        "BTC_USDC-20NOV26-70000-P",
        "ETH_USDC-20NOV26-2000-C",
    }
    assert all(i.book is Book.LINEAR for i in options)
    assert {i.kind for i in futures} == {InstrumentKind.PERPETUAL, InstrumentKind.FUTURE}
    request = server.methods("public/get_instruments")[0]["params"]
    assert request == {"currency": "USDC", "kind": "option", "expired": False}


async def test_delivery_prices_are_typed_and_exact() -> None:
    async with FakeDeribit() as server:
        client = make_client(RecordingSink(), server.url)
        try:
            await client.wait_ready(5)
            prices = await client.get_delivery_prices("btc_usdc", "USDC", count=3)
        finally:
            await client.stop()
    assert prices[0].price == Decimal("70123.45")
    assert prices[0].index_name == "btc_usdc"
    assert prices[0].currency == "USDC"
    assert server.methods("public/get_delivery_prices")[0]["params"] == {
        "index_name": "btc_usdc",
        "count": 3,
    }


async def test_notifications_reach_the_sink_as_typed_records() -> None:
    sink = RecordingSink()
    async with FakeDeribit() as server:
        client = make_client(sink, server.url)
        try:
            await client.set_subscriptions(SESSION_CHANNELS)
            await wait_until(lambda: len(sink.tickers) == 5 and len(sink.trades) == 3)
            await wait_until(lambda: len(sink.indexes) == 2)
        finally:
            await client.stop()
    by_name = {t.instrument_name: t for t in sink.tickers}
    assert by_name["BTC-20NOV26-70000-C"].mark_iv == 0.4125
    assert by_name["BTC_USDC-20NOV26-70000-C"].bid is None
    assert by_name["BTC_USDC-PERPETUAL"].current_funding == 0.0001
    assert {p.index_name for p in sink.indexes} == {"btc_usd", "btc_usdc"}
    assert {t.trade_id for t in sink.trades} == {"400000001", "USDC-100000001", "USDC-100000002"}
    assert client.decode_errors == 0


async def test_channels_spread_over_connections_with_a_cap() -> None:
    sink = RecordingSink()
    async with FakeDeribit() as server:
        client = make_client(sink, server.url, max_channels_per_connection=4)
        try:
            change = await client.set_subscriptions(SESSION_CHANNELS)
            assert change.added == 9
            assert client.connections == 3  # 9 channels, at most 4 each
            await wait_until(lambda: client.connected == 3)
            assert sorted(server.subscribed_channels()) == sorted(SESSION_CHANNELS)
            # Shrinking unsubscribes what is no longer wanted and keeps the rest.
            keep = {c for c in SESSION_CHANNELS if "BTC_USDC" not in c}
            change = await client.set_subscriptions(keep)
            assert change.removed == 2
            assert client.channels == frozenset(keep)
            dropped = [
                c for r in server.methods("public/unsubscribe") for c in r["params"]["channels"]
            ]
            assert sorted(dropped) == sorted(SESSION_CHANNELS - keep)
        finally:
            await client.stop()


async def test_dropped_connections_report_lost_streams_and_recover() -> None:
    sink = RecordingSink()
    async with FakeDeribit() as server:
        client = make_client(sink, server.url, max_channels_per_connection=5)
        try:
            await client.set_subscriptions(SESSION_CHANNELS)
            await wait_until(lambda: client.connected == 2)
            tickers_before = len(sink.tickers)
            await server.drop_all()
            await wait_until(lambda: len(sink.lost) == 2)
            lost = frozenset().union(*sink.lost)
            assert "BTC-PERPETUAL" in lost
            assert "btc_usd" in lost
            await wait_until(lambda: client.connected == 2)
            await wait_until(lambda: len(sink.tickers) >= tickers_before + 5)  # replayed again
            assert sink.kinds().count("disconnected") == 2
        finally:
            await client.stop()


async def test_only_public_market_data_channels() -> None:
    client = make_client(RecordingSink(), "ws://127.0.0.1:9")
    with pytest.raises(ValueError, match="not a public market-data channel"):
        await client.set_subscriptions({"user.portfolio.btc"})
    await client.stop()
