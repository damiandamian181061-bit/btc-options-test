"""The smile of one expiry: a smoothing spline of implied vol in log-moneyness k = ln(K/F).

Quotes (``smile_points``). For each strike, the out-of-the-money option only (puts below the
forward, calls at or above it): in-the-money prices are mostly intrinsic value and say little
about volatility. A quote counts when the option is open for trading, two-sided and not crossed,
no wider than ``max_rel_spread`` of its mid, and its mid IV is pinned down to within
``max_iv_error`` by the tick size (``optlab.pricing.iv``). Inverse premiums (BTC) are turned into
USD at the expiry's forward first, as Deribit prices them. The bid, ask and mid IVs are our own,
solved from the quotes; Deribit's mark IV is carried along only for comparison.

The fit (``fit_smile``). A cubic smoothing spline through the mid IVs, each weighted by
1 / (ask IV - bid IV), so tight quotes count more; the smoothing is chosen by generalised
cross-validation unless ``lam`` is given (scipy's ``make_smoothing_spline``). Outside the quoted
range the IV is held flat at its last fitted value.

Checks. Butterfly: Gatheral's density condition on total variance w(k) = iv(k)^2 tau,

    g(k) = (1 - k w'/(2w))^2 - (w'^2 / 4) (1/w + 1/4) + w''/2 >= 0,

on a grid across the quoted range; a negative g means the smile implies a negative probability
density (a butterfly that costs less than nothing). Calendar: total variance at the same k must
not fall from one expiry to the next (``calendar_gap``).
"""

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy.interpolate import BSpline, make_smoothing_spline
from scipy.optimize import brentq
from scipy.special import ndtr

from optlab.domain.market import Book
from optlab.pricing.black76 import FloatArray
from optlab.pricing.iv import implied_vol


@dataclass(frozen=True, slots=True)
class SmileConfig:
    max_rel_spread: float = 1.0  # leave out quotes wider than this fraction of their mid
    max_iv_error: float = 0.02  # leave out mid IVs a tick moves by more than 2 vol points
    min_points: int = 5
    min_spread_iv: float = 0.005  # weights use at least half a vol point of bid-ask
    lam: float | None = None  # None: chosen by generalised cross-validation
    grid: int = 101  # points for the butterfly and calendar checks


DEFAULT = SmileConfig()


def smile_points(
    chain: pd.DataFrame,
    forward: float,
    tau: float,
    book: Book,
    config: SmileConfig = DEFAULT,
) -> pd.DataFrame:
    """The usable out-of-the-money quotes of one expiry, with our IVs and fit weights.

    Columns: strike, k, call, bid_iv, ask_iv, mid_iv, weight, mark_iv, instrument_name.
    """
    df = chain.loc[
        (chain["call"].astype(bool) == (chain["strike"] >= forward)) & (chain["state"] == "open")
    ]
    bid = df["bid"].astype(float).to_numpy()
    ask = df["ask"].astype(float).to_numpy()
    with np.errstate(invalid="ignore"):
        mid = (bid + ask) / 2
        quoted = (bid > 0) & (ask >= bid) & ((ask - bid) <= config.max_rel_spread * mid)
    df = df.loc[quoted]
    bid, ask, mid = bid[quoted], ask[quoted], mid[quoted]
    to_usd = forward if book == Book.INVERSE else 1.0
    strikes = df["strike"].to_numpy(dtype=float)
    calls = df["call"].to_numpy(dtype=bool)
    step = df["tick_size"].to_numpy(dtype=float) * to_usd
    m = implied_vol(
        mid * to_usd,
        forward,
        strikes,
        tau,
        calls,
        price_step=step,
        max_iv_error=config.max_iv_error,
    )
    b = implied_vol(bid * to_usd, forward, strikes, tau, calls)
    a = implied_vol(ask * to_usd, forward, strikes, tau, calls)
    keep = m.ok & ~m.unreliable
    # A bid too low to have an IV (at or below the vol floor) still leaves a usable mid.
    bid_iv = np.where(b.ok, b.iv, np.nan)
    ask_iv = np.where(a.ok, a.iv, np.nan)
    width = np.where(np.isfinite(bid_iv) & np.isfinite(ask_iv), ask_iv - bid_iv, np.nan)
    width = np.where(np.isfinite(width), width, 2 * np.where(np.isfinite(ask_iv), ask_iv - m.iv, 0))
    weight = 1.0 / np.maximum(width, config.min_spread_iv)
    out = pd.DataFrame(
        {
            "instrument_name": df["instrument_name"].to_numpy(),
            "strike": strikes,
            "k": np.log(strikes / forward),
            "call": calls,
            "bid_iv": bid_iv,
            "ask_iv": ask_iv,
            "mid_iv": m.iv,
            "weight": weight,
            "mark_iv": df["mark_iv"].to_numpy(dtype=float),
        }
    )
    return out.loc[keep].sort_values("k", kind="stable").reset_index(drop=True)


