"""Recorder tables: instruments, option_quotes, underlying_ticks, option_trades,
delivery_prices and connection_events.

option_quotes and underlying_ticks are TimescaleDB hypertables (one-day chunks), compressed
once their chunks are older than 7 days.

Revision ID: 0001
Revises:
Create Date: 2026-11-12
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Every instrument the recorder has followed. Fields come from Deribit's instrument metadata.
-- Times are UTC milliseconds; prices of this instrument are in quote_currency.
CREATE TABLE instruments (
    instrument_name        text PRIMARY KEY,
    exchange_instrument_id bigint NOT NULL,
    kind                   text NOT NULL CHECK (kind IN ('option', 'future', 'perpetual')),
    book                   text NOT NULL CHECK (book IN ('linear', 'inverse')),
    base_currency          text NOT NULL,
    quote_currency         text NOT NULL,
    settlement_currency    text NOT NULL,
    counter_currency       text NOT NULL,
    price_index            text NOT NULL,
    option_type            text CHECK (option_type IN ('call', 'put')),
    strike                 double precision,
    expiry_ms              bigint,
    contract_size          double precision NOT NULL,
    tick_size              double precision NOT NULL,
    min_trade_amount       double precision NOT NULL,
    settlement_period      text NOT NULL,
    creation_ms            bigint NOT NULL,
    first_seen_ms          bigint NOT NULL,
    last_seen_ms           bigint NOT NULL,
    is_active              boolean NOT NULL DEFAULT true,
    delisted_ms            bigint,
    CHECK ((kind = 'option') = (option_type IS NOT NULL AND strike IS NOT NULL)),
    CHECK ((kind = 'perpetual') = (expiry_ms IS NULL))
);
CREATE INDEX instruments_expiry_idx ON instruments (expiry_ms);

-- One row per option per minute: the latest ticker received by ts (the snapshot minute).
-- Prices are in currency (BTC for inverse, USDC for linear); IVs are decimals; Greeks are in
-- Deribit's units; sizes in BTC; an empty side has a NULL price and size 0.
CREATE TABLE option_quotes (
    ts               timestamptz NOT NULL,
    instrument_name  text NOT NULL,
    currency         text NOT NULL,
    bid              double precision,
    bid_size         double precision NOT NULL,
    ask              double precision,
    ask_size         double precision NOT NULL,
    mark_price       double precision,
    mark_iv          double precision,
    bid_iv           double precision,
    ask_iv           double precision,
    delta            double precision,
    gamma            double precision,
    vega             double precision,
    theta            double precision,
    rho              double precision,
    open_interest    double precision,
    underlying_price double precision,
    underlying_index text,
    index_price      double precision,
    interest_rate    double precision,
    last_price       double precision,
    state            text NOT NULL,
    exchange_ts_ms   bigint NOT NULL,
    recv_ts_ms       bigint NOT NULL,
    PRIMARY KEY (instrument_name, ts)
);
-- create_hypertable also adds the index option_quotes_ts_idx on (ts DESC).
SELECT create_hypertable('option_quotes', by_range('ts', INTERVAL '1 day'));
ALTER TABLE option_quotes SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instrument_name',
    timescaledb.compress_orderby = 'ts DESC'
);
SELECT add_compression_policy('option_quotes', compress_after => INTERVAL '7 days');

-- One row per minute for each price index (kind 'index'), perpetual and dated future.
CREATE TABLE underlying_ticks (
    ts                       timestamptz NOT NULL,
    instrument_name          text NOT NULL,
    kind                     text NOT NULL CHECK (kind IN ('index', 'perpetual', 'future')),
    currency                 text NOT NULL,
    bid                      double precision,
    bid_size                 double precision,
    ask                      double precision,
    ask_size                 double precision,
    mark_price               double precision,
    last_price               double precision,
    index_price              double precision,
    open_interest            double precision,
    current_funding          double precision,
    funding_8h               double precision,
    interest_value           double precision,
    estimated_delivery_price double precision,
    exchange_ts_ms           bigint NOT NULL,
    recv_ts_ms               bigint NOT NULL,
    PRIMARY KEY (instrument_name, ts)
);
-- create_hypertable also adds the index underlying_ticks_ts_idx on (ts DESC).
SELECT create_hypertable('underlying_ticks', by_range('ts', INTERVAL '1 day'));
ALTER TABLE underlying_ticks SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instrument_name',
    timescaledb.compress_orderby = 'ts DESC'
);
SELECT add_compression_policy('underlying_ticks', compress_after => INTERVAL '7 days');

-- Every public option trade in the two books, stored once per trade_id.
CREATE TABLE option_trades (
    trade_id        text PRIMARY KEY,
    ts              timestamptz NOT NULL,
    instrument_name text NOT NULL,
    currency        text NOT NULL,
    price           double precision NOT NULL,
    amount          double precision NOT NULL,
    direction       text NOT NULL CHECK (direction IN ('buy', 'sell')),
    iv              double precision,
    mark_price      double precision,
    index_price     double precision,
    trade_seq       bigint,
    tick_direction  smallint,
    block_trade_id  text,
    liquidation     text,
    combo_id        text,
    exchange_ts_ms  bigint NOT NULL,
    recv_ts_ms      bigint NOT NULL
);
CREATE INDEX option_trades_instrument_ts_idx ON option_trades (instrument_name, ts);
CREATE INDEX option_trades_ts_idx ON option_trades (ts);

-- Deribit's daily delivery (settlement) price per index, as an exact decimal.
CREATE TABLE delivery_prices (
    index_name     text NOT NULL,
    delivery_date  date NOT NULL,
    delivery_price numeric NOT NULL,
    currency       text NOT NULL,
    recorded_ms    bigint NOT NULL,
    PRIMARY KEY (index_name, delivery_date)
);

-- Connects, disconnects, reconnects and recorder starts and stops.
CREATE TABLE connection_events (
    id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts     timestamptz NOT NULL,
    ts_ms  bigint NOT NULL,
    source text NOT NULL,
    event  text NOT NULL,
    detail text NOT NULL DEFAULT ''
);
CREATE INDEX connection_events_ts_idx ON connection_events (ts);
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    # A downgrade would drop every recorded table. CLAUDE.md forbids dropping tables on the
    # server without Damian's explicit yes and a verified backup, so it is not automated.
    raise RuntimeError("0001 has no automatic downgrade: it would delete recorded data")
