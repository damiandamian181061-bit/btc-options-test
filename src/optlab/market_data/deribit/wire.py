"""Deribit's JSON shapes (API v2, JSON-RPC 2.0 over WebSocket), decoded with msgspec.

These structs are private to the exchange adapter: nothing outside ``optlab.market_data.deribit``
imports them. Unknown fields are ignored, so new fields from Deribit don't break decoding;
missing optional fields decode as None. Formats were checked against docs.deribit.com and live
public messages on 12 November 2026.
"""

import msgspec


class RpcErrorWire(msgspec.Struct):
    code: int
    message: str


class Envelope(msgspec.Struct):
    """Any message from the server: a response (``id``) or a notification (``method``).

    ``params`` and ``result`` stay undecoded until we know their type. msgspec cannot decode
    ``Raw | None``, so a missing field is an empty ``Raw`` (falsy) instead of None.
    """

    id: int | None = None
    method: str | None = None
    params: msgspec.Raw = msgspec.Raw()
    result: msgspec.Raw = msgspec.Raw()
    error: RpcErrorWire | None = None


class SubscriptionParams(msgspec.Struct):
    channel: str
    data: msgspec.Raw


class HeartbeatParams(msgspec.Struct):
    type: str


class GreeksWire(msgspec.Struct):
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float


class TickerWire(msgspec.Struct):
    """``ticker.{instrument_name}.{interval}``. IVs in percent; an empty side has price 0."""

    instrument_name: str
    timestamp: int
    state: str
    best_bid_price: float | None = None
    best_bid_amount: float | None = None
    best_ask_price: float | None = None
    best_ask_amount: float | None = None
    mark_price: float | None = None
    last_price: float | None = None
    index_price: float | None = None
    open_interest: float | None = None
    estimated_delivery_price: float | None = None
    mark_iv: float | None = None
    bid_iv: float | None = None
    ask_iv: float | None = None
    greeks: GreeksWire | None = None
    underlying_price: float | None = None
    underlying_index: str | None = None
    interest_rate: float | None = None
    current_funding: float | None = None
    funding_8h: float | None = None
    interest_value: float | None = None


class TradeWire(msgspec.Struct):
    """One element of ``trades.{kind}.{currency}.{interval}``. IV in percent."""

    trade_id: str
    instrument_name: str
    timestamp: int
    price: float
    amount: float
    direction: str
    trade_seq: int | None = None
    iv: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    tick_direction: int | None = None
    block_trade_id: str | None = None
    liquidation: str | None = None
    combo_id: str | None = None


class PriceIndexWire(msgspec.Struct):
    """``deribit_price_index.{index_name}``."""

    index_name: str
    price: float
    timestamp: int


class InstrumentWire(msgspec.Struct):
    """One element of the ``public/get_instruments`` result."""

    instrument_name: str
    instrument_id: int
    kind: str
    base_currency: str
    quote_currency: str
    price_index: str
    contract_size: float
    tick_size: float
    min_trade_amount: float
    expiration_timestamp: int
    creation_timestamp: int
    is_active: bool
    instrument_type: str | None = None  # "linear" or "reversed"
    settlement_currency: str | None = None
    counter_currency: str | None = None
    settlement_period: str | None = None
    strike: float | None = None
    option_type: str | None = None


class DeliveryPriceWire(msgspec.Struct):
    date: str
    delivery_price: float


class DeliveryPricesWire(msgspec.Struct):
    """The ``public/get_delivery_prices`` result."""

    data: list[DeliveryPriceWire]
    records_total: int


ENVELOPE = msgspec.json.Decoder(Envelope)
SUBSCRIPTION_PARAMS = msgspec.json.Decoder(SubscriptionParams)
HEARTBEAT_PARAMS = msgspec.json.Decoder(HeartbeatParams)
TICKER = msgspec.json.Decoder(TickerWire)
TRADES = msgspec.json.Decoder(list[TradeWire])
PRICE_INDEX = msgspec.json.Decoder(PriceIndexWire)
INSTRUMENTS = msgspec.json.Decoder(list[InstrumentWire])
DELIVERY_PRICES = msgspec.json.Decoder(DeliveryPricesWire)
CHANNEL_LIST = msgspec.json.Decoder(list[str])
