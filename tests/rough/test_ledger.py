"""ROUGH (Phase 2B) ledger: paper fills at bid/ask with fees, buy-backs and settlement.

Every number is worked by hand in the comments (USDC, BTC_USDC options, Standard-tier fees:
taker 0.03% of the index capped at 12.5% of the premium; delivery 0.015% capped at 12.5% of
the option's value). Phase 4 replaces this with the double-entry ledger.
"""

from decimal import Decimal

import pytest

from optlab.domain.money import dec
from optlab.ledger.rough import (
    SpreadLegs,
    cash_of,
    entry_fills,
    exit_fills,
    intrinsic,
    open_value,
    settlement_fills,
)
from optlab.simulator.rough_fees import FeeSchedule, load_fee_schedule
from tests.conftest import REPO_ROOT
from tests.market_data.synthetic import utc_ms

D = Decimal
EXPIRY = utc_ms(2026, 11, 27, 8)  # a Friday
NOW = utc_ms(2026, 11, 15, 9)
INDEX = D("64722.17")


@pytest.fixture
def fees() -> FeeSchedule:
    return load_fee_schedule(REPO_ROOT / "configs")


def bear_call(amount: str = "0.5") -> SpreadLegs:
    return SpreadLegs(
        side="bear call",
        expiry_ms=EXPIRY,
        short_name="BTC_USDC-27NOV26-70000-C",
        long_name="BTC_USDC-27NOV26-72000-C",
        short_strike=D("70000"),
        long_strike=D("72000"),
        amount=D(amount),
    )


def bull_put(amount: str = "1") -> SpreadLegs:
    return SpreadLegs(
        side="bull put",
        expiry_ms=EXPIRY,
        short_name="BTC_USDC-27NOV26-60000-P",
        long_name="BTC_USDC-27NOV26-58000-P",
        short_strike=D("60000"),
        long_strike=D("58000"),
        amount=D(amount),
    )


def test_dec_keeps_the_printed_value_exactly() -> None:
    assert dec(64722.17) == D("64722.17")
    assert dec(0.1) + dec(0.2) == D("0.3")
    with pytest.raises(ValueError, match="finite"):
        dec(float("nan"))


def test_intrinsic_value() -> None:
    assert intrinsic("call", D("70000"), D("71000")) == D("1000")
    assert intrinsic("call", D("70000"), D("69000")) == D("0")
    assert intrinsic("put", D("60000"), D("57000")) == D("3000")
    assert intrinsic("put", D("60000"), D("60000")) == D("0")


def test_entry_sells_the_short_leg_at_its_bid_and_buys_the_long_leg_at_its_ask(
    fees: FeeSchedule,
) -> None:
    # Fee per BTC: min(0.0003 * 64,722.17 = 19.416651, cap) on both legs (caps 65 and 37.5);
    # for 0.5 BTC, 9.7083255 each.
    # Short: +0.5 * 520 - 9.7083255 = 250.2916745. Long: -0.5 * 300 - 9.7083255 = -159.7083255.
    short, long = entry_fills(bear_call(), D("520"), D("300"), INDEX, fees, at_ms=NOW)
    assert (short.instrument_name, short.direction, short.kind) == (
        "BTC_USDC-27NOV26-70000-C",
        "sell",
        "entry",
    )
    assert (long.direction, long.price, long.amount) == ("buy", D("300"), D("0.5"))
    assert short.fee == long.fee == D("9.7083255")
    assert short.cash == D("250.2916745")
    assert long.cash == D("-159.7083255")
    assert cash_of([short, long]) == D("90.583349")  # credit 110 minus fees 19.416651
    assert short.currency == "USDC"


def test_buy_back_buys_the_short_leg_at_its_ask_and_sells_the_long_leg_at_its_bid(
    fees: FeeSchedule,
) -> None:
    # Short ask 120: fee min(19.416651, 0.125 * 120 = 15) = 15 per BTC -> 7.5; cash -60 - 7.5.
    # Long bid 15: fee min(19.416651, 1.875) = 1.875 per BTC -> 0.9375; cash +7.5 - 0.9375.
    short, long = exit_fills(bear_call(), D("120"), D("15"), INDEX, fees, at_ms=NOW)
    assert (short.direction, long.direction) == ("buy", "sell")
    assert short.fee == D("7.5")
    assert long.fee == D("0.9375")
    assert short.cash == D("-67.5")
    assert long.cash == D("6.5625")
    entry = entry_fills(bear_call(), D("520"), D("300"), INDEX, fees, at_ms=NOW)
    # Round trip: 90.583349 - 60.9375 = 29.645849 USDC.
    assert cash_of([*entry, short, long]) == D("29.645849")


def test_bear_call_settles_at_the_delivery_price(fees: FeeSchedule) -> None:
    # Delivery 71,000: the short 70,000 call is worth 1,000; the long 72,000 call is worthless.
    # Delivery fee on the short leg: min(0.00015 * 71,000 = 10.65, 125) * 0.5 = 5.325.
    short, long = settlement_fills(bear_call(), D("71000"), fees)
    assert (short.kind, short.direction, short.price, short.fee) == (
        "settlement",
        "buy",
        D("1000"),
        D("5.325"),
    )
    assert short.cash == D("-505.325")
    assert (long.price, long.fee, long.cash) == (D("0"), D("0"), D("0"))


def test_bull_put_both_in_the_money_loses_the_width_plus_two_delivery_fees(
    fees: FeeSchedule,
) -> None:
    # Delivery 57,000: short 60,000 put worth 3,000, long 58,000 put worth 1,000.
    # Fees: min(0.00015 * 57,000 = 8.55, 375) and min(8.55, 125): 8.55 each for 1 BTC.
    # Cash: -3,000 - 8.55 + 1,000 - 8.55 = -2,017.10.
    fills = settlement_fills(bull_put(), D("57000"), fees)
    assert [f.fee for f in fills] == [D("8.55"), D("8.55")]
    assert cash_of(fills) == D("-2017.10")


def test_a_spread_that_expires_out_of_the_money_costs_nothing(fees: FeeSchedule) -> None:
    fills = settlement_fills(bull_put(), D("64000"), fees)
    assert cash_of(fills) == D("0")
    assert all(f.fee == 0 for f in fills)


def test_open_value_at_mark_is_a_liability() -> None:
    # Short 0.5 at mark 520, long 0.5 at mark 300: 0.5 * (300 - 520) = -110.
    assert open_value(bear_call(), D("520"), D("300")) == D("-110.0")
