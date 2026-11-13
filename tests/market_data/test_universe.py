"""Discovery keeps exactly the BTC instruments of the two books and runs on schedule."""

import itertools
from datetime import UTC, datetime

import pytest

from optlab.domain.clock import datetime_to_ms
from optlab.domain.market import InstrumentKind
from optlab.market_data.universe import build_universe, next_discovery_ms
from tests.market_data.helpers import fixture_instruments


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    return datetime_to_ms(datetime(year, month, day, hour, minute, second, tzinfo=UTC))


def test_universe_keeps_btc_options_perps_and_futures_of_both_books() -> None:
    universe = build_universe(fixture_instruments())
    assert set(universe.instruments) == {
        "BTC-20NOV26-70000-C",
        "BTC-20NOV26-70000-P",
        "BTC-ODDNAME",
        "BTC_USDC-20NOV26-70000-C",
        "BTC_USDC-20NOV26-70000-P",
        "BTC-PERPETUAL",
        "BTC-27NOV26",
        "BTC_USDC-PERPETUAL",
        "BTC_USDC-27NOV26",
    }
    # Left out: ETH_USDC option and perpetual (other base), the combo, the inactive option.
    assert len(universe.options) == 5
    assert {i.kind for i in universe.underlyings} == {
        InstrumentKind.PERPETUAL,
        InstrumentKind.FUTURE,
    }


def test_index_currencies_come_from_instrument_metadata() -> None:
    universe = build_universe(fixture_instruments())
    assert universe.index_currencies() == {"btc_usd": "USD", "btc_usdc": "USDC"}


def test_channels_cover_tickers_indexes_and_option_trades() -> None:
    universe = build_universe(fixture_instruments())
    channels = universe.channels("100ms")
    assert "ticker.BTC-ODDNAME.100ms" in channels
    assert "ticker.BTC_USDC-PERPETUAL.100ms" in channels
    assert {"deribit_price_index.btc_usd", "deribit_price_index.btc_usdc"} <= channels
    assert {"trades.option.BTC.100ms", "trades.option.USDC.100ms"} <= channels
    assert len(channels) == 9 + 2 + 2


def test_diff_names_added_and_removed() -> None:
    full = build_universe(fixture_instruments())
    smaller = build_universe(i for i in fixture_instruments() if i.name != "BTC-ODDNAME")
    assert smaller.diff(full) == ({"BTC-ODDNAME"}, set())
    assert full.diff(smaller) == (set(), {"BTC-ODDNAME"})


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (utc(2026, 11, 13, 7, 55), utc(2026, 11, 13, 8, 0, 30)),
        (utc(2026, 11, 13, 8, 0, 29), utc(2026, 11, 13, 8, 0, 30)),
        (utc(2026, 11, 13, 8, 0, 30), utc(2026, 11, 13, 8, 10, 30)),
        (utc(2026, 11, 13, 8, 3), utc(2026, 11, 13, 8, 10, 30)),
        (utc(2026, 11, 13, 23, 55), utc(2026, 11, 14, 0, 0, 30)),
    ],
)
def test_discovery_every_10_minutes_and_just_after_expiry(now: int, expected: int) -> None:
    assert next_discovery_ms(now) == expected


def test_after_expiry_run_is_kept_whatever_the_interval() -> None:
    seven_minutes = 7 * 60_000
    now = utc(2026, 11, 13, 7, 59)
    assert next_discovery_ms(now, seven_minutes) == utc(2026, 11, 13, 8, 0, 30)
    runs, t = [], utc(2026, 11, 13, 0, 0)
    while t < utc(2026, 11, 14, 0, 0):
        t = next_discovery_ms(t, seven_minutes)
        runs.append(t)
    assert utc(2026, 11, 13, 8, 0, 30) in runs
    assert max(b - a for a, b in itertools.pairwise(runs)) <= seven_minutes
