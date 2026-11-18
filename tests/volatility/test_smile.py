"""Smile per expiry: quote selection, the smoothing spline, the butterfly and calendar checks."""

import math
from typing import Any

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from optlab.domain.clock import MS_PER_DAY
from optlab.domain.market import Book
from optlab.volatility.smile import (
    SmileConfig,
    calendar_gap,
    fit_smile,
    smile_points,
)
from tests.volatility.helpers import YEAR_MS, make_chain, skew_smile

F = 65_000.0


def _one_expiry(
    book: Book = Book.LINEAR, days: float = 9.0, **kw: Any
) -> tuple[pd.DataFrame, float]:
    chain = make_chain(book=book, days=(days,), forward=F, **kw)
    return chain, round(days * MS_PER_DAY) / YEAR_MS


def test_points_are_out_of_the_money_with_their_true_iv() -> None:
    chain, tau = _one_expiry()
    pts = smile_points(chain, F, tau, Book.LINEAR)
    assert (pts.k == np.log(pts.strike / F)).all()
    assert (~pts.call[pts.strike < F]).all()
    assert pts.call[pts.strike >= F].all()
    assert pts.strike.is_unique
    truth = np.array([skew_smile()(k, tau) for k in pts.k])
    # Symmetric made-up spreads: the mid is the model price, so the mid IV is exact.
    assert np.allclose(pts.mid_iv, truth, atol=1e-9)
    assert (pts.bid_iv < pts.mid_iv).all()
    assert (pts.ask_iv > pts.mid_iv).all()
    assert (pts.weight > 0).all()


def test_inverse_points_use_the_btc_premium_times_the_forward() -> None:
    lin, tau = _one_expiry(Book.LINEAR)
    inv, _ = _one_expiry(Book.INVERSE)
    a = smile_points(lin, F, tau, Book.LINEAR).set_index("strike")
    b = smile_points(inv, F, tau, Book.INVERSE).set_index("strike")
    common = a.index.intersection(b.index)
    assert len(common) > 15
    assert np.allclose(a.loc[common, "mid_iv"], b.loc[common, "mid_iv"], atol=1e-9)


def test_unusable_quotes_are_left_out() -> None:
    chain, tau = _one_expiry()
    otm_call = chain.call & (chain.strike > F)
    k1, k2, k3, k4 = sorted(chain.loc[otm_call, "strike"])[:4]
    chain.loc[otm_call & (chain.strike == k1), "bid"] = None  # one-sided
    sel = otm_call & (chain.strike == k2)
    chain.loc[sel, "bid"] = chain.loc[sel, "ask"] * 1.05  # crossed
    sel = otm_call & (chain.strike == k3)
    chain.loc[sel, "ask"] = chain.loc[sel, "bid"] * 4  # wider than 100% of mid
    chain.loc[otm_call & (chain.strike == k4), "state"] = "locked"
    pts = smile_points(chain, F, tau, Book.LINEAR)
    assert not set(pts.strike) & {k1, k2, k3, k4}


def test_far_wing_quotes_at_a_tick_or_two_are_left_out() -> None:
    """A 5 USDC tick on a 1-USDC option says nothing about its vol (iv_error over 2 points)."""
    chain, tau = _one_expiry(strikes_per_side=30)
    pts = smile_points(chain, F, tau, Book.LINEAR)
    kept_far = pts.strike.max()
    assert kept_far < chain.strike.max()
    cfg = SmileConfig(max_iv_error=1.0)  # accept anything solvable
    assert smile_points(chain, F, tau, Book.LINEAR, cfg).strike.max() > kept_far


def test_spline_reproduces_a_clean_smile() -> None:
    chain, tau = _one_expiry()
    pts = smile_points(chain, F, tau, Book.LINEAR)
    s = fit_smile(pts, expiry_ms=1, tau=tau, forward=F)
    assert s is not None
    assert s.n_points == len(pts)
    assert s.rmse < 1e-4
    truth = skew_smile()
    grid = np.linspace(s.k_min, s.k_max, 50)
    assert np.allclose(s.iv(grid), [truth(k, tau) for k in grid], atol=5e-4)


