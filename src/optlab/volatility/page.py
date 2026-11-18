"""Data for the status page's "Volatility surface" tab. Read-only: plain SELECTs on the page's
read-only connection, and the live smiles from the latest snapshot through MarketDataView."""

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import psycopg

from optlab.domain.clock import ms_to_datetime
from optlab.domain.market import Book
from optlab.market_data.view import DatabaseSource, MarketDataView
from optlab.volatility import chain as ch
from optlab.volatility.smile import smile_points
from optlab.volatility.surface import (
    BUCKETS,
    TENORS_DAYS,
    Bucket,
    ExpiryFit,
    Surface,
    build_surface,
)


def latest_surface(conn: psycopg.Connection[Any], book: Book) -> tuple[int | None, pd.DataFrame]:
    """The newest stored minute's surface: tenor x bucket, IV and whether it is extrapolated."""
    row = conn.execute(
        "SELECT utc_ms(max(ts)) FROM vol_surface WHERE book = %s", (book.value,)
    ).fetchone()
    if row is None or row[0] is None:
        return None, pd.DataFrame(columns=["tenor_days", "bucket", "iv", "extrapolated"])
    ts = int(row[0])
    rows = conn.execute(
        "SELECT tenor_days, bucket, iv, extrapolated FROM vol_surface "
        "WHERE book = %s AND ts = %s ORDER BY tenor_days, bucket",
        (book.value, ms_to_datetime(ts)),
    ).fetchall()
    return ts, pd.DataFrame(rows, columns=["tenor_days", "bucket", "iv", "extrapolated"])


def surface_grid(rows: pd.DataFrame) -> pd.DataFrame:
    """Tenors down, buckets across, IV in % to 2 decimals; extrapolated cells end in "*"."""
    out = pd.DataFrame(index=[f"{d} d" for d in TENORS_DAYS], columns=[b.value for b in BUCKETS])
    for r in rows.itertuples(index=False):
        out.loc[f"{r.tenor_days} d", r.bucket] = f"{r.iv * 100:.2f}" + (
            "*" if r.extrapolated else ""
        )
    return out.fillna("-")


def latest_expiries(conn: psycopg.Connection[Any], book: Book) -> pd.DataFrame:
    """The newest stored minute's per-expiry rows (forward, fit quality, checks)."""
    rows = conn.execute(
        "SELECT expiry_ms, tau, forward, forward_source, future_price, parity_forward, "
        "parity_pairs, points, fit_rmse, atm_iv, iv_25d_call, iv_25d_put, butterfly_min, "
        "calendar_gap, used, note FROM vol_expiries "
        "WHERE book = %(b)s AND ts = (SELECT max(ts) FROM vol_expiries WHERE book = %(b)s) "
        "ORDER BY expiry_ms",
        {"b": book.value},
    ).fetchall()
    cols = [
        "expiry_ms", "tau", "forward", "forward_source", "future_price", "parity_forward",
        "parity_pairs", "points", "fit_rmse", "atm_iv", "iv_25d_call", "iv_25d_put",
        "butterfly_min", "calendar_gap", "used", "note",
    ]  # fmt: skip
    return pd.DataFrame(rows, columns=cols)


def expiry_table(rows: pd.DataFrame) -> pd.DataFrame:
    """The per-expiry rows as the page shows them."""

    def pct(x: Any) -> str:
        return "" if x is None or pd.isna(x) else f"{x * 100:.2f}"

    out = []
    for r in rows.itertuples(index=False):
        diff = (
            (r.future_price / r.parity_forward - 1) * 1e4
            if r.future_price is not None
            and r.parity_forward is not None
            and not pd.isna(r.future_price)
            and not pd.isna(r.parity_forward)
            else None
        )
        out.append(
            {
                "expiry": f"{ms_to_datetime(int(r.expiry_ms)):%d %b %y}",
                "days": round(float(r.tau) * 365, 2),
                "forward": "" if r.forward is None or pd.isna(r.forward) else f"{r.forward:,.2f}",
                "source": r.forward_source,
                "future - parity (bp)": "" if diff is None else f"{diff:+.1f}",
                "quotes": int(r.points),
                "fit RMSE (vol pts)": pct(r.fit_rmse),
                "ATM %": pct(r.atm_iv),
                "25d RR (vol pts)": (
                    ""
                    if r.iv_25d_call is None
                    or r.iv_25d_put is None
                    or pd.isna(r.iv_25d_call)
                    or pd.isna(r.iv_25d_put)
                    else f"{(r.iv_25d_call - r.iv_25d_put) * 100:+.2f}"
                ),
                "butterfly": (
                    ""
                    if r.butterfly_min is None or pd.isna(r.butterfly_min)
                    else ("ok" if r.butterfly_min >= 0 else "ARBITRAGE")
                ),
                "calendar": (
                    ""
                    if r.calendar_gap is None or pd.isna(r.calendar_gap)
                    else ("ok" if r.calendar_gap >= -1e-6 else "ARBITRAGE")
                ),
                "used": "yes" if r.used else "no",
                "note": r.note,
            }
        )
    return pd.DataFrame(out)


