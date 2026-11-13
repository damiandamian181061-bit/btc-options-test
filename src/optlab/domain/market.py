"""Typed market-data records used everywhere outside the exchange adapter.

Conventions (CLAUDE.md): timestamps are UTC milliseconds; implied volatilities are decimals
(0.52 means 52%); every price is in the instrument's ``price_currency`` (BTC for inverse option
premiums, USDC for linear options, USD for inverse futures and the ``btc_usd`` index); sizes are
in BTC. An empty side of the book is ``None``, never a zero price.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class Book(StrEnum):
    """Which Deribit book an instrument belongs to."""

    LINEAR = "linear"  # BTC_USDC: quoted, margined and settled in USDC
    INVERSE = "inverse"  # BTC: option premiums and settlement in BTC


class InstrumentKind(StrEnum):
    OPTION = "option"
    FUTURE = "future"  # dated future
    PERPETUAL = "perpetual"


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


class Direction(StrEnum):
    """The taker's side of a trade."""

    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class Instrument:
    """One tradable instrument, with every field read from the exchange's metadata."""

    name: str
    exchange_id: int
    kind: InstrumentKind
    book: Book
    base_currency: str
    quote_currency: str
    settlement_currency: str
    counter_currency: str
    price_index: str
    contract_size: float
    tick_size: float
    min_trade_amount: float
    creation_ms: int
    settlement_period: str
    is_active: bool
    expiry_ms: int | None = None  # None for perpetuals
    strike: float | None = None
    option_type: OptionType | None = None

    @property
    def price_currency(self) -> str:
        """Currency of this instrument's prices (premiums for options)."""
        return self.quote_currency


@dataclass(frozen=True, slots=True)
class Greeks:
    """Option Greeks exactly as the exchange reports them (Deribit's units)."""

    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float


@dataclass(frozen=True, slots=True)
class Ticker:
    """Latest state of one instrument. Option-only and future-only fields are None elsewhere."""

    instrument_name: str
    exchange_ts_ms: int
    recv_ts_ms: int
    state: str
    bid: float | None
    bid_size: float
    ask: float | None
    ask_size: float
    mark_price: float | None
    last_price: float | None
    index_price: float | None
    open_interest: float | None
    estimated_delivery_price: float | None
    # Options
    mark_iv: float | None = None
    bid_iv: float | None = None
    ask_iv: float | None = None
    greeks: Greeks | None = None
    underlying_price: float | None = None
    underlying_index: str | None = None
    interest_rate: float | None = None
    # Futures and perpetuals
    current_funding: float | None = None
    funding_8h: float | None = None
    interest_value: float | None = None


@dataclass(frozen=True, slots=True)
class IndexPrice:
    """A price index value, such as ``btc_usd`` or ``btc_usdc``."""

    index_name: str
    price: float
    exchange_ts_ms: int
    recv_ts_ms: int


@dataclass(frozen=True, slots=True)
class Trade:
    """One public trade. ``amount`` is in BTC for options."""

    trade_id: str
    instrument_name: str
    exchange_ts_ms: int
    recv_ts_ms: int
    price: float
    amount: float
    direction: Direction
    trade_seq: int | None = None
    iv: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    tick_direction: int | None = None
    block_trade_id: str | None = None
    liquidation: str | None = None
    combo_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryPrice:
    """The price an index settled at on one expiry date (exact decimal)."""

    index_name: str
    delivery_date: date
    price: Decimal
    currency: str


class ConnectionEventKind(StrEnum):
    STARTED = "started"
    STOPPED = "stopped"
    CONNECTED = "connected"
    SUBSCRIBED = "subscribed"
    SUBSCRIBE_REJECTED = "subscribe_rejected"
    DISCONNECTED = "disconnected"
    RECONNECT_SCHEDULED = "reconnect_scheduled"


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    """Something that happened to a data connection (or to the recorder itself)."""

    ts_ms: int
    source: str
    kind: ConnectionEventKind
    detail: str = ""
