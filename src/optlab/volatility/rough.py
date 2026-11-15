"""ROUGH (Phase 2B): 30-day ATM implied vol from Deribit's mark IVs, and realised vol.

Shortcuts, listed in docs/rough-version.md and replaced in Phase 3 by the fitted surface and
the features worker:

- ATM IV of an expiry is Deribit's mark IV at the forward, linear in strike between the two
  listed strikes around it (call and put averaged at each strike). No smile fit.
- The 30-day IV is interpolated linearly in total variance (iv^2 * tau) between the two expiries
  around 30 days. Outside the listed expiries there is no value: nothing is extrapolated.
- Realised vol is close to close: the root mean square of daily log returns (zero mean),
  annualised by sqrt(365).

Conventions (CLAUDE.md): tau in years, ACT/365, to the 08:00 UTC expiry; IV as a decimal.
Pricing maths uses floating point.
"""

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from itertools import pairwise

from optlab.domain.clock import MS_PER_DAY

DAYS_PER_YEAR = 365.0


def tau_years(expiry_ms: int, now_ms: int) -> float:
    """Time to expiry in years, ACT/365."""
    return (expiry_ms - now_ms) / (DAYS_PER_YEAR * MS_PER_DAY)


def strike_ivs(rows: Iterable[tuple[float, float | None]]) -> list[tuple[float, float]]:
    """(strike, IV) per strike, averaging the call and put mark IVs; missing IVs are skipped."""
    by_strike: dict[float, list[float]] = defaultdict(list)
    for strike, iv in rows:
        if iv is not None and math.isfinite(iv):
            by_strike[strike].append(iv)
    return [(k, sum(v) / len(v)) for k, v in sorted(by_strike.items())]


def atm_iv(points: Sequence[tuple[float, float]], forward: float) -> float | None:
    """IV at the forward, linear in strike between the two strikes around it.

    ``points`` are (strike, IV) pairs. None when the forward is outside the listed strikes.
    """
    pts = sorted(points)
    for (k0, v0), (k1, v1) in pairwise(pts):
        if k0 <= forward <= k1:
            return v0 + (v1 - v0) * (forward - k0) / (k1 - k0)
    if len(pts) == 1 and pts[0][0] == forward:
        return pts[0][1]
    return None


def iv_at_tenor(points: Sequence[tuple[float, float]], tau: float) -> float | None:
    """IV at ``tau`` years, linear in total variance between the two expiries around it.

    ``points`` are (tau, ATM IV) pairs, one per expiry. None outside the listed expiries.
    """
    pts = sorted(p for p in points if p[0] > 0)
    for t, v in pts:
        if t == tau:
            return v
    for (t0, v0), (t1, v1) in pairwise(pts):
        if t0 < tau < t1:
            w0, w1 = v0 * v0 * t0, v1 * v1 * t1
            w = w0 + (w1 - w0) * (tau - t0) / (t1 - t0)
            return math.sqrt(w / tau) if w > 0 else None
    return None


def realised_vol(prices: Sequence[float], returns: int) -> float | None:
    """Annualised close-to-close vol over the last ``returns`` daily log returns.

    sqrt(365 * mean(r^2)), with zero mean. Needs ``returns + 1`` prices, else None.
    """
    if returns < 1 or len(prices) < returns + 1:
        return None
    window = prices[-(returns + 1) :]
    logs = [math.log(b / a) for a, b in pairwise(window)]
    return math.sqrt(DAYS_PER_YEAR * sum(r * r for r in logs) / len(logs))


def moving_average(prices: Sequence[float], n: int) -> float | None:
    """Mean of the last ``n`` prices, or None if there are fewer."""
    if n < 1 or len(prices) < n:
        return None
    return sum(prices[-n:]) / n
