"""Deribit's JSON becomes typed records with our conventions (decimal IVs, None for empty sides,
metadata-only instrument fields)."""

from datetime import date
from decimal import Decimal

import msgspec
import pytest

from optlab.domain.market import Book, Direction, InstrumentKind, OptionType
from optlab.market_data.deribit import wire
from optlab.market_data.deribit.convert import (
    delivery_prices_from_wire,
    instrument_from_wire,
    iv_decimal,
    ticker_from_wire,
    trade_from_wire,
)
from tests.market_data.fake_deribit import load_delivery_prices, load_instruments, load_session
from tests.market_data.helpers import fixture_instruments

RECV = 1_794_500_001_000


def notification(channel: str) -> msgspec.Raw:
    for message in load_session():
        if message.get("params", {}).get("channel") == channel:
            env = wire.ENVELOPE.decode(msgspec.json.encode(message))
            return wire.SUBSCRIPTION_PARAMS.decode(env.params).data
    raise KeyError(channel)


def test_inverse_option_ticker() -> None:
    t = ticker_from_wire(wire.TICKER.decode(notification("ticker.BTC-20NOV26-70000-C.100ms")), RECV)
    assert t.instrument_name == "BTC-20NOV26-70000-C"
    assert (t.exchange_ts_ms, t.recv_ts_ms) == (1794500000100, RECV)
    assert (t.bid, t.bid_size, t.ask, t.ask_size) == (0.027, 22.0, 0.0285, 15.5)
    assert t.mark_iv == 0.4125
    assert t.bid_iv == 0.405
    assert t.ask_iv == 0.4275
    assert t.greeks is not None
    assert (t.greeks.delta, t.greeks.vega, t.greeks.theta) == (0.55123, 41.2345, -98.7654)
    assert t.underlying_index == "BTC-20NOV26"
    assert t.current_funding is None


def test_empty_bid_side_becomes_none_with_zero_size() -> None:
    raw = notification("ticker.BTC_USDC-20NOV26-70000-C.100ms")
    t = ticker_from_wire(wire.TICKER.decode(raw), RECV)
    assert t.bid is None
    assert t.bid_size == 0.0
    assert t.bid_iv is None  # Deribit sends 0 for the IV of an empty side
    assert (t.ask, t.ask_size, t.ask_iv) == (2000.0, 3.25, 0.431)
    assert t.last_price is None


def test_perpetual_ticker_keeps_funding() -> None:
    t = ticker_from_wire(wire.TICKER.decode(notification("ticker.BTC-PERPETUAL.100ms")), RECV)
    assert (t.current_funding, t.funding_8h, t.interest_value) == (3.5e-05, 2.9e-05, 0.0123)
    assert t.open_interest == 600000000.0
    assert t.greeks is None
    assert t.mark_iv is None


@pytest.mark.parametrize(
    ("percent", "expected"),
    [(41.25, 0.4125), (37.07, 0.3707), (0.0, None), (None, None), (-1.0, None)],
)
def test_iv_percent_to_decimal(percent: float | None, expected: float | None) -> None:
    assert iv_decimal(percent) == expected


def test_trades_convert_with_decimal_iv() -> None:
    trades = wire.TRADES.decode(notification("trades.option.USDC.100ms"))
    converted = [trade_from_wire(w, RECV) for w in trades]
    first = converted[0]
    assert first is not None
    assert first.trade_id == "USDC-100000001"
    assert first.direction is Direction.BUY
    assert first.iv == 0.43
    assert (first.price, first.amount, first.trade_seq) == (2000.0, 0.05, 3)
    assert converted[1] is not None
    assert converted[1].instrument_name == "ETH_USDC-20NOV26-2000-C"


def test_instrument_fields_come_from_metadata_not_the_name() -> None:
    by_name = {i.name: i for i in fixture_instruments()}
    odd = by_name["BTC-ODDNAME"]
    assert odd.kind is InstrumentKind.OPTION
    assert odd.strike == 85000.0
    assert odd.option_type is OptionType.PUT
    assert odd.expiry_ms == 1798185600000
    assert odd.contract_size == 1.0
    assert odd.book is Book.INVERSE


def test_instrument_kinds_and_books() -> None:
    by_name = {i.name: i for i in fixture_instruments()}
    assert by_name["BTC-PERPETUAL"].kind is InstrumentKind.PERPETUAL
    assert by_name["BTC-PERPETUAL"].expiry_ms is None
    assert by_name["BTC-PERPETUAL"].price_currency == "USD"
    assert by_name["BTC_USDC-27NOV26"].kind is InstrumentKind.FUTURE
    assert by_name["BTC_USDC-27NOV26"].book is Book.LINEAR
    assert by_name["BTC_USDC-20NOV26-70000-C"].price_currency == "USDC"
    assert by_name["BTC-20NOV26-70000-C"].price_currency == "BTC"


def test_combos_are_not_instruments() -> None:
    names = {i.name for i in fixture_instruments()}
    assert "BTC-CS-20NOV26-70000_72000" not in names
    combo = next(i for i in load_instruments()["BTC/option"] if i["kind"] == "option_combo")
    assert instrument_from_wire(wire.INSTRUMENTS.decode(msgspec.json.encode([combo]))[0]) is None


def test_unknown_fields_are_ignored_and_optional_fields_may_be_missing() -> None:
    raw = b'{"instrument_name":"X","timestamp":1,"state":"open","brand_new_field":{"a":1}}'
    t = ticker_from_wire(wire.TICKER.decode(raw), RECV)
    assert (t.bid, t.ask, t.mark_price, t.greeks) == (None, None, None, None)


def test_delivery_prices_are_exact_decimals() -> None:
    raw = msgspec.json.encode(load_delivery_prices())
    prices = delivery_prices_from_wire(wire.DELIVERY_PRICES.decode(raw), "btc_usd", "USD")
    assert prices[0].delivery_date == date(2026, 11, 12)
    assert prices[0].price == Decimal("70123.45")
    assert prices[2].price == Decimal("71555.1")
    assert {p.currency for p in prices} == {"USD"}