@dataclass(frozen=True)
class Smile:
    """A fitted smile: IV as a function of log-moneyness, flat outside [k_min, k_max]."""

    expiry_ms: int
    tau: float
    forward: float
    spline: BSpline
    k_min: float
    k_max: float
    n_points: int
    rmse: float  # root mean square of fitted minus quoted mid IV, as a decimal
    config: SmileConfig = DEFAULT

    def iv(self, k: npt.ArrayLike) -> FloatArray:
        x = np.clip(np.asarray(k, dtype=np.float64), self.k_min, self.k_max)
        out: FloatArray = np.asarray(self.spline(x), dtype=np.float64)
        return out

    def total_variance(self, k: npt.ArrayLike) -> FloatArray:
        v = self.iv(k)
        out: FloatArray = v * v * self.tau
        return out

    def iv_at_strike(self, strike: npt.ArrayLike) -> FloatArray:
        return self.iv(np.log(np.asarray(strike, dtype=np.float64) / self.forward))

    def grid(self) -> FloatArray:
        out: FloatArray = np.linspace(self.k_min, self.k_max, self.config.grid)
        return out

    def butterfly(self, k: npt.ArrayLike | None = None) -> FloatArray:
        """Gatheral's g(k) inside the quoted range (negative: butterfly arbitrage)."""
        x = self.grid() if k is None else np.asarray(k, dtype=np.float64)
        s = np.asarray(self.spline(x), dtype=np.float64)
        s1 = np.asarray(self.spline.derivative(1)(x), dtype=np.float64)
        s2 = np.asarray(self.spline.derivative(2)(x), dtype=np.float64)
        t = self.tau
        w = s * s * t
        w1 = 2 * t * s * s1
        w2 = 2 * t * (s1 * s1 + s * s2)
        with np.errstate(divide="ignore", invalid="ignore"):
            g = (1 - x * w1 / (2 * w)) ** 2 - (w1 * w1 / 4) * (1 / w + 0.25) + w2 / 2
        out: FloatArray = np.where(w > 0, g, -np.inf)
        return out

    def butterfly_min(self) -> float:
        return float(np.min(self.butterfly()))

    def k_for_delta(self, delta: float, *, call: bool) -> float | None:
        """Log-moneyness whose forward delta is ``delta`` (calls) or ``-delta`` (puts)."""
        target = delta if call else 1.0 - delta  # N(d1) for the call, or for the put's call

        def gap(k: float) -> float:
            w = float(self.total_variance(k))
            sd = np.sqrt(w)
            return float(ndtr((-k + w / 2) / sd)) - target

        sd0 = float(np.sqrt(self.total_variance(0.0)))
        lo, hi = -8 * sd0, 8 * sd0
        try:
            return float(brentq(gap, lo, hi, xtol=1e-12))
        except ValueError:
            return None

    def iv_for_delta(self, delta: float, *, call: bool) -> float | None:
        k = self.k_for_delta(delta, call=call)
        return None if k is None else float(self.iv(k))


def fit_smile(
    points: pd.DataFrame,
    *,
    expiry_ms: int,
    tau: float,
    forward: float,
    config: SmileConfig = DEFAULT,
) -> Smile | None:
    """Fit the smoothing spline to ``smile_points``; None with fewer than ``min_points``."""
    pts = points.dropna(subset=["k", "mid_iv", "weight"]).sort_values("k")
    pts = pts.loc[~pts["k"].duplicated()]
    if len(pts) < max(config.min_points, 5) or not tau > 0:
        return None
    x = pts["k"].to_numpy(dtype=float)
    y = pts["mid_iv"].to_numpy(dtype=float)
    w = pts["weight"].to_numpy(dtype=float)
    w = w / w.mean()
    spline = make_smoothing_spline(x, y, w=w, lam=config.lam)
    fitted = np.asarray(spline(x), dtype=np.float64)
    if not np.all(np.isfinite(fitted)) or np.min(fitted) <= 0:
        return None
    return Smile(
        expiry_ms=expiry_ms,
        tau=tau,
        forward=forward,
        spline=spline,
        k_min=float(x[0]),
        k_max=float(x[-1]),
        n_points=len(pts),
        rmse=float(np.sqrt(np.mean((fitted - y) ** 2))),
        config=config,
    )


def calendar_gap(earlier: Smile, later: Smile) -> float | None:
    """Smallest w_later(k) - w_earlier(k) over the k range both quote; negative is arbitrage."""
    lo, hi = max(earlier.k_min, later.k_min), min(earlier.k_max, later.k_max)
    if not hi > lo:
        return None
    k = np.linspace(lo, hi, earlier.config.grid)
    return float(np.min(later.total_variance(k) - earlier.total_variance(k)))
