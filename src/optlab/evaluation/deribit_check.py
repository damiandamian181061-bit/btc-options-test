"""Phase 3 gate: how far our IVs and Greeks are from Deribit's on recorded snapshots.

Two comparisons per option quote (state open, with a mark IV, before expiry):

- **Engine** (our Black-76 and IV solver with Deribit's own inputs): the forward is Deribit's
  ``underlying_price``, tau runs from the ticker's exchange time, and the vol is Deribit's mark
  IV. We re-solve the IV from Deribit's mark price, and price delta, gamma, vega and theta. Any
  difference is ours (formula, units, solver) or Deribit's rounding: mark IV to 0.01 vol point,
  BTC_USDC marks to 0.01 USDC, BTC marks to 0.0001 BTC, Greeks to 5 decimals.
- **Surface** (our whole pipeline): our forward (the dated future, else parity), our smile fitted
  to the bid and ask quotes, tau from the snapshot minute. Our IV and delta at each strike inside
  the quoted range, against Deribit's mark IV and delta. Here differences are model choices:
  two different smiles fitted to the same quotes.

``leg_choice`` then asks the finance question: picking the short leg closest to 0.20 |delta|
(within 0.15 to 0.30, expiries 7 to 30 days), how often would our delta choose a different
strike than Deribit's?

``gate_checks`` holds each measured difference against the tolerances Damian agreed on 18 Nov
2026 (``TOLERANCES``). They are never widened to make a check pass.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from optlab.domain.clock import MS_PER_DAY
from optlab.domain.market import Book
from optlab.pricing import black76 as b76
from optlab.pricing.iv import implied_vol
from optlab.volatility import chain as ch
from optlab.volatility.surface import Surface, SurfaceConfig, build_surface

YEAR_MS = 365 * MS_PER_DAY
DELTA_BUCKETS = (0.0, 0.05, 0.15, 0.30, 0.50, 0.85, 1.0001)
DAY_BUCKETS = (0.0, 1.0, 3.0, 7.0, 14.0, 30.0, 60.0, 120.0, 400.0)

# The Phase 3 gate's tolerances, agreed by Damian on 18 Nov 2026 (add-on g). Units: delta in
# delta units, gamma per USD, vega in USD per vol point, theta in USD a day, IV in vol points,
# the forward in basis points, shares as fractions, strikes as a count of listed strikes.
TOLERANCES: dict[str, float] = {
    "engine_delta_max": 0.0001,  # expiries 1 day or more
    "engine_gamma_max": 0.00001,  # expiries 1 day or more
    "engine_vega_max": 0.05,  # expiries 1 day or more
    "engine_theta_max": 0.05,  # expiries 7 days or more
    "engine_iv_p99": 0.5,  # IV from the mark price; 7-30 days, |delta| 0.05-0.50
    "surface_iv_median": 0.25,  # 7-30 days, |delta| 0.05-0.50
    "surface_iv_p99": 1.0,  # 7-30 days, |delta| 0.05-0.50
    "surface_delta_p99": 0.005,  # 7-30 days, |delta| 0.05-0.50
    "leg_differs_share": 0.02,  # the 0.20-delta short leg, 7-30 days
    "leg_differs_strikes": 1.0,  # ... and only ever the neighbouring strike
    "forward_parity_p95_bp": 10.0,  # dated future against put-call parity
}


@dataclass(frozen=True, slots=True)
class Check:
    """One gate measurement against its tolerance. No data, or NaN, never passes."""

    name: str
    measured: float
    limit: float
    n: int

    @property
    def passed(self) -> bool:
        return self.n > 0 and not math.isnan(self.measured) and self.measured <= self.limit


def engine_rows(chain: pd.DataFrame, book: Book, ts_ms: int) -> pd.DataFrame:
    """One row per quote: Deribit's IV and Greeks next to ours from Deribit's inputs."""
    q = chain.loc[
        (chain["state"] == "open")
        & chain["mark_iv"].notna()
        & chain["mark_price"].notna()
        & chain["underlying_price"].notna()
        & (chain["expiry_ms"] > chain["exchange_ts_ms"])
    ].copy()
    f = q["underlying_price"].to_numpy(dtype=float)
    k = q["strike"].to_numpy(dtype=float)
    tau = (q["expiry_ms"] - q["exchange_ts_ms"]).to_numpy(dtype=float) / YEAR_MS
    call = q["call"].to_numpy(dtype=bool)
    sigma = q["mark_iv"].to_numpy(dtype=float)
    to_usd = f if book == Book.INVERSE else 1.0
    mark_usd = q["mark_price"].to_numpy(dtype=float) * to_usd
    step = (0.0001 if book == Book.INVERSE else 0.01) * np.ones_like(f) * to_usd
    iv = implied_vol(mark_usd, f, k, tau, call, price_step=step / 2)
    g = b76.greeks(f, k, tau, sigma, call)
    return pd.DataFrame(
        {
            "ts_ms": ts_ms,
            "book": book.value,
            "instrument_name": q["instrument_name"].to_numpy(),
            "days": tau * 365,
            "abs_delta": np.abs(q["delta"].to_numpy(dtype=float)),
            "mark_iv": sigma,
            "our_iv": np.where(iv.ok, iv.iv, np.nan),
            "iv_rounding": np.where(iv.ok, iv.iv_error, np.nan),  # half a price tick, in IV
            "delta": q["delta"].to_numpy(dtype=float),
            "our_delta": g.delta,
            "gamma": q["gamma"].to_numpy(dtype=float),
            "our_gamma": g.gamma,
            "vega": q["vega"].to_numpy(dtype=float),
            "our_vega": g.vega,
            "theta": q["theta"].to_numpy(dtype=float),
            "our_theta": g.theta,
        }
    )


def surface_rows(chain: pd.DataFrame, surface: Surface) -> pd.DataFrame:
    """Our fitted IV and delta at each quoted strike inside each fitted expiry's range."""
    out = []
    for fit in surface.fits:
        s, fwd = fit.smile, fit.forward.forward
        if s is None or fwd is None:
            continue
        q = chain.loc[
            (chain["expiry_ms"] == fit.expiry_ms)
            & (chain["state"] == "open")
            & chain["mark_iv"].notna()
            & chain["delta"].notna()
        ]
        k = np.log(q["strike"].to_numpy(dtype=float) / fwd)
        inside = (k >= s.k_min) & (k <= s.k_max)
        q, k = q.loc[inside], k[inside]
        ours = s.iv(k)
        call = q["call"].to_numpy(dtype=bool)
        delta = b76.greeks(fwd, q["strike"].to_numpy(dtype=float), fit.tau, ours, call).delta
        out.append(
            pd.DataFrame(
                {
                    "ts_ms": surface.ts_ms,
                    "book": surface.book.value,
                    "expiry_ms": fit.expiry_ms,
                    "instrument_name": q["instrument_name"].to_numpy(),
                    "strike": q["strike"].to_numpy(dtype=float),
                    "call": call,
                    "days": fit.tau * 365,
                    "used": fit.used,
                    "abs_delta": np.abs(q["delta"].to_numpy(dtype=float)),
                    "mark_iv": q["mark_iv"].to_numpy(dtype=float),
                    "our_iv": ours,
                    "delta": q["delta"].to_numpy(dtype=float),
                    "our_delta": delta,
                    "deribit_forward": q["underlying_price"].to_numpy(dtype=float),
                    "our_forward": fwd,
                }
            )
        )
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def summarise(
    rows: pd.DataFrame, ours: str, theirs: str, *, scale: float = 1.0, by: str = "both"
) -> pd.DataFrame:
    """|ours - theirs| x scale by delta and/or days bucket: count, median, 99th percentile, max."""
    d = rows.assign(err=(rows[ours] - rows[theirs]).abs() * scale).dropna(subset=["err"])
    keys = []
    if by in ("both", "delta"):
        d = d.assign(delta_bucket=pd.cut(d["abs_delta"], DELTA_BUCKETS, right=False))
        keys.append("delta_bucket")
    if by in ("both", "days"):
        d = d.assign(days_bucket=pd.cut(d["days"], DAY_BUCKETS, right=False))
        keys.append("days_bucket")
    g = d.groupby(keys, observed=True)["err"]
    return pd.DataFrame(
        {"n": g.size(), "median": g.median(), "p99": g.quantile(0.99), "max": g.max()}
    )


