"""ROUGH (Phase 2B) paper-service tables, and host_boots for labelling reboot gaps.

- rough_runs: one row per paper-service run, updated every loop (the gate's "ran 24 hours").
- rough_decisions: one row per UTC day: the daily decision, with its reason, entered or not.
- rough_spreads: one row per paper spread, open until it is closed or settled.
- rough_fills: every paper fill (entry, exit, settlement), each with its fee and cash change.
- rough_equity: cash, open spreads at mark and equity, once per processed snapshot minute.
- host_boots: when the server booted, so the data checks can label the gap a restart leaves.

Money columns are exact numeric in USDC; amounts in BTC; times UTC milliseconds unless a
column is a timestamptz. No existing table is changed. The rough tables are replaced in later
phases (docs/rough-version.md); they are not part of the nightly R2 export.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-15
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE TABLE rough_runs (
    run_id           text PRIMARY KEY,
    started_ms       bigint NOT NULL,
    last_ms          bigint NOT NULL,
    last_snapshot_ms bigint,
    minutes          integer NOT NULL DEFAULT 0,
    skipped          integer NOT NULL DEFAULT 0,
    note             text NOT NULL DEFAULT ''
);

CREATE TABLE rough_spreads (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    side              text NOT NULL CHECK (side IN ('bull put', 'bear call')),
    expiry_ms         bigint NOT NULL,
    short_instrument  text NOT NULL,
    long_instrument   text NOT NULL,
    short_strike      numeric NOT NULL,
    long_strike       numeric NOT NULL,
    short_delta       double precision NOT NULL,
    amount            numeric NOT NULL CHECK (amount > 0),
    currency          text NOT NULL,
    entry_credit      numeric NOT NULL,
    credit_after_fees numeric NOT NULL,
    max_loss          numeric NOT NULL,
    opened_ms         bigint NOT NULL,
    thesis            text NOT NULL,
    status            text NOT NULL CHECK (status IN ('open', 'closed', 'settled')),
    exit_reason       text,
    closed_ms         bigint,
    exit_price        numeric,
    delivery_price    numeric,
    pnl               numeric,
    thesis_met        boolean,
    CHECK ((status = 'open') = (closed_ms IS NULL))
);
CREATE INDEX rough_spreads_open_idx ON rough_spreads (status) WHERE status = 'open';

CREATE TABLE rough_fills (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    spread_id       bigint NOT NULL REFERENCES rough_spreads (id),
    ts_ms           bigint NOT NULL,
    recorded_ms     bigint NOT NULL,
    instrument_name text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('entry', 'exit', 'settlement')),
    direction       text NOT NULL CHECK (direction IN ('buy', 'sell')),
    amount          numeric NOT NULL,
    price           numeric NOT NULL,
    fee             numeric NOT NULL,
    cash            numeric NOT NULL,
    currency        text NOT NULL
);
CREATE INDEX rough_fills_spread_idx ON rough_fills (spread_id);

CREATE TABLE rough_decisions (
    day           date PRIMARY KEY,
    ts_ms         bigint NOT NULL,
    decided_ms    bigint NOT NULL,
    iv30          double precision,
    rv30          double precision,
    index_price   double precision,
    trend_average double precision,
    side          text,
    short_legs    integer NOT NULL DEFAULT 0,
    passed        integer NOT NULL DEFAULT 0,
    action        text NOT NULL CHECK (action IN ('entered', 'no trade')),
    reason        text NOT NULL,
    spread_id     bigint REFERENCES rough_spreads (id),
    CHECK ((action = 'entered') = (spread_id IS NOT NULL))
);

CREATE TABLE rough_equity (
    ts            timestamptz PRIMARY KEY,
    cash          numeric NOT NULL,
    open_value    numeric NOT NULL,
    equity        numeric NOT NULL,
    open_spreads  integer NOT NULL,
    open_max_loss numeric NOT NULL
);

CREATE TABLE host_boots (
    boot_ms       bigint PRIMARY KEY,
    first_seen_ms bigint NOT NULL
);
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    # Dropping these would delete the paper ledger. Not automated: CLAUDE.md needs Damian's
    # explicit yes and a verified backup first.
    raise RuntimeError("0003 has no automatic downgrade: it would delete the paper ledger")
