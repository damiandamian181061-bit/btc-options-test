"""Writes and reads for the surface tables: vol_expiries, vol_surface and features.

One read-write connection in autocommit mode; each minute is stored in one transaction, so a
restart never leaves half a minute. Rows are keyed by book and minute and inserted with
``ON CONFLICT DO NOTHING``: a minute processed twice is stored once.
"""

from collections.abc import Iterable
from typing import Any

import psycopg

from optlab.domain.clock import ms_to_datetime
from optlab.volatility.features import Features
from optlab.volatility.surface import Bucket, Surface

EXPIRY_COLUMNS = (
    "ts", "book", "expiry_ms", "tau", "forward", "forward_source", "future_price",
    "parity_forward", "parity_pairs", "points", "fit_rmse", "k_min", "k_max", "atm_iv",
    "iv_25d_call", "iv_25d_put", "iv_10d_call", "iv_10d_put", "butterfly_min", "calendar_gap",
    "used", "note",
)  # fmt: skip
SURFACE_COLUMNS = (
    "ts", "book", "tenor_days", "bucket", "iv", "total_variance", "extrapolated",
    "lower_expiry_ms", "upper_expiry_ms",
)  # fmt: skip
FEATURE_COLUMNS = (
    "ts", "book", "index_price", "atm_iv_7d", "atm_iv_30d", "atm_iv_90d", "rr25_30d",
    "bf25_30d", "term_slope_7_30", "term_slope_30_90", "rv_7d", "rv_30d", "rv_30d_returns",
    "rv_30d_delivery", "iv_rv_30d", "basis_30d", "perp_funding_8h", "computed_ms",
)  # fmt: skip


def connect(database_url: str) -> psycopg.Connection[Any]:
    return psycopg.connect(database_url, autocommit=True, application_name="optlab-surface")


def expiry_rows(surface: Surface) -> list[tuple[Any, ...]]:
    ts = ms_to_datetime(surface.ts_ms)
    out = []
    for f in surface.fits:
        s = f.smile
        iv = f.bucket_iv
        out.append(
            (
                ts, surface.book.value, f.expiry_ms, f.tau, f.forward.forward,
                f.forward.source.value, f.forward.future, f.forward.parity,
                f.forward.parity_pairs, f.n_points, None if s is None else s.rmse,
                None if s is None else s.k_min, None if s is None else s.k_max,
                iv.get(Bucket.ATM), iv.get(Bucket.CALL_25), iv.get(Bucket.PUT_25),
                iv.get(Bucket.CALL_10), iv.get(Bucket.PUT_10), f.butterfly_min, f.calendar_gap,
                f.used, f.note,
            )
        )  # fmt: skip
    return out


def surface_rows(surface: Surface) -> list[tuple[Any, ...]]:
    ts = ms_to_datetime(surface.ts_ms)
    return [
        (
            ts,
            surface.book.value,
            p.tenor_days,
            p.bucket.value,
            p.iv,
            p.total_variance,
            p.extrapolated,
            p.lower_expiry_ms,
            p.upper_expiry_ms,
        )
        for p in surface.points
    ]


def feature_row(f: Features, computed_ms: int) -> tuple[Any, ...]:
    return (
        ms_to_datetime(f.ts_ms), f.book.value, f.index_price, f.atm_iv_7d, f.atm_iv_30d,
        f.atm_iv_90d, f.rr25_30d, f.bf25_30d, f.term_slope_7_30, f.term_slope_30_90, f.rv_7d,
        f.rv_30d, f.rv_30d_returns, f.rv_30d_delivery, f.iv_rv_30d, f.basis_30d,
        f.perp_funding_8h, computed_ms,
    )  # fmt: skip


def _insert(
    cur: psycopg.Cursor[Any], table: str, columns: tuple[str, ...], rows: Iterable[tuple[Any, ...]]
) -> int:
    rows = list(rows)
    if not rows:
        return 0
    cols = ", ".join(columns)
    marks = ", ".join(["%s"] * len(columns))
    cur.executemany(
        f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT DO NOTHING",  # noqa: S608 (fixed names)
        rows,
    )
    return len(rows)


def save_minute(
    conn: psycopg.Connection[Any],
    surfaces: Iterable[Surface],
    features: Iterable[Features],
    *,
    computed_ms: int,
) -> tuple[int, int, int]:
    """Store one minute (every book) in one transaction; rows offered per table."""
    with conn.transaction(), conn.cursor() as cur:
        surfaces = list(surfaces)
        e = _insert(
            cur, "vol_expiries", EXPIRY_COLUMNS, (r for s in surfaces for r in expiry_rows(s))
        )
        p = _insert(
            cur, "vol_surface", SURFACE_COLUMNS, (r for s in surfaces for r in surface_rows(s))
        )
        f = _insert(
            cur, "features", FEATURE_COLUMNS, (feature_row(x, computed_ms) for x in features)
        )
    return e, p, f


def last_minute(conn: psycopg.Connection[Any]) -> int | None:
    """The newest minute stored in features (UTC ms), or None."""
    row = conn.execute("SELECT utc_ms(max(ts)) FROM features").fetchone()
    return None if row is None or row[0] is None else int(row[0])
