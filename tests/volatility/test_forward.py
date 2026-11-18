"""Forwards per expiry: the dated future's mark, with put-call parity as fallback and check."""

import math

import numpy as np
import pandas as pd
import pytest

from optlab.domain.market import Book
from optlab.volatility.forward import ForwardSource, expiry_forwards, parity_forward
from tests.volatility.helpers import make_chain, make_futures


def test_parity_recovers_the_linear_forward() -> None:
    """C - P = F - K on the forward (undiscounted): each two-sided pair gives F exactly."""
    chain = make_chain(days=(9.0,), forward=65_432.1)
    f, pairs = parity_forward(chain, Book.LINEAR, near=65_000.0)
    assert pairs == 3
    assert f == pytest.approx(65_432.1, rel=1e-12)


def test_parity_recovers_the_inverse_forward() -> None:
    """BTC premiums: C - P = 1 - K/F, so F = K / (1 - (C - P))."""
    chain = make_chain(book=Book.INVERSE, days=(9.0,), forward=65_432.1)
    f, pairs = parity_forward(chain, Book.INVERSE, near=65_000.0)
    assert pairs == 3
    assert f == pytest.approx(65_432.1, rel=1e-12)


def test_parity_uses_the_strikes_nearest_the_forward_and_takes_the_median() -> None:
    chain = make_chain(days=(9.0,), forward=65_000.0, strike_step=1_000.0)
    # Spoil the 66,000 call's quote: one bad pair moves the median of three not at all.
    bad = (chain.strike == 66_000.0) & chain.call
    chain.loc[bad, ["bid", "ask"]] = chain.loc[bad, ["bid", "ask"]] * 1.5
    f, pairs = parity_forward(chain, Book.LINEAR, near=65_000.0, pairs=3)
    assert pairs == 3
    assert f == pytest.approx(65_000.0, rel=1e-12)


def test_parity_skips_one_sided_crossed_and_wide_pairs() -> None:
    chain = make_chain(days=(9.0,), forward=65_000.0)
    near = chain.strike.isin([64_000.0, 65_000.0, 66_000.0])
    chain.loc[near & chain.call & (chain.strike == 64_000.0), "bid"] = None  # one-sided
    crossed = near & ~chain.call & (chain.strike == 65_000.0)
    chain.loc[crossed, "bid"] = chain.loc[crossed, "ask"] * 1.1  # crossed
    wide = near & chain.call & (chain.strike == 66_000.0)
    chain.loc[wide, "ask"] = chain.loc[wide, "bid"] * 3  # far too wide
    # The three spoiled strikes are skipped; 67,000, 63,000 and 68,000 are the nearest clean ones.
    f, pairs = parity_forward(chain, Book.LINEAR, near=65_000.0, pairs=3)
    assert pairs == 3
    assert f == pytest.approx(65_000.0, rel=1e-12)


def test_parity_with_nothing_usable() -> None:
    chain = make_chain(days=(9.0,))
    chain["bid"] = None
    assert parity_forward(chain, Book.LINEAR, near=65_000.0) == (None, 0)


def test_future_mark_is_the_forward_and_parity_is_the_cross_check() -> None:
    chain = make_chain(days=(2.0, 9.0, 37.0), forwards=(65_010.0, 65_100.0, 65_500.0))
    futures = make_futures(chain)
    futures.loc[2, "mark_price"] = 65_530.0  # the future 30 USDC above the options' forward
    out = expiry_forwards(chain, futures, Book.LINEAR, index_price=65_000.0)
    assert [fw.source for fw in out.values()] == [ForwardSource.FUTURE] * 3
    last = out[int(futures.expiry_ms[2])]
    assert last.forward == 65_530.0
    assert last.future_name == futures.instrument_name[2]
    assert last.parity == pytest.approx(65_500.0, rel=1e-12)
    assert last.difference_bp == pytest.approx((65_530.0 / 65_500.0 - 1) * 1e4, rel=1e-9)


def test_parity_when_no_future_is_listed() -> None:
    chain = make_chain(book=Book.INVERSE, days=(2.0, 9.0), forwards=(65_010.0, 65_100.0))
    futures = make_futures(chain, book=Book.INVERSE).iloc[[0]]  # only the first expiry
    out = expiry_forwards(chain, futures, Book.INVERSE, index_price=65_000.0)
    first, second = out.values()
    assert first.source == ForwardSource.FUTURE
    assert second.source == ForwardSource.PARITY
    assert second.forward == pytest.approx(65_100.0, rel=1e-12)
    assert second.future is None
    assert second.difference_bp is None


def test_no_forward_at_all() -> None:
    chain = make_chain(days=(9.0,))
    chain["bid"] = None
    no_futures = pd.DataFrame(columns=["instrument_name", "expiry_ms", "mark_price", "bid", "ask"])
    out = expiry_forwards(chain, no_futures, Book.LINEAR, index_price=65_000.0)
    (only,) = out.values()
    assert only.source == ForwardSource.NONE
    assert only.forward is None


def test_a_future_without_a_mark_falls_back_to_parity() -> None:
    chain = make_chain(days=(9.0,), forward=65_100.0)
    futures = make_futures(chain)
    futures["mark_price"] = np.nan
    (only,) = expiry_forwards(chain, futures, Book.LINEAR, index_price=65_000.0).values()
    assert only.source == ForwardSource.PARITY
    assert math.isclose(only.forward or 0.0, 65_100.0, rel_tol=1e-12)
