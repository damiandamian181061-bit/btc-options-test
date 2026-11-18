"""The surface: per-expiry fits, then buckets by tenor and delta, interpolated in total variance."""

import math
from typing import Any

import numpy as np
import pytest

from optlab.domain.clock import MS_PER_DAY
from optlab.domain.market import Book
from optlab.volatility.surface import (
    BUCKETS,
    TENORS_DAYS,
    Bucket,
    Surface,
    SurfaceConfig,
    SurfacePoint,
    build_surface,
    interpolate_tenor,
)
from tests.volatility.helpers import T0, YEAR_MS, make_chain, make_futures, skew_smile

F = 65_000.0


def test_tenors_and_buckets_are_the_briefs() -> None:
    assert TENORS_DAYS == (1, 3, 7, 14, 30, 60, 90)
    assert [b.value for b in BUCKETS] == ["10d_put", "25d_put", "atm", "25d_call", "10d_call"]


def test_interpolation_is_linear_in_total_variance() -> None:
    nodes = [(7 / 365, 0.40), (30 / 365, 0.50)]
    res = interpolate_tenor(nodes, 14 / 365)
    assert res is not None
    iv, extrapolated, lo, hi = res
    w = 0.40**2 * 7 / 365 + (0.50**2 * 30 / 365 - 0.40**2 * 7 / 365) * (14 - 7) / (30 - 7)
    assert iv == pytest.approx(math.sqrt(w / (14 / 365)), rel=1e-12)
    assert (extrapolated, lo, hi) == (False, 0, 1)
    assert interpolate_tenor(nodes, 7 / 365) == (0.40, False, 0, 0)
    assert interpolate_tenor(nodes, 30 / 365) == (0.50, False, 1, 1)


def test_flat_iv_outside_the_listed_expiries() -> None:
    nodes = [(7 / 365, 0.40), (30 / 365, 0.50)]
    assert interpolate_tenor(nodes, 1 / 365) == (0.40, True, 0, 0)
    assert interpolate_tenor(nodes, 90 / 365) == (0.50, True, 1, 1)
    assert interpolate_tenor([], 30 / 365) is None


def _surface(book: Book = Book.LINEAR, **kw: Any) -> Surface:
    chain = make_chain(book=book, forward=F, **kw)
    futures = make_futures(chain, book=book)
    return build_surface(chain, futures, book, T0, index_price=F)


def _atm(s: Surface, days: int) -> SurfacePoint:
    p = s.point(days, Bucket.ATM)
    assert p is not None
    return p


def test_a_flat_smile_gives_the_same_iv_everywhere() -> None:
    s = _surface(days=(2.0, 9.0, 37.0, 100.0), smile=lambda k, t: 0.5)
    assert len(s.points) == len(TENORS_DAYS) * len(BUCKETS)
    for p in s.points:
        assert p.iv == pytest.approx(0.5, abs=2e-6), p
        assert p.total_variance == pytest.approx(0.25 * p.tenor_days / 365, rel=1e-5)
    # 1 day is before the first expiry (2 days): flat extrapolation, labelled.
    assert _atm(s, 1).extrapolated
    assert not _atm(s, 30).extrapolated
    assert not _atm(s, 90).extrapolated


def test_term_structure_atm_matches_total_variance_interpolation() -> None:
    def smile(k: float, t: float) -> float:
        return 0.40 + 0.5 * t  # rising with tenor, flat in strike

    s = _surface(days=(9.0, 37.0), smile=smile)
    t1, t2 = round(9 * MS_PER_DAY) / YEAR_MS, round(37 * MS_PER_DAY) / YEAR_MS
    v1, v2 = smile(0, t1), smile(0, t2)
    t = 30 / 365
    w = v1 * v1 * t1 + (v2 * v2 * t2 - v1 * v1 * t1) * (t - t1) / (t2 - t1)
    p = s.point(30, Bucket.ATM)
    assert p is not None
    assert p.iv == pytest.approx(math.sqrt(w / t), abs=1e-6)
    assert (p.lower_expiry_ms, p.upper_expiry_ms) == (s.fits[0].expiry_ms, s.fits[1].expiry_ms)


def test_buckets_follow_the_skew() -> None:
    s = _surface(days=(9.0, 37.0), smile=skew_smile(skew=-0.3, curve=0.4))
    for fit in s.fits:
        iv = fit.bucket_iv
        assert iv[Bucket.PUT_10] > iv[Bucket.PUT_25] > iv[Bucket.ATM]
        smile = fit.smile
        assert smile is not None
        assert iv[Bucket.ATM] == pytest.approx(float(smile.iv(0.0)))
        k25 = smile.k_for_delta(0.25, call=True)
        assert k25 is not None
        assert iv[Bucket.CALL_25] == pytest.approx(float(smile.iv(k25)))


def test_each_fit_records_its_forward_and_quality() -> None:
    s = _surface(days=(9.0, 37.0))
    for fit in s.fits:
        assert fit.forward.forward == F
        assert fit.forward.parity == pytest.approx(F, rel=1e-12)
        assert fit.smile is not None
        assert fit.n_points > 10
        assert fit.butterfly_min is not None
        assert fit.butterfly_min > 0
        assert fit.used
    assert s.fits[0].calendar_gap is None  # nothing before the first expiry
    assert s.fits[1].calendar_gap is not None
    assert s.fits[1].calendar_gap > 0


def test_inverse_book_gives_the_same_surface() -> None:
    lin = _surface(Book.LINEAR, days=(9.0, 37.0))
    inv = _surface(Book.INVERSE, days=(9.0, 37.0))
    for a, b in zip(lin.points, inv.points, strict=True):
        assert (a.tenor_days, a.bucket) == (b.tenor_days, b.bucket)
        assert a.iv == pytest.approx(b.iv, abs=1e-4)


def test_expiries_too_close_are_fitted_but_not_used() -> None:
    s = _surface(days=(0.2, 9.0, 37.0), strike_step=250.0, strikes_per_side=40)
    assert [f.used for f in s.fits] == [False, True, True]
    assert "under 12 h" in s.fits[0].note
    first = s.point(1, Bucket.ATM)
    assert first is not None
    assert first.extrapolated
    assert first.lower_expiry_ms == s.fits[1].expiry_ms


def test_calendar_arbitrage_is_flagged() -> None:
    s = _surface(days=(9.0, 37.0), smile=lambda k, t: 0.6 if t < 0.05 else 0.25)
    assert s.fits[1].calendar_gap is not None
    assert s.fits[1].calendar_gap < 0
    assert not s.fits[1].calendar_ok


def test_no_forward_no_fit() -> None:
    chain = make_chain(days=(9.0,), forward=F)
    chain["bid"] = None
    futures = make_futures(chain).iloc[0:0]
    s = build_surface(chain, futures, Book.LINEAR, T0, index_price=F)
    (fit,) = s.fits
    assert fit.smile is None
    assert not fit.used
    assert fit.note == "no forward"
    assert s.points == []


def test_config_min_hours() -> None:
    chain = make_chain(days=(0.2, 9.0), forward=F, strike_step=250.0, strikes_per_side=40)
    s = build_surface(
        chain,
        make_futures(chain),
        Book.LINEAR,
        T0,
        index_price=F,
        config=SurfaceConfig(min_tau_hours=1.0),
    )
    assert [f.used for f in s.fits] == [True, True]
    assert np.isfinite([p.iv for p in s.points]).all()
