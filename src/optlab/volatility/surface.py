"""The volatility surface of one book at one snapshot minute.

For each listed expiry: its forward (``forward``), its quotes and smile (``smile``), and the IV
at the money (k = 0, the forward) and at the 25-delta and 10-delta calls and puts (forward
delta on the fitted smile). Then, for each bucket, the IV at the brief's tenors of 1, 3, 7, 14,
30, 60 and 90 days: total variance iv^2 tau is interpolated linearly in tau between the two
expiries around the tenor, at the same bucket (fixed delta, or k = 0 for ATM). Before the first
usable expiry and after the last, the IV is held flat, and the point is marked extrapolated.

Expiries closer than ``min_tau_hours`` are fitted and stored but not used for the tenors: in the
last hours Deribit's settlement average (07:30 to 08:00 UTC) and a few ticks of premium make
their IVs erratic.

Calendar check: each usable expiry's smile is compared with the previous usable one
(``smile.calendar_gap``); a negative gap means total variance falls with time somewhere.
"""

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import pandas as pd

from optlab.domain.clock import MS_PER_DAY
from optlab.domain.market import Book
from optlab.volatility.forward import Forward, expiry_forwards
from optlab.volatility.smile import (
    DEFAULT,
    Smile,
    SmileConfig,
    calendar_gap,
    fit_smile,
    smile_points,
)

YEAR_MS = 365 * MS_PER_DAY
TENORS_DAYS: tuple[int, ...] = (1, 3, 7, 14, 30, 60, 90)


class Bucket(StrEnum):
    PUT_10 = "10d_put"
    PUT_25 = "25d_put"
    ATM = "atm"
    CALL_25 = "25d_call"
    CALL_10 = "10d_call"


BUCKETS: tuple[Bucket, ...] = tuple(Bucket)
BUCKET_DELTA: dict[Bucket, tuple[bool, float]] = {
    Bucket.PUT_10: (False, 0.10),
    Bucket.PUT_25: (False, 0.25),
    Bucket.CALL_25: (True, 0.25),
    Bucket.CALL_10: (True, 0.10),
}


@dataclass(frozen=True, slots=True)
class SurfaceConfig:
    min_tau_hours: float = 12.0
    smile: SmileConfig = DEFAULT
    parity_pairs: int = 3
    parity_max_spread: float = 0.2
    calendar_tolerance: float = 1e-6  # total variance; below minus this is arbitrage


@dataclass(frozen=True)
class ExpiryFit:
    expiry_ms: int
    tau: float
    forward: Forward
    smile: Smile | None
    n_points: int
    bucket_iv: dict[Bucket, float]
    used: bool  # counts for the tenor buckets
    butterfly_min: float | None
    calendar_gap: float | None  # against the previous used expiry
    calendar_ok: bool | None
    note: str = ""


@dataclass(frozen=True, slots=True)
class SurfacePoint:
    tenor_days: int
    bucket: Bucket
    iv: float
    total_variance: float
    extrapolated: bool
    lower_expiry_ms: int
    upper_expiry_ms: int


@dataclass(frozen=True)
class Surface:
    ts_ms: int
    book: Book
    fits: list[ExpiryFit]
    points: list[SurfacePoint] = field(default_factory=list)

    def point(self, tenor_days: int, bucket: Bucket) -> SurfacePoint | None:
        for p in self.points:
            if p.tenor_days == tenor_days and p.bucket == bucket:
                return p
        return None

    def iv(self, tenor_days: int, bucket: Bucket) -> float | None:
        p = self.point(tenor_days, bucket)
        return None if p is None else p.iv


