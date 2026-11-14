"""Database rows for the recorder's tables, in column order.

Each NamedTuple's field names are the table's column names, so the store can COPY them
directly. ``ts`` columns are timezone-aware UTC datetimes (TimescaleDB partitions on them); raw
exchange and receive times stay as integer UTC milliseconds.
"""

from datetime import date, datetime
from typing import NamedTuple

from optlab.domain.clock import ms_to_datetime
from optlab.domain.market import IndexPrice, Instrument, Ticker, Trade


class OptionQuoteRow(NamedTuple):
    ts: datetime  # snapshot minute
    instrument_name: str
    currency: str  # of bid, ask, mark and last prices
    bid: float | None
    bid_size: float
    ask: float | None
    ask_size: float
    mark_price: float | None
    mark_iv: float | None  # decimals
    bid_iv: float | None
    ask_iv: float | None
    delta: float | None  # Deribit's units
    gamma: float | None
    vega: float | None
    theta: float | None
    rho: float | None
    open_interest: float | None
    underlying_price: float | None
    underlying_index: str | None
    index_price: float | None
    interest_rate: float | None
    last_price: float | None
    state: str
    exchange_ts_ms: int
    recv_ts_ms: int


class UnderlyingTickRow(NamedTuple):
    ts: datetime  # snapshot minute
    instrument_name: str  # e.g. BTC-PERPETUAL, BTC-27NOV26, or an index such as btc_usd
    kind: str  # index, perpetual or future
    currency: str
    bid: float | None
    bid_size: float | None
    ask: float | None
    ask_size: float | None
    mark_price: float | None
    last_price: float | None
    index_price: float | None
    open_interest: float | None
    current_funding: float | None
    funding_8h: float | None
    interest_value: float | None
    estimated_delivery_price: float | None
    exchange_ts_ms: int
    recv_ts_ms: int


class OptionTradeRow(NamedTuple):
    trade_id: str
    ts: datetime  # exchange time of the trade
    instrument_name: str
    currency: str  # of price and mark_price
    price: float
    amount: float  # BTC
    direction: str
    iv: float | None
    mark_price: float | None
    index_price: float | None
    trade_seq: int | None
    tick_direction: int | None
    block_trade_id: str | None
    liquidation: str | None
    combo_id: str | None
    exchange_ts_ms: int
    recv_ts_ms: int


class RecorderStatusRow(NamedTuple):
    ts: datetime
    run_id: str
    started_ms: int
    rss_bytes: int | None  # resident memory now
    peak_rss_bytes: int | None  # highest resident memory of this run
    connections_up: int
    connections: int
    channels: int
    msgs_per_s: float
    last_option_rows: int
    db_jobs_waiting: int
    decode_errors: int
    raw_day: date  # UTC day the raw stream is writing
    raw_bytes_day: int  # bytes of raw files for raw_day so far


def option_quote_row(ts_ms: int, instrument: Instrument, t: Ticker) -> OptionQuoteRow:
    g = t.greeks
    return OptionQuoteRow(
        ts=ms_to_datetime(ts_ms),
        instrument_name=instrument.name,
        currency=instrument.price_currency,
        bid=t.bid,
        bid_size=t.bid_size,
        ask=t.ask,
        ask_size=t.ask_size,
        mark_price=t.mark_price,
        mark_iv=t.mark_iv,
        bid_iv=t.bid_iv,
        ask_iv=t.ask_iv,
        delta=None if g is None else g.delta,
        gamma=None if g is None else g.gamma,
        vega=None if g is None else g.vega,
        theta=None if g is None else g.theta,
        rho=None if g is None else g.rho,
        open_interest=t.open_interest,
        underlying_price=t.underlying_price,
        underlying_index=t.underlying_index,
        index_price=t.index_price,
        interest_rate=t.interest_rate,
        last_price=t.last_price,
        state=t.state,
        exchange_ts_ms=t.exchange_ts_ms,
        recv_ts_ms=t.recv_ts_ms,
    )


def underlying_row(ts_ms: int, instrument: Instrument, t: Ticker) -> UnderlyingTickRow:
    return UnderlyingTickRow(
        ts=ms_to_datetime(ts_ms),
        instrument_name=instrument.name,
        kind=instrument.kind.value,
        currency=instrument.price_currency,
        bid=t.bid,
        bid_size=t.bid_size,
        ask=t.ask,
        ask_size=t.ask_size,
        mark_price=t.mark_price,
        last_price=t.last_price,
        index_price=t.index_price,
        open_interest=t.open_interest,
        current_funding=t.current_funding,
        funding_8h=t.funding_8h,
        interest_value=t.interest_value,
        estimated_delivery_price=t.estimated_delivery_price,
        exchange_ts_ms=t.exchange_ts_ms,
        recv_ts_ms=t.recv_ts_ms,
    )


def index_row(ts_ms: int, p: IndexPrice, currency: str) -> UnderlyingTickRow:
    return UnderlyingTickRow(
        ts=ms_to_datetime(ts_ms),
        instrument_name=p.index_name,
        kind="index",
        currency=currency,
        bid=None,
        bid_size=None,
        ask=None,
        ask_size=None,
        mark_price=None,
        last_price=None,
        index_price=p.price,
        open_interest=None,
        current_funding=None,
        funding_8h=None,
        interest_value=None,
        estimated_delivery_price=None,
        exchange_ts_ms=p.exchange_ts_ms,
        recv_ts_ms=p.recv_ts_ms,
    )


def option_trade_row(instrument: Instrument, t: Trade) -> OptionTradeRow:
    return OptionTradeRow(
        trade_id=t.trade_id,
        ts=ms_to_datetime(t.exchange_ts_ms),
        instrument_name=t.instrument_name,
        currency=instrument.price_currency,
        price=t.price,
        amount=t.amount,
        direction=t.direction.value,
        iv=t.iv,
        mark_price=t.mark_price,
        index_price=t.index_price,
        trade_seq=t.trade_seq,
        tick_direction=t.tick_direction,
        block_trade_id=t.block_trade_id,
        liquidation=t.liquidation,
        combo_id=t.combo_id,
        exchange_ts_ms=t.exchange_ts_ms,
        recv_ts_ms=t.recv_ts_ms,
    )
