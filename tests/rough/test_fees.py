"""ROUGH (Phase 2B) fees: Deribit's option fee page, in exact decimals.

Numbers checked by hand from configs/fees/deribit.yaml (Standard tier, confirmed 15 Nov 2026).
"""

from decimal import Decimal

import pytest

from optlab.simulator.rough_fees import Book, FeeSchedule, is_daily_expiry, load_fee_schedule
from tests.conftest import REPO_ROOT
from tests.market_data.synthetic import utc_ms

D = Decimal
FRI = utc_ms(2026, 11, 27, 8)  # a Friday expiry
MON = utc_ms(2026, 11, 16, 8)  # a "daily" expiry


@pytest.fixture
def fees() -> FeeSchedule:
    return load_fee_schedule(REPO_ROOT / "configs")


def test_the_config_records_its_source_and_dates(fees: FeeSchedule) -> None:
    assert fees.source == "https://support.deribit.com/hc/en-us/articles/25944746248989-Fees"
    assert fees.effective_from_ms == utc_ms(2026, 11, 10, 12)
    assert fees.tier == "standard"
    assert fees.trading["linear"].taker == D("0.0003")
    assert fees.trading["linear"].cap_of_premium == D("0.125")
    assert fees.delivery.linear.rate == D("0.00015")


def test_daily_means_not_a_friday() -> None:
    assert not is_daily_expiry(FRI)
    assert is_daily_expiry(MON)


def test_linear_taker_fee_is_3_bps_of_the_index_capped_at_12_5_pct_of_premium(
    fees: FeeSchedule,
) -> None:
    # 0.0003 * 64,722.17 = 19.416651 USDC per BTC, under the cap 0.125 * 860 = 107.5.
    # For 0.5 BTC: 9.7083255 USDC.
    fee = fees.trading_fee("linear", D("860"), D("64722.17"), D("0.5"), FRI, at_ms=FRI - 1)
    assert fee == D("9.7083255")
    # A cheap option: the cap 0.125 * 100 = 12.5 per BTC binds; 0.5 BTC pays 6.25.
    assert fees.trading_fee("linear", D("100"), D("64722.17"), D("0.5"), FRI, at_ms=FRI) == D(
        "6.25"
    )


def test_linear_daily_options_have_no_maker_fee_until_31_jan_2027(fees: FeeSchedule) -> None:
    args: tuple[Book, Decimal, Decimal, Decimal, int] = (
        "linear",
        D("860"),
        D("64722.17"),
        D("1"),
        MON,
    )
    assert fees.trading_fee(*args, at_ms=MON - 1, maker=True) == D("0")
    assert fees.trading_fee(*args, at_ms=MON - 1) == D("19.416651")  # taker unchanged
    friday: tuple[Book, Decimal, Decimal, Decimal, int] = (
        "linear",
        D("860"),
        D("64722.17"),
        D("1"),
        FRI,
    )
    assert fees.trading_fee(*friday, at_ms=FRI - 1, maker=True) == D("19.416651")
    after = utc_ms(2027, 2, 1, 8)
    assert fees.trading_fee(
        "linear", D("860"), D("64722.17"), D("1"), after, at_ms=after - 1, maker=True
    ) == D("19.416651")


def test_inverse_fee_is_0_0003_btc_per_contract_capped_at_12_5_pct(fees: FeeSchedule) -> None:
    # Underlying is 1 BTC per contract: 0.0003 BTC, under the cap 0.125 * 0.0135 = 0.0016875.
    assert fees.trading_fee("inverse", D("0.0135"), D("64722.17"), D("2"), FRI, at_ms=FRI) == D(
        "0.0006"
    )
    # Premium 0.002 BTC: cap 0.00025 BTC per contract binds.
    assert fees.trading_fee("inverse", D("0.002"), D("64722.17"), D("1"), FRI, at_ms=FRI) == D(
        "0.000250"
    )


def test_delivery_fee_only_in_the_money_and_capped_at_12_5_pct_of_value(fees: FeeSchedule) -> None:
    # In the money by 4,000: 0.00015 * 64,000 = 9.6 per BTC, under the cap 500.
    assert fees.delivery_fee("linear", D("4000"), D("64000"), D("0.5"), FRI) == D("4.80000")
    # In the money by 50: the cap 0.125 * 50 = 6.25 per BTC binds.
    assert fees.delivery_fee("linear", D("50"), D("64000"), D("1"), FRI) == D("6.250")
    assert fees.delivery_fee("linear", D("0"), D("64000"), D("1"), FRI) == D("0")
    # Daily options pay no delivery fee.
    assert fees.delivery_fee("linear", D("4000"), D("64000"), D("1"), MON) == D("0")


def test_a_schedule_is_refused_before_its_effective_date(fees: FeeSchedule) -> None:
    before = utc_ms(2026, 11, 10, 11, 59)
    with pytest.raises(ValueError, match="before the fee schedule's effective date"):
        fees.trading_fee("linear", D("860"), D("64722.17"), D("1"), FRI, at_ms=before)
