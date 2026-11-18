"""Realised vol from daily closes, and the per-minute features."""

import math
from datetime import date, timedelta

import numpy as np
import pytest

from optlab.domain.clock import MS_PER_DAY
from optlab.domain.market import Book
from optlab.volatility.chain import Perpetual
from optlab.volatility.features import annualised_basis, compute_features
from optlab.volatility.realised import (
    CloseSource,
    DailyClose,
    close_ms,
    closes_known_at,
    merge_closes,
    realised_vol,
)
from optlab.volatility.surface import build_surface
from tests.volatility.helpers import T0, make_chain, make_futures

D0 = date(2026, 11, 1)


def closes(prices: list[float], source: CloseSource = CloseSource.INDEX) -> list[DailyClose]:
    return [DailyClose(D0 + timedelta(days=i), p, source) for i, p in enumerate(prices)]


def test_realised_vol_by_hand() -> None:
    """Closes 100, 110, 99, 99: returns ln 1.1 = 0.0953102, ln 0.9 = -0.1053605, 0.
    mean r^2 = (0.00908403 + 0.01110084 + 0) / 3 = 0.00672829; x 365 = 2.455826;
    sqrt = 1.567107 (157% a year: a made-up, very jumpy price)."""
    rv = realised_vol(closes([100, 110, 99, 99]), 3)
    assert rv is not None
    assert rv.vol == pytest.approx(1.567107, abs=1e-6)
    assert (rv.returns, rv.from_delivery) == (3, 0)


def test_realised_vol_uses_the_last_n_returns_only() -> None:
    rv = realised_vol(closes([50, 100, 110, 99, 99]), 3)
    assert rv is not None
    assert rv.vol == pytest.approx(1.567107, abs=1e-6)
    assert realised_vol(closes([100, 110, 99]), 3) is None  # needs n + 1 closes


def test_realised_vol_needs_consecutive_days() -> None:
    c = closes([100, 110, 99, 99])
    gap = [c[0], c[1], DailyClose(c[2].day + timedelta(days=1), 99, CloseSource.INDEX), c[3]]
    gap[3] = DailyClose(gap[2].day + timedelta(days=1), 99, CloseSource.INDEX)
    assert realised_vol(gap, 3) is None


def test_merge_prefers_our_own_index_close() -> None:
    index = {D0 + timedelta(days=2): 101.0, D0 + timedelta(days=3): 102.0}
    delivery = {D0 + timedelta(days=i): 90.0 + i for i in range(4)}
    merged = merge_closes(index, delivery)
    assert [(c.price, c.source) for c in merged] == [
        (90.0, CloseSource.DELIVERY),
        (91.0, CloseSource.DELIVERY),
        (101.0, CloseSource.INDEX),
        (102.0, CloseSource.INDEX),
    ]
    rv = realised_vol(merged, 3)
    assert rv is not None
    assert rv.from_delivery == 2


def test_closes_known_at_the_minute() -> None:
    c = closes([100, 101, 102])
    last = c[-1].day
    assert close_ms(last) % MS_PER_DAY == 8 * 3_600_000
    assert closes_known_at(c, close_ms(last) - 1) == c[:2]
    assert closes_known_at(c, close_ms(last)) == c


def test_annualised_basis() -> None:
    """ln(F/S) / tau at 30 days, interpolated in tau; flat rate outside the expiries."""
    s = 65_000.0
    nodes = [(10 / 365, s * math.exp(0.04 * 10 / 365)), (40 / 365, s * math.exp(0.07 * 40 / 365))]
    want = (0.04 * 10 / 365 + (0.07 * 40 - 0.04 * 10) / 365 * (30 - 10) / (40 - 10)) / (30 / 365)
    assert annualised_basis(nodes, s, 30 / 365) == pytest.approx(want, rel=1e-12)
    assert annualised_basis(nodes, s, 5 / 365) == pytest.approx(0.04, rel=1e-9)
    assert annualised_basis(nodes, s, 90 / 365) == pytest.approx(0.07, rel=1e-9)
    assert annualised_basis([], s, 30 / 365) is None
    assert annualised_basis(nodes, None, 30 / 365) is None


def test_features_on_a_flat_surface() -> None:
    s0, r = 65_000.0, 0.05
    days = (3.0, 9.0, 37.0, 100.0)
    fwd = [s0 * math.exp(r * d / 365) for d in days]
    chain = make_chain(days=days, forwards=fwd, smile=lambda k, t: 0.5)
    surface = build_surface(chain, make_futures(chain), Book.LINEAR, T0, index_price=s0)
    hist = closes([100 * math.exp(0.01 * ((-1) ** i)) for i in range(31)])  # +-1% every day
    perp = Perpetual(mark_price=s0 + 5, funding_8h=0.0001, current_funding=0.00002)
    f = compute_features(surface, hist, s0, perp)
    assert f.ts_ms == T0
    assert f.book == Book.LINEAR
    assert f.index_price == s0
    for v in (f.atm_iv_7d, f.atm_iv_30d, f.atm_iv_90d):
        assert v == pytest.approx(0.5, abs=2e-6)
    for v in (f.rr25_30d, f.bf25_30d, f.term_slope_7_30, f.term_slope_30_90):
        assert v == pytest.approx(0.0, abs=5e-6)
    # Every daily return is +-2% (log), so RV = 0.02 sqrt(365) whatever the window.
    assert f.rv_7d == pytest.approx(0.02 * math.sqrt(365), rel=1e-9)
    assert f.rv_30d == pytest.approx(0.02 * math.sqrt(365), rel=1e-9)
    assert (f.rv_30d_returns, f.rv_30d_delivery) == (30, 0)
    assert f.iv_rv_30d == pytest.approx(0.5 - 0.02 * math.sqrt(365), abs=2e-6)
    assert f.basis_30d == pytest.approx(r, rel=1e-6)
    assert f.perp_funding_8h == 0.0001


def test_features_without_enough_history() -> None:
    chain = make_chain(days=(9.0, 37.0))
    surface = build_surface(chain, make_futures(chain), Book.LINEAR, T0, index_price=65_000.0)
    f = compute_features(surface, closes([100.0] * 5), 65_000.0, None)
    assert f.rv_30d is None
    assert f.iv_rv_30d is None
    assert f.rv_30d_returns == 4
    assert f.rv_7d is None
    assert f.perp_funding_8h is None
    assert f.atm_iv_30d is not None
    assert np.isfinite(f.atm_iv_30d)
