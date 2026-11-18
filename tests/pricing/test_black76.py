"""Black-76 on the forward: golden values from btc-vol-models, finite differences, parity.

The golden file is produced by research/btc_vol_models_golden.py from Damian's btc-vol-models
(the commit is in the file). Its Black-Scholes with r = 0 and S0 = F is Black-76 undiscounted.
"""

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from optlab.pricing import black76 as b76
from optlab.pricing.iv import implied_vol

GOLDEN = json.loads((Path(__file__).parent / "golden" / "btc_vol_models.json").read_text())
DAY = 1.0 / 365.0


def _close(ours: float, ref: float, rel: float, abs_: float) -> bool:
    return abs(ours - ref) <= abs_ + rel * abs(ref)


def test_golden_file_names_its_source_commit() -> None:
    assert GOLDEN["commit"] == "4bb1cb1f670ff42eda07f3b31d01b0c720cb175a"
    assert len(GOLDEN["grid"]) == 432
    assert len(GOLDEN["greeks_table"]) == 21


@pytest.mark.parametrize("case", GOLDEN["grid"], ids=lambda c: f"{c['strike']}-{c['option_type']}")
def test_matches_btc_vol_models_grid(case: dict[str, Any]) -> None:
    f, k, t, s = case["forward"], case["strike"], case["tau"], case["sigma"]
    call = case["option_type"] == "call"
    g = b76.greeks(f, k, t, s, call)
    # Price: closed form on both sides, so it agrees to rounding.
    assert _close(float(g.price), case["price"], 1e-11, 1e-9)
    # btc-vol-models' Greeks are central differences (1 bp of spot, 0.01 vol point): they agree
    # with the closed form to the bump's truncation error, h^2/6 times the third derivative.
    # That is largest for 1-day wings (up to about 1e-4 relative for delta) and deep in the
    # money at low vol, where vega bends sharply with sigma (5e-6 for K = 0.7 F, 30 days, 30%).
    # Our closed form itself is held to 1e-7 by the five-point test below.
    assert _close(float(g.delta), case["delta"], 2e-4, 1e-7)
    assert _close(float(g.gamma), case["gamma"], 1e-4, 1e-12)
    assert _close(float(g.vega), case["vega"], 1e-4, 1e-8)
    # Its theta is the change over one calendar day; ours is the instantaneous rate per day.
    one_day = float(b76.price(f, k, max(t - DAY, 1e-6), s, call) - g.price)
    assert _close(one_day, case["theta_one_day"], 1e-9, 1e-8)
    # The IV is pinned down only where the price moves with it: far in or out of the money the
    # time value is ~0 and any IV prices back to the same number (we then report no IV).
    if case["implied_vol"] is not None and float(g.vega) > 0.01:
        ours = implied_vol(case["price"], f, k, t, call)
        assert ours.ok.all()
        assert abs(float(ours.iv) - case["implied_vol"]) < 1e-6


def test_matches_btc_vol_models_greeks_table() -> None:
    """Its committed study output (S0 = 60,000, r = 0, one calibrated flat vol for every row)."""
    rows = GOLDEN["greeks_table"]
    f = GOLDEN["greeks_table_spot"]
    first = rows[0]
    sigma = float(
        implied_vol(
            first["price"], f, first["strike"], first["tau"], first["option_type"] == "call"
        ).iv
    )
    assert 0.1 < sigma < 2.0
    for row in rows:
        call = row["option_type"] == "call"
        g = b76.greeks(f, row["strike"], row["tau"], sigma, call)
        assert _close(float(g.price), row["price"], 1e-9, 1e-7), row
        assert _close(float(g.delta), row["delta"], 2e-4, 1e-7), row
        assert _close(float(g.gamma), row["gamma"], 1e-4, 1e-12), row
        assert _close(float(g.vega), row["vega"], 1e-4, 1e-8), row
        one_day = float(b76.price(f, row["strike"], row["tau"] - DAY, sigma, call) - g.price)
        assert _close(one_day, row["theta_one_day"], 1e-8, 1e-7), row


CASES = [
    (64_000.0, k, t, s, call)
    for k in (40_000.0, 56_000.0, 62_000.0, 64_000.0, 66_000.0, 72_000.0, 100_000.0)
    for t in (2 * DAY, 9 * DAY, 30 * DAY, 130 * DAY)
    for s in (0.25, 0.45, 0.9)
    for call in (True, False)
]


@pytest.mark.parametrize(("f", "k", "t", "s", "call"), CASES)
def test_greeks_match_finite_differences(
    f: float, k: float, t: float, s: float, call: bool
) -> None:
    """Five-point stencils (truncation error of order h^4), so the comparison can be tight even
    for short-dated wings, where a three-point difference is off by about 1e-4 relative."""
    g = b76.greeks(f, k, t, s, call)
    # Rounding in a difference quotient: about eps * price / bump, so tolerances scale with it.
    noise = 100 * np.finfo(float).eps * max(float(g.price), 1.0)

    def first(fn: Any, x: float, h: float) -> float:
        return float((-fn(x + 2 * h) + 8 * fn(x + h) - 8 * fn(x - h) + fn(x - 2 * h)) / (12 * h))

    def second(fn: Any, x: float, h: float) -> float:
        a = -fn(x + 2 * h) + 16 * fn(x + h) - 30 * fn(x) + 16 * fn(x - h) - fn(x - 2 * h)
        return float(a / (12 * h * h))

    def by_f(x: float) -> float:
        return float(b76.price(x, k, t, s, call))

    def by_s(x: float) -> float:
        return float(b76.price(f, k, t, x, call))

    def by_t(x: float) -> float:
        return float(b76.price(f, k, x, s, call))

    h = f * s * math.sqrt(t) * 1e-3  # a small step on the scale the price moves on
    assert math.isclose(float(g.delta), first(by_f, f, h), rel_tol=1e-7, abs_tol=noise / h)
    assert math.isclose(float(g.gamma), second(by_f, f, h), rel_tol=1e-5, abs_tol=10 * noise / h**2)
    # Vega in Deribit's units: per 1 vol point (0.01 of sigma).
    hv = s * 1e-3
    assert math.isclose(
        float(g.vega), first(by_s, s, hv) * 0.01, rel_tol=1e-7, abs_tol=noise / hv * 0.01
    )
    # Theta in Deribit's units: per calendar day, the instantaneous rate (-dV/dtau / 365).
    ht = t * 1e-3
    assert math.isclose(
        float(g.theta), -first(by_t, t, ht) / 365, rel_tol=1e-7, abs_tol=noise / ht / 365
    )


