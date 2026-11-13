"""Minute snapshots: point in time, no stale rows, nothing after expiry, currencies attached."""

from dataclasses import replace

from optlab.domain.clock import ms_to_datetime
from optlab.domain.market import Greeks, IndexPrice, Ticker
from optlab.market_data.snapshot import LatestState, take_snapshot
from optlab.market_data.universe import build_universe
from tests.market_data.helpers import fixture_instruments

T = 1_794_500_040_000  # a minute boundary
MAX_AGE = 300_000


def ticker(name: str, recv_ms: int, **kw: object) -> Ticker:
    base = Ticker(
        instrument_name=name,
        exchange_ts_ms=recv_ms - 5,
        recv_ts_ms=recv_ms,
        state="open",
        bid=0.02,
        bid_size=1.0,
        ask=0.03,
        ask_size=2.0,
        mark_price=0.025,
        last_price=None,
        index_price=70_000.0,
        open_interest=10.0,
        estimated_delivery_price=70_000.0,
        mark_iv=0.41,
        greeks=Greeks(0.5, 0.0001, 40.0, -90.0, 7.0),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def test_snapshot_rows_carry_time_currency_and_fields() -> None:
    universe = build_universe(fixture_instruments())
    state = LatestState()
    state.update_ticker(ticker("BTC-20NOV26-70000-C", T - 1_000))
    state.update_ticker(ticker("BTC_USDC-20NOV26-70000-C", T - 2_000, bid=None, bid_size=0.0))
    state.update_ticker(ticker("BTC-PERPETUAL", T - 500, mark_iv=None, greeks=None))
    state.update_index(IndexPrice("btc_usd", 70_000.0, T - 300, T - 200))
    snap = take_snapshot(T, universe, state, MAX_AGE)
    rows = {r.instrument_name: r for r in snap.options}
    assert set(rows) == {"BTC-20NOV26-70000-C", "BTC_USDC-20NOV26-70000-C"}
    inverse, linear = rows["BTC-20NOV26-70000-C"], rows["BTC_USDC-20NOV26-70000-C"]
    assert inverse.ts == ms_to_datetime(T)
    assert (inverse.currency, linear.currency) == ("BTC", "USDC")
    assert (inverse.delta, inverse.vega, inverse.mark_iv) == (0.5, 40.0, 0.41)
    assert (linear.bid, linear.bid_size) == (None, 0.0)
    assert inverse.recv_ts_ms == T - 1_000
    under = {r.instrument_name: r for r in snap.underlyings}
    assert under["BTC-PERPETUAL"].kind == "perpetual"
    assert under["BTC-PERPETUAL"].currency == "USD"
    assert under["btc_usd"].kind == "index"
    assert under["btc_usd"].index_price == 70_000.0
    assert "btc_usdc" not in under  # no value received yet


def test_data_received_after_the_boundary_is_not_in_that_snapshot() -> None:
    universe = build_universe(fixture_instruments())
    state = LatestState()
    state.update_ticker(ticker("BTC-20NOV26-70000-C", T + 1))
    state.update_index(IndexPrice("btc_usd", 70_000.0, T, T + 1))
    snap = take_snapshot(T, universe, state, MAX_AGE)
    assert snap.options == []
    assert snap.underlyings == []


def test_stale_and_lost_streams_leave_a_gap() -> None:
    universe = build_universe(fixture_instruments())
    state = LatestState()
    state.update_ticker(ticker("BTC-20NOV26-70000-C", T - MAX_AGE - 1))  # too old
    state.update_ticker(ticker("BTC-20NOV26-70000-P", T - MAX_AGE))  # just fresh enough
    state.update_ticker(ticker("BTC_USDC-20NOV26-70000-C", T - 10))
    state.drop({"BTC_USDC-20NOV26-70000-C"})  # its connection dropped
    snap = take_snapshot(T, universe, state, MAX_AGE)
    assert [r.instrument_name for r in snap.options] == ["BTC-20NOV26-70000-P"]


def test_expired_instruments_are_not_written() -> None:
    instruments = fixture_instruments()
    universe = build_universe(instruments)
    expiry = universe.instruments["BTC-20NOV26-70000-C"].expiry_ms
    assert expiry is not None
    state = LatestState()
    state.update_ticker(ticker("BTC-20NOV26-70000-C", expiry - 10))
    assert len(take_snapshot(expiry, universe, state, MAX_AGE).options) == 1  # at expiry
    assert take_snapshot(expiry + 60_000, universe, state, MAX_AGE).options == []


def test_unknown_instruments_are_ignored() -> None:
    universe = build_universe(fixture_instruments())
    state = LatestState()
    state.update_ticker(ticker("ETH_USDC-20NOV26-2000-C", T - 10))
    assert take_snapshot(T, universe, state, MAX_AGE).options == []
