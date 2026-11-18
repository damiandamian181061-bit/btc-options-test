"""Implied vol solver: round trips, no-arbitrage bounds, flags, and the Brent fallback."""

import math

import numpy as np
import pytest

from optlab.pricing import black76 as b76
from optlab.pricing.iv import IVStatus, implied_vol

DAY = 1.0 / 365.0
F = 64_000.0

ROUND_TRIP = [
    (k, t, s, call)
    for k in (30_000.0, 48_000.0, 58_000.0, 63_500.0, 64_000.0, 64_500.0, 70_000.0, 90_000.0)
    for t in (0.25 * DAY, 2 * DAY, 9 * DAY, 30 * DAY, 130 * DAY, 2.0)
    for s in (0.08, 0.3, 0.55, 1.2, 3.0)
    for call in (True, False)
]


@pytest.mark.parametrize(("k", "t", "s", "call"), ROUND_TRIP)
def test_round_trip_price_iv_price(k: float, t: float, s: float, call: bool) -> None:
    p = float(b76.price(F, k, t, s, call))
    r = implied_vol(p, F, k, t, call)
    vega = float(b76.greeks(F, k, t, s, call).vega)
    time_value = p - max((F - k) if call else (k - F), 0.0)
    if time_value <= 1e-9 * F:
        # So far out that double precision keeps (almost) no time value: either no IV is
        # found, or the one found prices back to the same number.
        assert r.status[()] != IVStatus.NOT_CONVERGED
        if r.ok.all():
            assert abs(float(b76.price(F, k, t, float(r.iv), call)) - p) <= 1e-9 * F
        return
    assert r.status[()] == IVStatus.OK, (p, time_value)
    back = float(b76.price(F, k, t, float(r.iv), call))
    assert math.isclose(back, p, rel_tol=1e-10, abs_tol=1e-9)
    if vega > 0.01:  # the IV itself is pinned down only where the price moves with it
        assert abs(float(r.iv) - s) < 1e-7


def test_whole_chain_at_once_with_mixed_statuses() -> None:
    strikes = np.array([50_000.0, 60_000.0, 64_000.0, 70_000.0, 80_000.0, 64_000.0])
    calls = np.array([False, False, True, True, True, True])
    tau = np.array([10 * DAY] * 5 + [0.0])
    sig = np.array([0.6, 0.45, 0.4, 0.42, 0.55, 0.4])
    prices = b76.price(F, strikes, np.maximum(tau, 1e-9), sig, calls)
    prices[1] = np.nan
    r = implied_vol(prices, F, strikes, tau, calls)
    assert list(r.status) == ["ok", "no_price", "ok", "ok", "ok", "no_time"]
    assert np.allclose(r.iv[[0, 2, 3, 4]], sig[[0, 2, 3, 4]], atol=1e-9)
    assert np.isnan(r.iv[[1, 5]]).all()
    assert r.ok.tolist() == [True, False, True, True, True, False]


def test_no_arbitrage_bounds() -> None:
    t = 20 * DAY
    # A call is worth at least F - K and less than F; a put at least K - F and less than K.
    cases = [
        (4_000.0, 60_000.0, True, IVStatus.BELOW_INTRINSIC),  # exactly intrinsic
        (3_999.0, 60_000.0, True, IVStatus.BELOW_INTRINSIC),
        (F, 60_000.0, True, IVStatus.ABOVE_MAXIMUM),
        (70_000.0, 70_000.0, False, IVStatus.ABOVE_MAXIMUM),
        (5_999.0, 70_000.0, False, IVStatus.BELOW_INTRINSIC),
        (0.0, 70_000.0, True, IVStatus.NO_PRICE),
        (-1.0, 70_000.0, True, IVStatus.NO_PRICE),
    ]
    for p, k, call, status in cases:
        r = implied_vol(p, F, k, t, call)
        assert r.status[()] == status, (p, k, call)
        assert math.isnan(float(r.iv))


def test_price_beyond_the_solver_range_is_out_of_range() -> None:
    t = 30 * DAY
    p = float(b76.price(F, F, t, 14.0, True))  # a 1400% vol: above the 1000% ceiling
    assert implied_vol(p, F, F, t, True).status[()] == IVStatus.OUT_OF_RANGE
    p = float(b76.price(F, F, t, 0.00005, True))  # 0.005%: below the 0.01% floor
    assert p > 0
    assert implied_vol(p, F, F, t, True).status[()] == IVStatus.OUT_OF_RANGE


def test_brent_fallback_gives_the_same_answer() -> None:
    strikes = np.array([40_000.0, 60_000.0, 64_000.0, 72_000.0, 110_000.0])
    sig = np.array([1.1, 0.5, 0.45, 0.5, 0.9])
    calls = np.array([False, False, True, True, True])
    prices = b76.price(F, strikes, 25 * DAY, sig, calls)
    newton = implied_vol(prices, F, strikes, 25 * DAY, calls)
    brent = implied_vol(prices, F, strikes, 25 * DAY, calls, max_newton=0)
    assert not newton.brent.any()
    assert brent.brent.all()
    assert np.allclose(newton.iv, sig, atol=1e-9)
    assert np.allclose(brent.iv, sig, atol=1e-9)


def test_quotes_too_coarse_to_pin_the_iv_are_flagged() -> None:
    """A price known only to the nearest 2 USDC: fine at the money, useless in the far wing."""
    t = 9 * DAY
    atm = float(b76.price(F, F, t, 0.4, True))
    wing = float(b76.price(F, 100_000.0, t, 0.9, True))
    r = implied_vol(np.array([atm, wing]), F, np.array([F, 100_000.0]), t, True, price_step=2.0)
    assert r.ok.all()
    # iv_error: how far the IV moves for a price error of one price_step, as a decimal.
    vega_atm = float(b76.greeks(F, F, t, 0.4, True).vega) * 100  # per unit of sigma
    assert float(r.iv_error[0]) == pytest.approx(2.0 / vega_atm, rel=1e-6)
    assert r.iv_error[0] < 0.001  # under 0.1 vol point
    assert r.iv_error[1] > 0.01  # over 1 vol point
    assert r.unreliable.tolist() == [False, True]
    assert r.vega[0] == pytest.approx(vega_atm / 100, rel=1e-9)


def test_deribit_rounding_example() -> None:
    """Deribit rounds BTC_USDC mark prices to 0.01 USDC and mark IVs to 0.01 vol points.

    Re-solving a rounded price gives back the IV to within the IV rounding plus the price
    rounding divided by vega. Made-up quote: F 64,321.55, K 60,000 put, 9.4 days, 46.12%.
    """
    f, k, t, s = 64_321.55, 60_000.0, 9.4 * DAY, 0.4612
    mark = round(float(b76.price(f, k, t, s, False)), 2)
    r = implied_vol(mark, f, k, t, False, price_step=0.01)
    assert r.ok.all()
    assert abs(float(r.iv) - s) <= 0.00005 + float(r.iv_error)
