"""ROUGH (Phase 2B) volatility maths: hand-computed golden values.

Each expected number is worked out in the comment next to it, so it can be checked by hand.
Phase 3 replaces this module (and these tests) with the fitted surface and its golden tests.
"""

import math

import pytest

from optlab.domain.clock import MS_PER_DAY, MS_PER_HOUR
from optlab.volatility.rough import (
    atm_iv,
    iv_at_tenor,
    moving_average,
    realised_vol,
    strike_ivs,
    tau_years,
)
from tests.market_data.synthetic import utc_ms


def test_tau_is_act_365_to_the_0800_expiry() -> None:
    # 15 Nov 09:00 to 27 Nov 08:00 is 11 days 23 hours = 11.958333 days.
    now = utc_ms(2026, 11, 15, 9)
    expiry = utc_ms(2026, 11, 27, 8)
    assert tau_years(expiry, now) == pytest.approx((11 + 23 / 24) / 365, rel=1e-15)
    assert tau_years(now + 365 * MS_PER_DAY, now) == 1.0
    assert tau_years(now - MS_PER_HOUR, now) < 0


def test_strike_ivs_average_call_and_put_and_skip_missing() -> None:
    rows = [
        (64_000.0, 0.50),
        (64_000.0, 0.52),
        (66_000.0, 0.46),
        (66_000.0, None),
        (68_000.0, None),
    ]
    assert strike_ivs(rows) == [(64_000.0, pytest.approx(0.51)), (66_000.0, 0.46)]


def test_atm_iv_is_linear_in_strike_between_the_two_strikes_around_the_forward() -> None:
    points = [(66_000.0, 0.46), (64_000.0, 0.50), (70_000.0, 0.40)]
    # Halfway between 64,000 (0.50) and 66,000 (0.46): 0.48.
    assert atm_iv(points, 65_000.0) == pytest.approx(0.48)
    # A quarter of the way: 0.50 - 0.25 * 0.04 = 0.49.
    assert atm_iv(points, 64_500.0) == pytest.approx(0.49)
    # 66,000 (0.46) to 70,000 (0.40), at 67,000: 0.46 - 0.25 * 0.06 = 0.445.
    assert atm_iv(points, 67_000.0) == pytest.approx(0.445)
    assert atm_iv(points, 66_000.0) == pytest.approx(0.46)  # on a strike
    assert atm_iv(points, 63_999.0) is None  # outside the listed strikes: no guess
    assert atm_iv(points, 70_001.0) is None
    assert atm_iv([(65_000.0, 0.47)], 65_000.0) == pytest.approx(0.47)
    assert atm_iv([], 65_000.0) is None


def test_iv_at_tenor_interpolates_total_variance() -> None:
    # w = iv^2 * tau. 10 days at 0.50: w1 = 0.25 * 10/365 = 2.5/365. 40 days at 0.40:
    # w2 = 0.16 * 40/365 = 6.4/365. At 30 days, two thirds of the way in time:
    # w = (2.5 + (6.4 - 2.5) * 20/30) / 365 = 5.1/365, so iv = sqrt(5.1/30) = 0.4123105626.
    points = [(40 / 365, 0.40), (10 / 365, 0.50)]
    assert iv_at_tenor(points, 30 / 365) == pytest.approx(0.412310562561766, rel=1e-12)
    assert iv_at_tenor(points, 40 / 365) == pytest.approx(0.40)
    assert iv_at_tenor(points, 10 / 365) == pytest.approx(0.50)
    # A flat term structure stays flat.
    assert iv_at_tenor([(7 / 365, 0.45), (60 / 365, 0.45)], 30 / 365) == pytest.approx(0.45)
    # Outside the expiries there is nothing to interpolate between.
    assert iv_at_tenor(points, 41 / 365) is None
    assert iv_at_tenor(points, 9 / 365) is None
    assert iv_at_tenor([(30 / 365, 0.42)], 30 / 365) == pytest.approx(0.42)


def test_realised_vol_is_root_mean_square_daily_log_return_times_root_365() -> None:
    # Returns ln(110/100) = 0.0953102 and ln(99/110) = -0.1053605; mean square 0.0100924;
    # times 365 = 3.683744; square root = 1.9193068.
    assert realised_vol([100.0, 110.0, 99.0], 2) == pytest.approx(1.919306782593102, rel=1e-12)
    # Only the last `returns` returns count.
    assert realised_vol([50.0, 100.0, 110.0, 99.0], 2) == pytest.approx(1.919306782593102)
    assert realised_vol([70_000.0] * 31, 30) == 0.0
    assert realised_vol([100.0, 110.0], 2) is None  # needs returns + 1 prices
    # One 1% move a day, every day: 0.00995033 * sqrt(365) = 0.190102.
    prices = [100.0 * 1.01**i for i in range(31)]
    assert realised_vol(prices, 30) == pytest.approx(math.log(1.01) * math.sqrt(365))


def test_moving_average_of_the_last_n_prices() -> None:
    assert moving_average([1.0, 2.0, 3.0, 4.0], 2) == 3.5
    assert moving_average([1.0, 2.0, 3.0, 4.0], 4) == 2.5
    assert moving_average([1.0, 2.0], 3) is None