@dataclass(frozen=True, slots=True)
class LegPick:
    ts_ms: int
    expiry_ms: int
    call: bool
    by_deribit: float | None
    by_ours: float | None


def leg_choice(
    rows: pd.DataFrame, *, low: float = 0.15, high: float = 0.30, target: float = 0.20
) -> list[LegPick]:
    """Per minute, expiry (7-30 days) and side: the strike nearest ``target`` |delta| within
    [low, high], chosen by Deribit's delta and by ours (from ``surface_rows``)."""
    out: list[LegPick] = []
    window = rows.loc[(rows["days"] >= 7) & (rows["days"] <= 30)]
    for (ts, expiry, call), g in window.groupby(["ts_ms", "expiry_ms", "call"]):

        def pick(col: str, g: pd.DataFrame = g) -> float | None:
            a = g[col].abs()
            ok = g.loc[(a >= low) & (a <= high)]
            if ok.empty:
                return None
            best = (ok[col].abs() - target).abs().sort_values(kind="stable")
            return float(ok.loc[best.index[0], "strike"])

        out.append(LegPick(int(ts), int(expiry), bool(call), pick("delta"), pick("our_delta")))
    return out


def leg_steps(rows: pd.DataFrame, picks: Iterable[LegPick]) -> list[int]:
    """For each pick where both sides chose and chose differently: how many listed strikes
    (in ``rows`` for that minute, expiry and side) lie between Deribit's pick and ours."""
    strikes = {
        key: sorted(g["strike"].unique()) for key, g in rows.groupby(["ts_ms", "expiry_ms", "call"])
    }
    out: list[int] = []
    for p in picks:
        if p.by_deribit is None or p.by_ours is None or p.by_deribit == p.by_ours:
            continue
        listed = strikes.get((p.ts_ms, p.expiry_ms, p.call), [])
        if p.by_deribit in listed and p.by_ours in listed:
            out.append(abs(listed.index(p.by_deribit) - listed.index(p.by_ours)))
        else:  # cannot place the picks: count it as far apart, so the check fails
            out.append(len(listed) + 1)
    return out