@pytest.mark.parametrize(("f", "k", "t", "s", "call"), [c for c in CASES if c[4]])
def test_put_call_parity(f: float, k: float, t: float, s: float, call: bool) -> None:
    c = float(b76.price(f, k, t, s, True))
    p = float(b76.price(f, k, t, s, False))
    assert math.isclose(c - p, f - k, rel_tol=0, abs_tol=1e-8 * f)
    gc, gp = b76.greeks(f, k, t, s, True), b76.greeks(f, k, t, s, False)
    assert math.isclose(float(gc.delta - gp.delta), 1.0, abs_tol=1e-12)
    assert float(gc.gamma) == pytest.approx(float(gp.gamma), rel=1e-12)
    assert float(gc.vega) == pytest.approx(float(gp.vega), rel=1e-12)
    assert float(gc.theta) == pytest.approx(float(gp.theta), rel=1e-12)


def test_inverse_premium_is_the_usd_price_over_the_forward() -> None:
    """Deribit's inverse options: the BTC premium is the USD price divided by the forward."""
    f, k, t, s = 65_000.0, 60_000.0, 20 * DAY, 0.5
    for call in (True, False):
        usd = float(b76.price(f, k, t, s, call))
        assert float(b76.price_inverse(f, k, t, s, call)) == pytest.approx(usd / f, rel=1e-14)
    # Parity in BTC: C - P = 1 - K/F.
    c = float(b76.price_inverse(f, k, t, s, True))
    p = float(b76.price_inverse(f, k, t, s, False))
    assert c - p == pytest.approx(1 - k / f, abs=1e-14)


def test_hand_worked_atm_example() -> None:
    """F = K = 64,000, 30 days, 50%: every number by hand.

    sqrt(tau) = sqrt(30/365) = 0.286691; s = 0.5 * 0.286691 = 0.143346
    d1 = s/2 = 0.071673, d2 = -0.071673; N(d1) = 0.528569; n(d1) = 0.397919
    price = F (N(d1) - N(d2)) = 64,000 * (2 * 0.528569 - 1) = 3,656.81 USDC
    delta = N(d1) = 0.528569
    vega = F n(d1) sqrt(tau) / 100 = 64,000 * 0.397919 * 0.286691 / 100 = 73.011 USDC a vol point
    theta = -F n(d1) sigma / (2 sqrt(tau)) / 365 = -22,207.3 / 365 = -60.842 USDC a day
    gamma = n(d1) / (F s) = 0.397919 / (64,000 * 0.143346) = 4.3374e-5 per USDC
    """
    g = b76.greeks(64_000.0, 64_000.0, 30 / 365, 0.5, True)
    assert float(g.price) == pytest.approx(3656.81, abs=0.01)
    assert float(g.delta) == pytest.approx(0.528569, abs=1e-6)
    assert float(g.vega) == pytest.approx(73.011, abs=0.001)
    assert float(g.theta) == pytest.approx(-60.842, abs=0.001)
    assert float(g.gamma) == pytest.approx(4.3374e-5, rel=1e-4)


def test_expired_or_zero_vol_is_intrinsic() -> None:
    f = 64_000.0
    for t, s in ((0.0, 0.5), (-1.0, 0.5), (0.1, 0.0)):
        assert float(b76.price(f, 60_000.0, t, s, True)) == 4_000.0
        assert float(b76.price(f, 60_000.0, t, s, False)) == 0.0
        assert float(b76.price(f, 70_000.0, t, s, False)) == 6_000.0
        g = b76.greeks(f, 60_000.0, t, s, True)
        assert (float(g.delta), float(g.gamma), float(g.vega), float(g.theta)) == (1, 0, 0, 0)
        assert float(b76.greeks(f, 70_000.0, t, s, False).delta) == -1.0
        assert float(b76.greeks(f, 70_000.0, t, s, True).delta) == 0.0


def test_vectorised_over_a_chain() -> None:
    strikes = np.array([50_000.0, 60_000.0, 64_000.0, 70_000.0, 80_000.0])
    calls = np.array([False, False, True, True, True])
    g = b76.greeks(64_000.0, strikes, 10 * DAY, np.full(5, 0.4), calls)
    assert g.price.shape == (5,)
    for i in range(5):
        one = b76.greeks(64_000.0, strikes[i], 10 * DAY, 0.4, bool(calls[i]))
        assert float(g.delta[i]) == float(one.delta)
        assert float(g.price[i]) == float(one.price)


def test_option_type_from_text() -> None:
    assert b76.is_call("call") is True
    assert b76.is_call("put") is False
    with pytest.raises(ValueError, match="option type"):
        b76.is_call("straddle")
