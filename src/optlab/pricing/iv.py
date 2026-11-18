"""Implied volatility from a Black-76 price: safeguarded Newton, with a Brent fallback.

The solver works on the out-of-the-money equivalent of each option: an in-the-money call has
the same time value as the put at its strike (put-call parity on the forward, undiscounted), and
time value is what carries the volatility. That keeps deep in-the-money quotes well conditioned.

No-arbitrage bounds (undiscounted, r = 0): a call is worth more than max(F - K, 0) and less than
F; a put more than max(K - F, 0) and less than K. A price outside them has no IV.

Newton starts at the inflection point sqrt(2 |ln(F/K)| / tau) (Manaster and Koehler), where it
converges monotonically, and every step is kept inside a bracket that is narrowed as it goes; a
step that would leave it bisects instead. Anything not converged after ``max_newton`` steps goes
to scipy's Brent root finder on [``lo``, ``hi``].

Flags for unreliable quotes: ``iv_error`` is how far the IV moves for a price error of
``price_step`` (the quote's precision, such as the tick size), price_step / (dV/dsigma).
``unreliable`` marks a solved IV with ``iv_error`` above ``max_iv_error``: the price says too
little about the volatility, typically a far wing quoted at a tick or two.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import numpy as np
import numpy.typing as npt
from scipy.optimize import brentq

from optlab.pricing import black76 as b76
from optlab.pricing.black76 import BoolArray, FloatArray

IV_MIN: Final = 1e-4  # 0.01%
IV_MAX: Final = 10.0  # 1000%


class IVStatus(StrEnum):
    OK = "ok"
    NO_PRICE = "no_price"  # missing, not finite, or not positive
    NO_TIME = "no_time"  # at or past expiry
    BELOW_INTRINSIC = "below_intrinsic"  # at or below intrinsic value: no time value left
    ABOVE_MAXIMUM = "above_maximum"  # at or above F (call) or K (put)
    OUT_OF_RANGE = "out_of_range"  # the IV would be outside [IV_MIN, IV_MAX]
    NOT_CONVERGED = "not_converged"


@dataclass(frozen=True, slots=True)
class IVResult:
    iv: FloatArray  # decimal; NaN unless status is ok
    status: npt.NDArray[np.str_]
    vega: FloatArray  # per vol point at the solution (Deribit's units); NaN unless ok
    iv_error: FloatArray  # decimal IV moved by one price_step; NaN unless ok
    unreliable: BoolArray  # ok, but iv_error > max_iv_error
    brent: BoolArray  # solved by the Brent fallback

    @property
    def ok(self) -> BoolArray:
        out: BoolArray = self.status == IVStatus.OK.value
        return out


def implied_vol(
    price: npt.ArrayLike,
    forward: npt.ArrayLike,
    strike: npt.ArrayLike,
    tau: npt.ArrayLike,
    call: npt.ArrayLike,
    *,
    price_step: npt.ArrayLike = 0.0,
    max_iv_error: float = 0.01,
    tol: float = 1e-12,
    max_newton: int = 50,
    lo: float = IV_MIN,
    hi: float = IV_MAX,
) -> IVResult:
    """IV of each price (in the forward's currency); broadcast like ``black76.price``."""
    p, f, k, t, step = (
        np.asarray(x, dtype=np.float64) for x in (price, forward, strike, tau, price_step)
    )
    c = np.asarray(call, dtype=np.bool_)
    p, f, k, t, step, c = np.broadcast_arrays(p, f, k, t, step, c)
    shape = p.shape
    status = np.full(shape, IVStatus.OK.value, dtype=object)
    with np.errstate(invalid="ignore"):
        intrinsic = np.where(c, np.maximum(f - k, 0.0), np.maximum(k - f, 0.0))
        time_value = p - intrinsic
        # The out-of-the-money equivalent: a call when K >= F, else a put.
        otm_call = k >= f
        upper = np.where(otm_call, f, k)
        bad_price = ~np.isfinite(p) | (p <= 0) | ~np.isfinite(f) | (f <= 0) | (k <= 0)
    status[bad_price] = IVStatus.NO_PRICE.value
    todo = ~bad_price
    status[todo & ~(t > 0)] = IVStatus.NO_TIME.value
    todo &= t > 0
    floor = 1e-12 * np.maximum(f, 1.0)
    status[todo & (time_value <= floor)] = IVStatus.BELOW_INTRINSIC.value
    todo &= time_value > floor
    status[todo & (time_value >= upper)] = IVStatus.ABOVE_MAXIMUM.value
    todo &= time_value < upper

    # The target can't be reached inside [lo, hi].
    with np.errstate(all="ignore"):
        p_lo = b76.price(f, k, t, lo, otm_call)
        p_hi = b76.price(f, k, t, hi, otm_call)
    beyond = todo & ((time_value < p_lo) | (time_value > p_hi))
    status[beyond] = IVStatus.OUT_OF_RANGE.value
    todo &= ~beyond

    iv = np.full(shape, np.nan)
    brent = np.zeros(shape, dtype=np.bool_)
    idx = np.flatnonzero(todo)
    if idx.size:
        fi: FloatArray = f.ravel()[idx]
        ki: FloatArray = k.ravel()[idx]
        ti: FloatArray = t.ravel()[idx]
        qi: FloatArray = time_value.ravel()[idx]
        ci: BoolArray = otm_call.ravel()[idx]
        sol, done = _newton(fi, ki, ti, qi, ci, tol=tol, max_iter=max_newton, lo=lo, hi=hi)
        for j in np.flatnonzero(~done):
            root = _brent(fi[j], ki[j], ti[j], qi[j], bool(ci[j]), lo=lo, hi=hi, tol=tol)
            if root is not None:
                sol[j] = root
                done[j] = True
                brent.ravel()[idx[j]] = True
        flat = iv.ravel()
        flat[idx[done]] = sol[done]
        st = status.ravel()
        st[idx[~done]] = IVStatus.NOT_CONVERGED.value

    ok = status == IVStatus.OK.value
    sig = np.where(ok, iv, 1.0)
    vega_unit = np.where(ok, b76.vega_per_unit(f, k, t, sig), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        iv_error = np.where(ok, step / vega_unit, np.nan)
    unreliable = ok & (iv_error > max_iv_error)
    return IVResult(
        iv=iv,
        status=status.astype(str),
        vega=vega_unit * b76.VOL_POINT,
        iv_error=iv_error,
        unreliable=unreliable,
        brent=brent,
    )


def _newton(
    f: FloatArray,
    k: FloatArray,
    t: FloatArray,
    q: FloatArray,
    call: BoolArray,
    *,
    tol: float,
    max_iter: int,
    lo: float,
    hi: float,
) -> tuple[FloatArray, BoolArray]:
    """Newton on the out-of-the-money price q, kept inside a shrinking bracket."""
    a = np.full(f.shape, lo)
    b = np.full(f.shape, hi)
    with np.errstate(divide="ignore", invalid="ignore"):
        start = np.sqrt(2.0 * np.abs(np.log(f / k)) / t)
        atm_guess = np.sqrt(2.0 * np.pi / t) * q / f
    sig = np.clip(np.where(start > 0, np.maximum(start, atm_guess), atm_guess), lo, hi)
    done = np.zeros(f.shape, dtype=np.bool_)
    for _ in range(max_iter):
        live = ~done
        if not live.any():
            break
        diff = b76.price(f, k, t, sig, call) - q
        vega = b76.vega_per_unit(f, k, t, sig)
        conv = np.abs(diff) <= tol * np.maximum(q, 1e-300) + 1e-14 * f
        done |= live & conv
        live &= ~conv
        # The price rises with sigma: a positive diff means the root is below sig.
        b = np.where(live & (diff > 0), sig, b)
        a = np.where(live & (diff < 0), sig, a)
        with np.errstate(divide="ignore", invalid="ignore"):
            nxt = sig - diff / vega
        inside = np.isfinite(nxt) & (nxt > a) & (nxt < b)
        nxt = np.where(inside, nxt, 0.5 * (a + b))
        small = np.abs(nxt - sig) <= 1e-15 * np.maximum(sig, 1.0)
        sig = np.where(live, nxt, sig)
        done |= live & small & (b - a < 1e-12)
    if max_iter == 0:
        done[:] = False
    else:
        diff = b76.price(f, k, t, sig, call) - q
        done &= np.abs(diff) <= 1e-9 * np.maximum(q, 1e-300) + 1e-12 * f
    return sig, done


def _brent(
    f: float, k: float, t: float, q: float, call: bool, *, lo: float, hi: float, tol: float
) -> float | None:
    def gap(s: float) -> float:
        return float(b76.price(f, k, t, s, call)) - q

    try:
        root = brentq(gap, lo, hi, xtol=1e-14, rtol=4 * np.finfo(float).eps, maxiter=200)
    except ValueError:
        return None
    if abs(gap(root)) > max(1e-9 * q, 1e-12 * f, tol):
        return None
    return float(root)
