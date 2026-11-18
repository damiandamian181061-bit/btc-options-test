"""Black-76 on the forward: prices and Greeks in Deribit's units.

Deribit prices BTC options with Black-76 on each expiry's forward (its ``underlying_price``) and
an interest rate of 0, so there is no discounting (checked on 16 Nov 2026 data, see
docs/conventions.md).

- ``price``: in the forward's currency, per 1 BTC contract (USDC for BTC_USDC options).
- ``price_inverse``: inverse (BTC) options' premium in BTC = the USD price / the forward.
- ``delta``: dV/dF, per contract (call N(d1), put N(d1) - 1). Not premium-adjusted.
- ``gamma``: d2V/dF2, per USD of the forward.
- ``vega``: change in price for a 1 vol point (0.01) rise in IV.
- ``theta``: change in price per calendar day, the instantaneous rate -dV/dtau / 365.

Deribit reports vega and theta in USD for both books, so the same functions compare with both.
tau is in years (ACT/365); IV is a decimal. Every function takes floats or numpy arrays
(broadcast together) and returns numpy arrays. With no time or no volatility left the price is
intrinsic value, delta is 1, 0 or -1 (0.5 or -0.5 exactly at the money), and the other Greeks
are 0.
"""

from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt
from scipy.special import ndtr

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

DAYS_PER_YEAR: Final = 365.0
VOL_POINT: Final = 0.01
_INV_SQRT_2PI: Final = 1.0 / np.sqrt(2.0 * np.pi)


def is_call(option_type: str) -> bool:
    """True for "call", False for "put"."""
    if option_type == "call":
        return True
    if option_type == "put":
        return False
    raise ValueError(f"unknown option type {option_type!r}")


def _pdf(x: FloatArray) -> FloatArray:
    out: FloatArray = _INV_SQRT_2PI * np.exp(-0.5 * x * x)
    return out


@dataclass(frozen=True, slots=True)
class _Inputs:
    f: FloatArray
    k: FloatArray
    t: FloatArray
    s: FloatArray
    call: BoolArray
    live: BoolArray  # time and vol left: the closed form applies
    sqrt_t: FloatArray
    d1: FloatArray
    d2: FloatArray


def _inputs(
    forward: npt.ArrayLike,
    strike: npt.ArrayLike,
    tau: npt.ArrayLike,
    sigma: npt.ArrayLike,
    call: npt.ArrayLike,
) -> _Inputs:
    f, k, t, s = (np.asarray(x, dtype=np.float64) for x in (forward, strike, tau, sigma))
    c = np.asarray(call, dtype=np.bool_)
    f, k, t, s, c = np.broadcast_arrays(f, k, t, s, c)
    live = (t > 0) & (s > 0) & (f > 0) & (k > 0)
    sqrt_t = np.sqrt(np.where(live, t, 1.0))
    sd = np.where(live, s, 1.0) * sqrt_t
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = np.where(live, (np.log(np.where(live, f / k, 1.0)) + 0.5 * sd * sd) / sd, 0.0)
    return _Inputs(f, k, t, s, c, live, sqrt_t, d1, d1 - sd)


def _intrinsic(x: _Inputs) -> FloatArray:
    out: FloatArray = np.where(x.call, np.maximum(x.f - x.k, 0.0), np.maximum(x.k - x.f, 0.0))
    return out


def price(
    forward: npt.ArrayLike,
    strike: npt.ArrayLike,
    tau: npt.ArrayLike,
    sigma: npt.ArrayLike,
    call: npt.ArrayLike,
) -> FloatArray:
    """Undiscounted Black-76 price, in the forward's currency per contract."""
    x = _inputs(forward, strike, tau, sigma, call)
    c = x.f * ndtr(x.d1) - x.k * ndtr(x.d2)
    p = x.k * ndtr(-x.d2) - x.f * ndtr(-x.d1)
    out: FloatArray = np.where(x.live, np.where(x.call, c, p), _intrinsic(x))
    return out


def price_inverse(
    forward: npt.ArrayLike,
    strike: npt.ArrayLike,
    tau: npt.ArrayLike,
    sigma: npt.ArrayLike,
    call: npt.ArrayLike,
) -> FloatArray:
    """Inverse (BTC) option premium in BTC: the USD price divided by the forward."""
    out: FloatArray = price(forward, strike, tau, sigma, call) / np.asarray(forward, np.float64)
    return out


@dataclass(frozen=True, slots=True)
class Greeks:
    """Price and Greeks in Deribit's units (see the module docstring)."""

    price: FloatArray
    delta: FloatArray
    gamma: FloatArray
    vega: FloatArray
    theta: FloatArray


def greeks(
    forward: npt.ArrayLike,
    strike: npt.ArrayLike,
    tau: npt.ArrayLike,
    sigma: npt.ArrayLike,
    call: npt.ArrayLike,
) -> Greeks:
    """Price, delta, gamma, vega (per vol point) and theta (per calendar day)."""
    x = _inputs(forward, strike, tau, sigma, call)
    n1 = _pdf(x.d1)
    nd1 = ndtr(x.d1)
    c = x.f * nd1 - x.k * ndtr(x.d2)
    p = x.k * ndtr(-x.d2) - x.f * ndtr(-x.d1)
    value = np.where(x.live, np.where(x.call, c, p), _intrinsic(x))
    # At expiry: 1, 0 or -1, and +-0.5 exactly at the money (the limit of N(d1) there).
    step = np.where(x.f > x.k, 1.0, np.where(x.f < x.k, 0.0, 0.5))
    delta = np.where(x.live, nd1, step) - np.where(x.call, 0.0, 1.0)
    sd = np.where(x.live, x.s, 1.0) * x.sqrt_t
    with np.errstate(divide="ignore", invalid="ignore"):
        gamma = np.where(x.live, n1 / (x.f * sd), 0.0)
        theta = np.where(x.live, -x.f * n1 * x.s / (2.0 * x.sqrt_t) / DAYS_PER_YEAR, 0.0)
    vega = np.where(x.live, x.f * n1 * x.sqrt_t * VOL_POINT, 0.0)
    return Greeks(price=value, delta=delta, gamma=gamma, vega=vega, theta=theta)


def vega_per_unit(
    forward: npt.ArrayLike,
    strike: npt.ArrayLike,
    tau: npt.ArrayLike,
    sigma: npt.ArrayLike,
) -> FloatArray:
    """dV/dsigma for sigma as a decimal (what the IV solver's Newton step needs)."""
    x = _inputs(forward, strike, tau, sigma, True)
    out: FloatArray = np.where(x.live, x.f * _pdf(x.d1) * x.sqrt_t, 0.0)
    return out
