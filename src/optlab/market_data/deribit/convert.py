"""Turn Deribit wire structs into the platform's typed records.

This is where Deribit's conventions become ours:

- IVs arrive in percent (``mark_iv: 37.55``) and leave as decimals (0.3755).
- An empty side of the book arrives as price 0 (and amount 0) and leaves as ``None`` with size 0.
- Strikes, expiries and contract sizes come from instrument metadata, never from the name.
- Deribit's ``instrument_type`` "reversed" is our inverse book.
"""

from datetime import date
from decimal import Decimal

from optlab.domain.market import (
    Book,
    DeliveryPrice,
    Direction,
    Greeks,
    IndexPrice,
    Instrument,
    InstrumentKind,
    OptionType,
    Ticker,
    Trade,
)
from optlab.market_data.deribit.wire import (
    DeliveryPricesWire,
    InstrumentWire,
    PriceIndexWire,
    TickerWire,
    TradeWire,
)

_BOOKS = {"linear": Book.LINEAR, "reversed": Book.INVERSE}
_OPTION_TYPES = {"call": OptionType.CALL, "put": OptionType.PUT}
_DIRECTIONS = {"buy": Direction.BUY, "sell": Direction.SELL}


def iv_decimal(percent: float | None) -> float | None:
    """Deribit IV in percent to a decimal; missing or non-positive IVs become None.

    Deribit quotes IVs to 0.01 percentage points, so rounding the decimal to 10 places loses
    nothing and removes float noise (37.07 -> 0.3707, not 0.37070000000000003).
    """
    if percent is None or percent <= 0:
        return None
    return round(percent / 100.0, 10)


def _side(price: float | None, amount: float | None) -> tuple[float | None, float]:
    """A book side as (price or None, size). Deribit marks an empty side with price 0."""
    if price is None or amount is None or price <= 0 or amount <= 0:
        return None, 0.0
    return price, amount


def instrument_from_wire(w: InstrumentWire) -> Instrument | None:
    """The instrument, or None if it is a kind the platform does not record (combos, spot)."""
    book = _BOOKS.get(w.instrument_type or "")
    if book is None or w.settlement_currency is None or w.counter_currency is None:
        return None
    settlement_period = w.settlement_period or ""
    if w.kind == "option":
        option_type = _OPTION_TYPES.get(w.option_type or "")
        if option_type is None or w.strike is None:
            return None
        kind = InstrumentKind.OPTION
    elif w.kind == "future":
        option_type = None
        kind = (
            InstrumentKind.PERPETUAL if settlement_period == "perpetual" else InstrumentKind.FUTURE
        )
    else:
        return None
    return Instrument(
        name=w.instrument_name,
        exchange_id=w.instrument_id,
        kind=kind,
        book=book,
        base_currency=w.base_currency,
        quote_currency=w.quote_currency,
        settlement_currency=w.settlement_currency,
        counter_currency=w.counter_currency,
        price_index=w.price_index,
        contract_size=w.contract_size,
        tick_size=w.tick_size,
        min_trade_amount=w.min_trade_amount,
        creation_ms=w.creation_timestamp,
        settlement_period=settlement_period,
        is_active=w.is_active,
        expiry_ms=None if kind is InstrumentKind.PERPETUAL else w.expiration_timestamp,
        strike=w.strike if kind is InstrumentKind.OPTION else None,
        option_type=option_type,
    )


def ticker_from_wire(w: TickerWire, recv_ts_ms: int) -> Ticker:
    bid, bid_size = _side(w.best_bid_price, w.best_bid_amount)
    ask, ask_size = _side(w.best_ask_price, w.best_ask_amount)
    g = w.greeks
    return Ticker(
        instrument_name=w.instrument_name,
        exchange_ts_ms=w.timestamp,
        recv_ts_ms=recv_ts_ms,
        state=w.state,
        bid=bid,
        bid_size=bid_size,
        ask=ask,
        ask_size=ask_size,
        mark_price=w.mark_price,
        last_price=w.last_price,
        index_price=w.index_price,
        open_interest=w.open_interest,
        estimated_delivery_price=w.estimated_delivery_price,
        mark_iv=iv_decimal(w.mark_iv),
        bid_iv=iv_decimal(w.bid_iv) if bid is not None else None,
        ask_iv=iv_decimal(w.ask_iv) if ask is not None else None,
        greeks=None if g is None else Greeks(g.delta, g.gamma, g.vega, g.theta, g.rho),
        underlying_price=w.underlying_price,
        underlying_index=w.underlying_index,
        interest_rate=w.interest_rate,
        current_funding=w.current_funding,
        funding_8h=w.funding_8h,
        interest_value=w.interest_value,
    )


def trade_from_wire(w: TradeWire, recv_ts_ms: int) -> Trade | None:
    direction = _DIRECTIONS.get(w.direction)
    if direction is None:
        return None
    return Trade(
        trade_id=w.trade_id,
        instrument_name=w.instrument_name,
        exchange_ts_ms=w.timestamp,
        recv_ts_ms=recv_ts_ms,
        price=w.price,
        amount=w.amount,
        direction=direction,
        trade_seq=w.trade_seq,
        iv=iv_decimal(w.iv),
        mark_price=w.mark_price,
        index_price=w.index_price,
        tick_direction=w.tick_direction,
        block_trade_id=w.block_trade_id,
        liquidation=w.liquidation,
        combo_id=w.combo_id,
    )


def index_price_from_wire(w: PriceIndexWire, recv_ts_ms: int) -> IndexPrice:
    return IndexPrice(
        index_name=w.index_name, price=w.price, exchange_ts_ms=w.timestamp, recv_ts_ms=recv_ts_ms
    )


def delivery_prices_from_wire(
    w: DeliveryPricesWire, index_name: str, currency: str
) -> list[DeliveryPrice]:
    """Delivery prices as exact decimals: ``Decimal(repr(float))`` keeps Deribit's digits."""
    return [
        DeliveryPrice(
            index_name=index_name,
            delivery_date=date.fromisoformat(d.date),
            price=Decimal(repr(d.delivery_price)),
            currency=currency,
        )
        for d in w.data
    ]
