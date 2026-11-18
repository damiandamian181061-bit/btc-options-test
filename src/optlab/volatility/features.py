"""Features every minute, per book, from the surface and data at or before the minute.

- ``atm_iv_7d``, ``atm_iv_30d``, ``atm_iv_90d``: ATM-forward IV at 7, 30 and 90 days.
- ``rr25_30d``: 25-delta risk reversal at 30 days, IV(25d call) - IV(25d put). Negative when
  puts are dearer (downside skew).
- ``bf25_30d``: 25-delta butterfly at 30 days, (IV(25d call) + IV(25d put)) / 2 - ATM IV.
- ``term_slope_7_30`` and ``term_slope_30_90``: ATM IV at the longer tenor minus the shorter.
- ``rv_7d``, ``rv_30d``: close-to-close realised vol (``optlab.volatility.realised``);
  ``rv_30d_returns`` counts the daily returns available (of 30) and ``rv_30d_delivery`` the
  closes that were Deribit delivery prices rather than our own 08:00 index snapshot.
- ``iv_rv_30d``: ATM 30-day IV minus 30-day realised vol (the variance risk premium proxy).
- ``basis_30d``: the forward's annualised carry at 30 days, ln(F/S) / tau, with ln(F/S)
  interpolated linearly in tau between the expiries' forwards (flat rate outside them).
- ``perp_funding_8h``: the perpetual's funding rate over the last 8 hours, as Deribit reports.

IVs and vols are decimals (0.45 = 45%); the basis is a decimal rate a year.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from optlab.domain.market import Book
from optlab.volatility.chain import Perpetual
from optlab.volatility.realised import DailyClose, available_returns, realised_vol
from optlab.volatility.surface import Bucket, Surface


@dataclass(frozen=True, slots=True)
class Features:
    ts_ms: int
    book: Book
    index_price: float | None
    atm_iv_7d: float | None
    atm_iv_30d: float | None
    atm_iv_90d: float | None
    rr25_30d: float | None
    bf25_30d: float | None
    term_slope_7_30: float | None
    term_slope_30_90: float | None
    rv_7d: float | None
    rv_30d: float | None
    rv_30d_returns: int
    rv_30d_delivery: int
    iv_rv_30d: float | None
    basis_30d: float | None
    perp_funding_8h: float | None


def _minus(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


def annualised_basis(
    forwards: Sequence[tuple[float, float]], spot: float | None, tau: float
) -> float | None:
    """ln(F/S)/tau at ``tau`` from (tau, forward) pairs: ln(F/S) linear in tau between them."""
    nodes = sorted((t, math.log(f / spot)) for t, f in forwards if t > 0 and f > 0) if spot else []
    if not nodes or not tau > 0:
        return None
    if tau <= nodes[0][0]:
        return nodes[0][1] / nodes[0][0]
    if tau >= nodes[-1][0]:
        return nodes[-1][1] / nodes[-1][0]
    for (t0, c0), (t1, c1) in pairwise(nodes):
        if t0 <= tau <= t1:
            return (c0 + (c1 - c0) * (tau - t0) / (t1 - t0)) / tau
    return None  # unreachable: tau is inside the nodes


def compute_features(
    surface: Surface,
    closes: Sequence[DailyClose],
    index_price: float | None,
    perp: Perpetual | None,
) -> Features:
    """The features at the surface's minute. ``closes`` must be those known by then."""
    atm7 = surface.iv(7, Bucket.ATM)
    atm30 = surface.iv(30, Bucket.ATM)
    atm90 = surface.iv(90, Bucket.ATM)
    c25 = surface.iv(30, Bucket.CALL_25)
    p25 = surface.iv(30, Bucket.PUT_25)
    rr = _minus(c25, p25)
    bf = None if c25 is None or p25 is None or atm30 is None else (c25 + p25) / 2 - atm30
    rv7 = realised_vol(closes, 7)
    rv30 = realised_vol(closes, 30)
    forwards = [
        (f.tau, f.forward.forward) for f in surface.fits if f.used and f.forward.forward is not None
    ]
    return Features(
        ts_ms=surface.ts_ms,
        book=surface.book,
        index_price=index_price,
        atm_iv_7d=atm7,
        atm_iv_30d=atm30,
        atm_iv_90d=atm90,
        rr25_30d=rr,
        bf25_30d=bf,
        term_slope_7_30=_minus(atm30, atm7),
        term_slope_30_90=_minus(atm90, atm30),
        rv_7d=None if rv7 is None else rv7.vol,
        rv_30d=None if rv30 is None else rv30.vol,
        rv_30d_returns=30 if rv30 is not None else available_returns(closes, 30),
        rv_30d_delivery=0 if rv30 is None else rv30.from_delivery,
        iv_rv_30d=None if rv30 is None else _minus(atm30, rv30.vol),
        basis_30d=annualised_basis(forwards, index_price, 30 / 365),
        perp_funding_8h=None if perp is None else perp.funding_8h,
    )
