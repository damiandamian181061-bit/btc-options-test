"""Phase 3: the volatility surface and features, written every minute by the surface worker.

- vol_expiries: one row per book, listed expiry and snapshot minute: the forward (the dated
  future's mark, or put-call parity) and the parity cross-check, the smile fit's quality, its
  ATM, 25-delta and 10-delta IVs, and the butterfly and calendar checks.
- vol_surface: one row per book, tenor (1, 3, 7, 14, 30, 60, 90 days), bucket (10- and
  25-delta puts and calls, ATM) and minute, interpolated in total variance.
- features: one row per book and minute (ATM IVs, risk reversal, butterfly, term slope,
  realised vol, IV minus realised vol, basis, funding), from data at or before the minute.

IVs are decimals, tau in years (ACT/365), times UTC milliseconds unless a column is a
timestamptz. No existing table is changed: the recorder, the nightly export and the rough paper
service keep running on this revision (optlab.market_data.schema). These tables hold derived
data that can be rebuilt from the recorded quotes, so they are not in the nightly R2 export.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE TABLE vol_expiries (
    ts             timestamptz NOT NULL,
    book           text NOT NULL CHECK (book IN ('linear', 'inverse')),
    expiry_ms      bigint NOT NULL,
    tau            double precision NOT NULL,
    forward        double precision,
    forward_source text NOT NULL CHECK (forward_source IN ('future', 'parity', 'none')),
    future_price   double precision,
    parity_forward double precision,
    parity_pairs   integer NOT NULL,
    points         integer NOT NULL,
    fit_rmse       double precision,
    k_min          double precision,
    k_max          double precision,
    atm_iv         double precision,
    iv_25d_call    double precision,
    iv_25d_put     double precision,
    iv_10d_call    double precision,
    iv_10d_put     double precision,
    butterfly_min  double precision,
    calendar_gap   double precision,
    used           boolean NOT NULL,
    note           text NOT NULL DEFAULT '',
    PRIMARY KEY (book, expiry_ms, ts)
);
SELECT create_hypertable('vol_expiries', by_range('ts', INTERVAL '1 day'));
ALTER TABLE vol_expiries SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'book',
    timescaledb.compress_orderby = 'ts DESC, expiry_ms'
);
SELECT add_compression_policy('vol_expiries', compress_after => INTERVAL '7 days');

CREATE TABLE vol_surface (
    ts              timestamptz NOT NULL,
    book            text NOT NULL CHECK (book IN ('linear', 'inverse')),
    tenor_days      integer NOT NULL CHECK (tenor_days > 0),
    bucket          text NOT NULL
        CHECK (bucket IN ('10d_put', '25d_put', 'atm', '25d_call', '10d_call')),
    iv              double precision NOT NULL,
    total_variance  double precision NOT NULL,
    extrapolated    boolean NOT NULL,
    lower_expiry_ms bigint NOT NULL,
    upper_expiry_ms bigint NOT NULL,
    PRIMARY KEY (book, tenor_days, bucket, ts)
);
SELECT create_hypertable('vol_surface', by_range('ts', INTERVAL '1 day'));
ALTER TABLE vol_surface SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'book',
    timescaledb.compress_orderby = 'ts DESC, tenor_days, bucket'
);
SELECT add_compression_policy('vol_surface', compress_after => INTERVAL '7 days');

CREATE TABLE features (
    ts               timestamptz NOT NULL,
    book             text NOT NULL CHECK (book IN ('linear', 'inverse')),
    index_price      double precision,
    atm_iv_7d        double precision,
    atm_iv_30d       double precision,
    atm_iv_90d       double precision,
    rr25_30d         double precision,
    bf25_30d         double precision,
    term_slope_7_30  double precision,
    term_slope_30_90 double precision,
    rv_7d            double precision,
    rv_30d           double precision,
    rv_30d_returns   integer NOT NULL,
    rv_30d_delivery  integer NOT NULL,
    iv_rv_30d        double precision,
    basis_30d        double precision,
    perp_funding_8h  double precision,
    computed_ms      bigint NOT NULL,
    PRIMARY KEY (book, ts)
);
CREATE INDEX features_ts_idx ON features (ts);
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    # Dropping these deletes the stored surface history. Not automated: CLAUDE.md needs
    # Damian's explicit yes and a verified backup first.
    raise RuntimeError("0004 has no automatic downgrade: it would delete the surface history")
