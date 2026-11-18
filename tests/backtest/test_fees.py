"""Phase 4 fee schedule: options, perpetuals and funding, in exact decimals.

Every expected number is worked by hand from configs/fees/deribit.yaml (Standard tier; Deribit's
fee page re-read on 18 Nov 2026, unchanged since 11 Nov) and Deribit's Funding Specifications.
"""

from decimal import Decimal

import pytest

from optlab.simulator.fees import FeeSchedule, funding_payment, funding_rate, load_fees
from tests.conftest import REPO_ROOT
from tests.market_data.synthetic import utc_ms

D = Decimal
FRI = utc_ms(2026, 11, 27, 8)
MON = utc_ms(2026, 11, 16, 8)
PERP = "BTC_USDC-PERPETUAL"


@pytest.fixture
def fees() -> FeeSchedule:
    return load_fees(REPO_ROOT / "configs")


def test_the_schedule_records_its_sources(fees: FeeSchedule) -> None:
    assert fees.source.endswith("25944746248989-Fees")
    assert fees.funding.source.endswith("31424939178397-Funding-Specifications")
    assert fees.tier == "standard"
    assert fees.combo_discount is False  # Damian, 18 Nov 2026


def test_option_taker_fee_is_3_bps_of_the_index_capped_at_12_5_pct_of_premium(
    fees: FeeSchedule,
) -> None:
    # 0.0003 x 65,000 = 19.5 a BTC (under 0.125 x 800 = 100); 0.5 BTC: 9.75.
    assert fees.option_fee(D("800"), D("65000"), D("0.5"), FRI, at_ms=FRI - 1) == D("9.75")
    # Cheap option: the cap 0.125 x 100 = 12.5 a BTC binds; 0.5 BTC: 6.25.
    assert fees.option_fee(D("100"), D("65000"), D("0.5"), FRI, at_ms=FRI - 1) == D("6.25")


def test_the_multiplier_scales_every_fee(fees: FeeSchedule) -> None:
    double = fees.scaled(D("2"))
    assert double.option_fee(D("800"), D("65000"), D("0.5"), FRI, at_ms=FRI - 1) == D("19.50")
    assert double.option_delivery_fee(D("500"), D("59000"), D("0.5"), FRI) == D("8.850")
    assert double.perpetual_fee(D("65000"), D("1"), at_ms=FRI) == D("45.50")


def test_daily_options_pay_no_maker_fee_but_full_taker(fees: FeeSchedule) -> None:
    assert fees.option_fee(D("800"), D("65000"), D("1"), MON, at_ms=MON - 1, maker=True) == 0
    assert fees.option_fee(D("800"), D("65000"), D("1"), MON, at_ms=MON - 1) == D("19.5")


def test_a_run_before_the_schedule_took_effect_is_refused(fees: FeeSchedule) -> None:
    with pytest.raises(ValueError, match="effective"):
        fees.option_fee(D("800"), D("65000"), D("1"), FRI, at_ms=utc_ms(2026, 11, 1))


def test_delivery_fee_is_1_5_bps_of_the_delivery_price_capped_at_12_5_pct_of_value(
    fees: FeeSchedule,
) -> None:
    # docs/accounting.md: 59,000 delivery, 60,000 put worth 1,000 a BTC, 0.5 BTC.
    # min(0.00015 x 59,000 = 8.85, 0.125 x 1,000 = 125) x 0.5 = 4.425.
    assert fees.option_delivery_fee(D("1000"), D("59000"), D("0.5"), FRI) == D("4.425")
    # Barely in the money: worth 10 a BTC, cap 1.25 binds.
    assert fees.option_delivery_fee(D("10"), D("59000"), D("1"), FRI) == D("1.250")
    assert fees.option_delivery_fee(D("0"), D("59000"), D("1"), FRI) == 0  # worthless
    assert fees.option_delivery_fee(D("1000"), D("59000"), D("1"), MON) == 0  # daily: exempt


def test_perpetual_taker_fee_is_3_5_bps_of_notional(fees: FeeSchedule) -> None:
    # 0.00035 x 65,000 x 0.2 = 4.55 USDC.
    assert fees.perpetual_fee(D("65000"), D("0.2"), at_ms=FRI) == D("4.55000")
    assert fees.perpetual_fee(D("65000"), D("0.2"), at_ms=FRI, maker=True) == D("1.95000")


@pytest.mark.parametrize(
    ("mark", "index", "expected"),
    [
        # Deribit's examples: premium 0.075% -> 0.05%; 0.02% -> 0 (inside the damper);
        # 7.5% on the BTC_USDC perpetual (cap 5%) -> 5%.
        ("100075", "100000", "0.0005"),
        ("100020", "100000", "0"),
        ("99925", "100000", "-0.0005"),
        ("107500", "100000", "0.05"),
        ("90000", "100000", "-0.05"),
    ],
)
def test_funding_rate_damper_and_cap(
    fees: FeeSchedule, mark: str, index: str, expected: str
) -> None:
    assert funding_rate(D(mark), D(index), fees.funding, PERP) == D(expected)


def test_the_inverse_perpetual_has_the_btc_cap(fees: FeeSchedule) -> None:
    assert funding_rate(D("107500"), D("100000"), fees.funding, "BTC-PERPETUAL") == D("0.005")


def test_funding_payment_for_one_minute_and_eight_hours(fees: FeeSchedule) -> None:
    # Deribit's SOL_USDC example scaled to BTC: rate 0.075%, long notional 100,000 USDC
    # (1 BTC at an index of 100,000), 1 minute -> 0.075% x 100,000 / 480 = 0.15625 paid.
    rate = D("0.00075")
    one_min = funding_payment(rate, D("1"), D("100000"), 60_000, fees.funding)
    assert one_min == D("0.15625")
    eight_h = funding_payment(rate, D("1"), D("100000"), 8 * 3_600_000, fees.funding)
    assert eight_h == D("75")
    # A short receives it: the payment is negative.
    assert funding_payment(rate, D("-1"), D("100000"), 60_000, fees.funding) == D("-0.15625")