def interpolate_tenor(
    nodes: list[tuple[float, float]], tau: float
) -> tuple[float, bool, int, int] | None:
    """IV at ``tau`` from (tau, iv) nodes sorted by tau: linear in total variance between the
    two around it, flat outside. Returns (iv, extrapolated, lower index, upper index)."""
    if not nodes:
        return None
    taus = [t for t, _ in nodes]
    if tau <= taus[0]:
        return nodes[0][1], tau < taus[0], 0, 0
    if tau >= taus[-1]:
        return nodes[-1][1], tau > taus[-1], len(nodes) - 1, len(nodes) - 1
    hi = next(i for i, t in enumerate(taus) if t >= tau)
    lo = hi - 1
    (t0, v0), (t1, v1) = nodes[lo], nodes[hi]
    if t1 == tau:
        return v1, False, hi, hi
    w0, w1 = v0 * v0 * t0, v1 * v1 * t1
    w = w0 + (w1 - w0) * (tau - t0) / (t1 - t0)
    return float(np.sqrt(max(w, 0.0) / tau)), False, lo, hi


def _bucket_ivs(smile: Smile) -> dict[Bucket, float]:
    out = {Bucket.ATM: float(smile.iv(0.0))}
    for bucket, (call, delta) in BUCKET_DELTA.items():
        iv = smile.iv_for_delta(delta, call=call)
        if iv is not None:
            out[bucket] = iv
    return out


def build_surface(
    chain: pd.DataFrame,
    futures: pd.DataFrame,
    book: Book,
    ts_ms: int,
    *,
    index_price: float | None,
    config: SurfaceConfig = SurfaceConfig(),  # noqa: B008 (frozen, safe as a default)
) -> Surface:
    """Fit every expiry of ``chain`` (one book at the snapshot ``ts_ms``), then the buckets."""
    live = chain.loc[chain["expiry_ms"] > ts_ms]
    forwards = expiry_forwards(
        live,
        futures,
        book,
        index_price=index_price,
        pairs=config.parity_pairs,
        max_spread=config.parity_max_spread,
    )
    fits: list[ExpiryFit] = []
    previous: Smile | None = None
    for expiry, group in sorted(live.groupby("expiry_ms"), key=lambda kv: int(kv[0])):
        e = int(expiry)
        tau = (e - ts_ms) / YEAR_MS
        fwd = forwards[e]
        too_close = tau * 365 * 24 < config.min_tau_hours
        if fwd.forward is None:
            fits.append(ExpiryFit(e, tau, fwd, None, 0, {}, False, None, None, None, "no forward"))
            continue
        pts = smile_points(group, fwd.forward, tau, book, config.smile)
        smile = fit_smile(pts, expiry_ms=e, tau=tau, forward=fwd.forward, config=config.smile)
        if smile is None:
            note = f"{len(pts)} usable quotes: too few to fit"
            fits.append(ExpiryFit(e, tau, fwd, None, len(pts), {}, False, None, None, None, note))
            continue
        used = not too_close
        gap = calendar_gap(previous, smile) if used and previous is not None else None
        ok = None if gap is None else gap >= -config.calendar_tolerance
        note = (
            f"under {config.min_tau_hours:g} h to expiry: not used for the tenors"
            if too_close
            else ""
        )
        fits.append(
            ExpiryFit(
                expiry_ms=e,
                tau=tau,
                forward=fwd,
                smile=smile,
                n_points=smile.n_points,
                bucket_iv=_bucket_ivs(smile),
                used=used,
                butterfly_min=smile.butterfly_min(),
                calendar_gap=gap,
                calendar_ok=ok,
                note=note,
            )
        )
        if used:
            previous = smile

    points: list[SurfacePoint] = []
    usable = [f for f in fits if f.used]
    for bucket in BUCKETS:
        nodes = [(f, f.bucket_iv[bucket]) for f in usable if bucket in f.bucket_iv]
        for days in TENORS_DAYS:
            res = interpolate_tenor([(f.tau, v) for f, v in nodes], days / 365)
            if res is None:
                continue
            iv, extrapolated, lo, hi = res
            points.append(
                SurfacePoint(
                    tenor_days=days,
                    bucket=bucket,
                    iv=iv,
                    total_variance=iv * iv * days / 365,
                    extrapolated=extrapolated,
                    lower_expiry_ms=nodes[lo][0].expiry_ms,
                    upper_expiry_ms=nodes[hi][0].expiry_ms,
                )
            )
    return Surface(ts_ms=ts_ms, book=book, fits=fits, points=points)