def feature_history(conn: psycopg.Connection[Any], book: Book, since_ms: int) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT utc_ms(ts), atm_iv_7d, atm_iv_30d, atm_iv_90d, rr25_30d, bf25_30d, rv_30d, "
        "rv_30d_returns, rv_30d_delivery, iv_rv_30d, basis_30d, perp_funding_8h, index_price "
        "FROM features WHERE book = %s AND ts >= %s ORDER BY ts",
        (book.value, ms_to_datetime(since_ms)),
    ).fetchall()
    cols = [
        "ts_ms", "atm_iv_7d", "atm_iv_30d", "atm_iv_90d", "rr25_30d", "bf25_30d", "rv_30d",
        "rv_30d_returns", "rv_30d_delivery", "iv_rv_30d", "basis_30d", "perp_funding_8h",
        "index_price",
    ]  # fmt: skip
    return pd.DataFrame(rows, columns=cols)


@dataclass(frozen=True)
class LiveSmile:
    expiry_ms: int
    days: float
    label: str
    info: str
    curve: pd.DataFrame  # strike, iv (%), series
    points: pd.DataFrame  # strike, series, iv (%), and the table view's columns


def smile_frames(fit: ExpiryFit, chain: pd.DataFrame, book: Book) -> LiveSmile | None:
    """Our fitted smile, our mid IVs from the quotes, and Deribit's mark IVs, by strike."""
    s, fwd = fit.smile, fit.forward.forward
    if s is None or fwd is None:
        return None
    group = chain.loc[chain["expiry_ms"] == fit.expiry_ms]
    pts = smile_points(group, fwd, fit.tau, book, s.config)
    k = np.linspace(s.k_min, s.k_max, 81)
    curve = pd.DataFrame({"strike": fwd * np.exp(k), "iv": s.iv(k) * 100, "series": "Our fit"})
    otm = group.loc[(group["call"] == (group["strike"] >= fwd)) & group["mark_iv"].notna()]
    otm = otm.loc[
        (otm["strike"] >= curve["strike"].min()) & (otm["strike"] <= curve["strike"].max())
    ]
    points = pd.concat(
        [
            pd.DataFrame(
                {
                    "strike": pts["strike"],
                    "iv": pts["mid_iv"] * 100,
                    "series": "Quote mid (ours)",
                    "bid IV %": pts["bid_iv"] * 100,
                    "ask IV %": pts["ask_iv"] * 100,
                }
            ),
            pd.DataFrame(
                {"strike": otm["strike"], "iv": otm["mark_iv"] * 100, "series": "Deribit mark"}
            ),
        ],
        ignore_index=True,
    )
    diff = fit.forward.difference_bp
    info = (
        f"Forward {fwd:,.2f} ({fit.forward.source.value}"
        + ("" if diff is None else f", future - parity {diff:+.1f} bp")
        + f"); {fit.n_points} quotes; fit RMSE {s.rmse * 100:.2f} vol pts; "
        + f"ATM {fit.bucket_iv.get(Bucket.ATM, float('nan')) * 100:.2f}%; butterfly "
        + ("ok" if (fit.butterfly_min or 0) >= 0 else "ARBITRAGE")
        + (
            ""
            if fit.calendar_ok is None
            else ", calendar " + ("ok" if fit.calendar_ok else "ARBITRAGE")
        )
    )
    label = f"{ms_to_datetime(fit.expiry_ms):%d %b %y} ({fit.tau * 365:.1f} d)"
    return LiveSmile(fit.expiry_ms, fit.tau * 365, label, info, curve, points)


def live_smiles(database_url: str, book: Book, now_ms: int) -> tuple[int | None, list[LiveSmile]]:
    """The smiles of the latest snapshot, fitted now with the worker's code."""
    source = DatabaseSource(database_url)
    try:
        view = MarketDataView(source, now_ms)
        snap = view.snapshot_ms()
        if snap is None:
            return None, []
        view = MarketDataView(source, snap)
        ticks = view.underlyings()
        instruments = view.instruments()
        chain = ch.option_chain(view.option_chain(), instruments, book)
        surface: Surface = build_surface(
            chain,
            ch.futures(ticks, instruments, book),
            book,
            snap,
            index_price=ch.index_price(ticks, book),
        )
    finally:
        source.close()
    out = [x for f in surface.fits if (x := smile_frames(f, chain, book)) is not None]
    return snap, out
