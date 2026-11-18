"""Backtest results tables and risk_events (Phase 4).

- backtest_runs: one row per run, with everything needed to reproduce it (config, git commit,
  dataset hash, seed) and its result hash;
- backtest_orders, backtest_fills, backtest_positions, backtest_equity, backtest_groups: what the
  run did, minute by minute (money as exact numeric, USDC);
- risk_events: every rejection and every triggered monitor of the risk gateway, from backtests
  (run_id set) and, from Phase 8, the paper service.

Backtests run on the laptop (docs/backtest.md); the server gets these tables at its next
deploy, for Phase 8's risk_events. Plain PostgreSQL tables, no hypertables.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE TABLE backtest_runs (
    run_id           text PRIMARY KEY,
    name             text NOT NULL,
    config           jsonb NOT NULL,
    config_hash      text NOT NULL,
    git_commit       text NOT NULL,
    git_dirty        boolean NOT NULL,
    dataset_hash     text NOT NULL,
    dataset_versions jsonb NOT NULL,
    seed             integer NOT NULL,
    start_ms         bigint NOT NULL,
    end_ms           bigint NOT NULL,
    minutes          integer NOT NULL,
    started_ms       bigint NOT NULL,
    finished_ms      bigint NOT NULL,
    result_hash      text NOT NULL,
    final_equity     numeric,
    mlflow_run_id    text
);

CREATE TABLE backtest_orders (
    run_id     text NOT NULL REFERENCES backtest_runs (run_id) ON DELETE CASCADE,
    ticket_id  text NOT NULL,
    leg        integer NOT NULL,
    group_id   text,
    ts_ms      bigint NOT NULL,
    instrument text NOT NULL,
    direction  text NOT NULL CHECK (direction IN ('buy', 'sell')),
    intent     text NOT NULL CHECK (intent IN ('open', 'close')),
    requested  numeric NOT NULL,
    filled     numeric NOT NULL,
    status     text NOT NULL CHECK (status IN ('filled', 'partial', 'unfilled', 'rejected')),
    reason     text NOT NULL,
    detail     text NOT NULL,
    PRIMARY KEY (run_id, ticket_id, leg)
);

CREATE TABLE backtest_fills (
    run_id         text NOT NULL REFERENCES backtest_runs (run_id) ON DELETE CASCADE,
    fill_id        integer NOT NULL,
    ticket_id      text,
    group_id       text,
    ts_ms          bigint NOT NULL,
    instrument     text NOT NULL,
    kind           text NOT NULL CHECK (kind IN ('trade', 'settlement')),
    direction      text NOT NULL CHECK (direction IN ('buy', 'sell')),
    amount         numeric NOT NULL,
    price          numeric NOT NULL,
    fee            numeric NOT NULL,
    index_price    numeric,
    mid            numeric,
    mark           numeric,
    delivery_price numeric,
    PRIMARY KEY (run_id, fill_id)
);

CREATE TABLE backtest_positions (
    run_id      text NOT NULL REFERENCES backtest_runs (run_id) ON DELETE CASCADE,
    seq         integer NOT NULL,
    ts_ms       bigint NOT NULL,
    group_id    text NOT NULL,
    instrument  text NOT NULL,
    qty         numeric NOT NULL,
    entry_value numeric NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE backtest_equity (
    run_id             text NOT NULL REFERENCES backtest_runs (run_id) ON DELETE CASCADE,
    ts_ms              bigint NOT NULL,
    equity             numeric NOT NULL,
    liquidation_equity numeric NOT NULL,
    cash               numeric NOT NULL,
    option_value       numeric NOT NULL,
    hedge_unrealised   numeric NOT NULL,
    option_pnl         numeric NOT NULL,
    hedge_pnl          numeric NOT NULL,
    fees               numeric NOT NULL,
    funding            numeric NOT NULL,
    open_groups        integer NOT NULL,
    max_loss           numeric,
    initial_margin     numeric NOT NULL,
    maintenance_margin numeric NOT NULL,
    delta              numeric NOT NULL,
    vega               numeric NOT NULL,
    stale_marks        integer NOT NULL,
    PRIMARY KEY (run_id, ts_ms)
);

CREATE TABLE backtest_groups (
    run_id      text NOT NULL REFERENCES backtest_runs (run_id) ON DELETE CASCADE,
    group_id    text NOT NULL,
    opened_ms   bigint NOT NULL,
    closed_ms   bigint,
    thesis      text NOT NULL,
    reason      text NOT NULL,
    exit_reason text NOT NULL,
    realised    numeric NOT NULL,
    fees        numeric NOT NULL,
    PRIMARY KEY (run_id, group_id)
);

CREATE TABLE risk_events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source      text NOT NULL CHECK (source IN ('backtest', 'paper')),
    run_id      text,
    ts_ms       bigint NOT NULL,
    rule        text NOT NULL,
    action      text NOT NULL CHECK (action IN ('rejected', 'triggered', 'cleared')),
    detail      text NOT NULL,
    ticket_id   text,
    group_id    text,
    value       numeric,
    limit_value numeric,
    CHECK ((source = 'backtest') = (run_id IS NOT NULL))
);
CREATE INDEX risk_events_run_idx ON risk_events (run_id);
CREATE INDEX risk_events_ts_idx ON risk_events (ts_ms);
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    # Dropping these would delete results and the risk log. Not automated: CLAUDE.md needs
    # Damian's explicit yes and a verified backup first.
    raise NotImplementedError("0005 has no automatic downgrade")