def _window(rows: pd.DataFrame) -> pd.DataFrame:
    """Where the strategy trades: 7 to 30 days to expiry, |delta| 0.05 to 0.50."""
    return rows.loc[
        (rows["days"] >= 7)
        & (rows["days"] <= 30)
        & (rows["abs_delta"] >= 0.05)
        & (rows["abs_delta"] <= 0.50)
    ]


def _err(rows: pd.DataFrame, ours: str, theirs: str, scale: float = 1.0) -> pd.Series:
    return ((rows[ours] - rows[theirs]).abs() * scale).dropna()


def gate_checks(
    engine: pd.DataFrame, surface: pd.DataFrame, surfaces: Sequence[Surface]
) -> list[Check]:
    """Every gate measurement for one book against ``TOLERANCES``, in that order.

    ``engine`` and ``surface`` are that book's ``engine_rows`` and ``surface_rows`` over all the
    sampled minutes, and ``surfaces`` its fitted surfaces.
    """
    t = TOLERANCES
    checks: list[Check] = []

    def add(name: str, values: pd.Series | np.ndarray, how: str) -> None:
        v = np.asarray(values, dtype=float)
        if v.size == 0:
            measured = float("nan")
        elif how == "max":
            measured = float(v.max())
        elif how == "median":
            measured = float(np.median(v))
        else:  # a percentile, e.g. "p99"
            measured = float(np.percentile(v, float(how[1:])))
        checks.append(Check(name, measured, t[name], int(v.size)))

    day_plus = engine.loc[engine["days"] >= 1]
    add("engine_delta_max", _err(day_plus, "our_delta", "delta"), "max")
    add("engine_gamma_max", _err(day_plus, "our_gamma", "gamma"), "max")
    add("engine_vega_max", _err(day_plus, "our_vega", "vega"), "max")
    add("engine_theta_max", _err(engine.loc[engine["days"] >= 7], "our_theta", "theta"), "max")
    add("engine_iv_p99", _err(_window(engine), "our_iv", "mark_iv", 100), "p99")

    srf = _window(surface) if len(surface) else surface
    iv = _err(srf, "our_iv", "mark_iv", 100) if len(srf) else pd.Series(dtype=float)
    add("surface_iv_median", iv, "median")
    add("surface_iv_p99", iv, "p99")
    delta = _err(srf, "our_delta", "delta") if len(srf) else pd.Series(dtype=float)
    add("surface_delta_p99", delta, "p99")

    picks = leg_choice(surface) if len(surface) else []
    both = [p for p in picks if p.by_deribit is not None and p.by_ours is not None]
    steps = leg_steps(surface, both) if both else []
    share = len(steps) / len(both) if both else float("nan")
    checks.append(Check("leg_differs_share", share, t["leg_differs_share"], len(both)))
    checks.append(
        Check(
            "leg_differs_strikes",
            float(max(steps, default=0)) if both else float("nan"),
            t["leg_differs_strikes"],
            len(both),
        )
    )

    bp = [
        abs(f.forward.difference_bp)
        for s in surfaces
        for f in s.fits
        if f.forward.difference_bp is not None
    ]
    add("forward_parity_p95_bp", np.asarray(bp, dtype=float), "p95")
    return checks