def test_spline_smooths_noise() -> None:
    rng = np.random.default_rng(7)
    chain, tau = _one_expiry(strikes_per_side=20, strike_step=500.0)
    pts = smile_points(chain, F, tau, Book.LINEAR)
    noise = rng.normal(0, 0.003, len(pts))  # 0.3 vol points
    pts = pts.assign(mid_iv=pts.mid_iv + noise)
    s = fit_smile(pts, expiry_ms=1, tau=tau, forward=F)
    assert s is not None
    truth = np.array([skew_smile()(k, tau) for k in pts.k])
    fitted = s.iv(pts.k.to_numpy())
    # Closer to the true smile than the noisy quotes are.
    assert np.sqrt(np.mean((fitted - truth) ** 2)) < np.sqrt(np.mean(noise**2))


def test_too_few_points_gives_no_smile() -> None:
    chain, tau = _one_expiry()
    pts = smile_points(chain, F, tau, Book.LINEAR).iloc[:4]
    assert fit_smile(pts, expiry_ms=1, tau=tau, forward=F) is None


def test_flat_outside_the_quoted_range() -> None:
    chain, tau = _one_expiry()
    s = fit_smile(smile_points(chain, F, tau, Book.LINEAR), expiry_ms=1, tau=tau, forward=F)
    assert s is not None
    assert float(s.iv(s.k_min - 0.5)) == pytest.approx(float(s.iv(s.k_min)), abs=1e-15)
    assert float(s.iv(s.k_max + 0.5)) == pytest.approx(float(s.iv(s.k_max)), abs=1e-15)
    assert float(s.total_variance(0.0)) == pytest.approx(float(s.iv(0.0)) ** 2 * tau)


def test_no_butterfly_arbitrage_on_a_sensible_smile() -> None:
    chain, tau = _one_expiry()
    s = fit_smile(smile_points(chain, F, tau, Book.LINEAR), expiry_ms=1, tau=tau, forward=F)
    assert s is not None
    assert s.butterfly_min() > 0


def test_butterfly_arbitrage_is_detected() -> None:
    """A smile with a sharp dip just out of the money implies a negative density there."""
    chain, tau = _one_expiry(strikes_per_side=16, strike_step=500.0)
    pts = smile_points(chain, F, tau, Book.LINEAR)
    dip = (pts.k > 0.02) & (pts.k < 0.04)
    pts.loc[dip, "mid_iv"] -= 0.12
    s = fit_smile(pts, expiry_ms=1, tau=tau, forward=F, config=SmileConfig(lam=1e-12))
    assert s is not None
    assert s.butterfly_min() < 0


def test_strike_for_a_delta_on_a_flat_smile() -> None:
    """Flat 50%: the 25-delta call has N(d1) = 0.25, so k = 0.6745 s + s^2/2, s = 0.5 sqrt(tau)."""
    chain, tau = _one_expiry(smile=lambda k, t: 0.5)
    s = fit_smile(smile_points(chain, F, tau, Book.LINEAR), expiry_ms=1, tau=tau, forward=F)
    assert s is not None
    sd = 0.5 * math.sqrt(tau)
    z = float(norm.ppf(0.75))
    assert s.k_for_delta(0.25, call=True) == pytest.approx(z * sd + sd * sd / 2, abs=1e-6)
    assert s.k_for_delta(0.25, call=False) == pytest.approx(-z * sd + sd * sd / 2, abs=1e-6)
    k10, k25 = s.k_for_delta(0.10, call=True), s.k_for_delta(0.25, call=True)
    assert k10 is not None
    assert k25 is not None
    assert k10 > k25


def test_calendar_gap() -> None:
    """Total variance must not fall with time at the same log-moneyness."""
    near, t1 = _one_expiry(days=9.0, smile=lambda k, t: 0.5)
    far, t2 = _one_expiry(days=30.0, smile=lambda k, t: 0.5)
    low, _ = _one_expiry(days=30.0, smile=lambda k, t: 0.2)
    s1 = fit_smile(smile_points(near, F, t1, Book.LINEAR), expiry_ms=1, tau=t1, forward=F)
    s2 = fit_smile(smile_points(far, F, t2, Book.LINEAR), expiry_ms=2, tau=t2, forward=F)
    s3 = fit_smile(smile_points(low, F, t2, Book.LINEAR), expiry_ms=2, tau=t2, forward=F)
    assert s1 is not None
    assert s2 is not None
    assert s3 is not None
    assert calendar_gap(s1, s2) == pytest.approx(0.25 * (t2 - t1), rel=1e-3)
    bad = calendar_gap(s1, s3)
    assert bad is not None
    assert bad < 0  # 20% for 30 days < 50% for 9 days: arbitrage