def minute_frames(
    quotes: pd.DataFrame,
    ticks: pd.DataFrame,
    instruments: pd.DataFrame,
    ts_ms: int,
    books: Iterable[Book] = (Book.LINEAR, Book.INVERSE),
    config: SurfaceConfig = SurfaceConfig(),  # noqa: B008 (frozen, safe as a default)
) -> tuple[pd.DataFrame, pd.DataFrame, list[Surface]]:
    """Engine rows, surface rows and the surfaces of every book at one snapshot minute."""
    engines, surfaces_rows, surfaces = [], [], []
    for book in books:
        chain = ch.option_chain(quotes, instruments, book)
        surface = build_surface(
            chain,
            ch.futures(ticks, instruments, book),
            book,
            ts_ms,
            index_price=ch.index_price(ticks, book),
            config=config,
        )
        engines.append(engine_rows(chain, book, ts_ms))
        surfaces_rows.append(surface_rows(chain, surface))
        surfaces.append(surface)
    return (
        pd.concat(engines, ignore_index=True),
        pd.concat(surfaces_rows, ignore_index=True),
        surfaces,
    )


def open_interest_check(oi: pd.DataFrame, trades: pd.DataFrame) -> dict[str, float]:
    """How open interest moves against trades, per instrument and minute.

    ``oi``: ts_ms, instrument_name, open_interest (every minute); ``trades``: instrument_name,
    recv_ts_ms, amount. A minute "has a trade" if one was received in the minute before it.
    Returns the share of instrument-minutes where OI changed, with and without a trade, and for
    traded minutes how many minutes later OI first changed (median and 90th percentile).
    """
    oi = oi.sort_values(["instrument_name", "ts_ms"])
    oi = oi.assign(changed=oi.groupby("instrument_name")["open_interest"].diff().fillna(0) != 0)
    t = trades.assign(minute=(trades["recv_ts_ms"] // 60_000 + 1) * 60_000)
    traded = set(zip(t["instrument_name"], t["minute"], strict=True))
    key = list(zip(oi["instrument_name"], oi["ts_ms"], strict=True))
    has_trade = np.array([k in traded for k in key])
    changed = oi["changed"].to_numpy()
    first = oi.groupby("instrument_name")["ts_ms"].transform("min").to_numpy()
    valid = oi["ts_ms"].to_numpy() > first
    lags = []
    by_name = {n: g for n, g in oi.loc[oi["changed"]].groupby("instrument_name")}
    for name, minute in traded:
        g = by_name.get(name)
        if g is None:
            continue
        later = g.loc[g["ts_ms"] >= minute, "ts_ms"]
        if not later.empty and minute >= oi["ts_ms"].min() and minute <= oi["ts_ms"].max():
            lags.append((int(later.iloc[0]) - minute) / 60_000)
    lag = np.array(lags) if lags else np.array([np.nan])
    return {
        "instrument_minutes": float(valid.sum()),
        "traded_minutes": float((has_trade & valid).sum()),
        "changed_share_traded": float(changed[has_trade & valid].mean())
        if (has_trade & valid).any()
        else float("nan"),
        "changed_share_untraded": float(changed[~has_trade & valid].mean()),
        "lag_median_min": float(np.nanmedian(lag)),
        "lag_p90_min": float(np.nanpercentile(lag, 90)),
        "trades_with_lag": float(len(lags)),
    }
