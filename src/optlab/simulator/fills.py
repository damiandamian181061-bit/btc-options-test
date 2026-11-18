"""How a ticket fills against one snapshot's top of book (docs/backtest.md).

- Every leg crosses the spread at top of book: a buy fills at the ask, a sell at the bid, each
  at its own price (spreads fill leg by leg, no combo).
- At most the quoted size fills: a ticket larger than the size shown at the top of the book
  fills in part and the rest is dropped (the snapshot has no deeper levels). A strategy that
  wants the rest sends it again at a later minute.
- The legs of a multi-leg ticket have the same size and fill the same amount, the smallest any
  leg can take, so a spread is never left with one leg.
- Amounts are floored to the instrument's minimum trade amount (0.01 BTC for BTC_USDC options).
- A leg with no price on its side, or no size, leaves the whole ticket unfilled.
- No latency, no slippage beyond the spread, no market impact.

``fill_ticket`` only works out what would fill; the engine books it after the risk gateway
approves exactly that fill.
"""

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from optlab.domain.checks import need
from optlab.domain.orders import Direction, Order, Ticket
from optlab.simulator.fees import FeeSchedule
from optlab.simulator.market import InstrumentInfo, MarketState

ZERO = Decimal(0)


@dataclass(frozen=True)
class LegFill:
    instrument: str
    kind: str  # option or perpetual
    direction: Direction
    amount: Decimal
    price: Decimal
    fee: Decimal
    index_price: Decimal
    mid: Decimal | None
    mark: Decimal | None

    @property
    def signed(self) -> Decimal:
        return self.amount if self.direction == "buy" else -self.amount


@dataclass(frozen=True)
class TicketFill:
    requested: Decimal
    filled: Decimal
    legs: tuple[LegFill, ...] = field(default_factory=tuple)
    reason: str = ""  # why it filled in part or not at all


def floor_to(amount: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return amount
    return (amount / step).to_integral_value(rounding=ROUND_FLOOR) * step


def fill_ticket(ticket: Ticket, state: MarketState, fees: FeeSchedule) -> TicketFill:
    """What the ticket would fill at this minute (see the module notes)."""
    if state.index is None:
        return TicketFill(ticket.amount, ZERO, reason="no btc_usdc index at this minute")
    available = ticket.amount
    step = ZERO
    sides: list[tuple[Order, InstrumentInfo, Decimal]] = []
    for order in ticket.orders:
        info = state.instruments.get(order.instrument)
        quote = state.quote(order.instrument)
        if info is None or quote is None:
            return TicketFill(ticket.amount, ZERO, reason=f"no quote for {order.instrument}")
        if info.kind not in ("option", "perpetual"):
            return TicketFill(ticket.amount, ZERO, reason=f"{info.kind}s are not traded")
        if order.direction == "buy":
            price, size, side = quote.ask, quote.ask_size, "ask"
        else:
            price, size, side = quote.bid, quote.bid_size, "bid"
        if price is None or size <= 0:
            return TicketFill(ticket.amount, ZERO, reason=f"no {side} for {order.instrument}")
        available = min(available, size)
        step = max(step, info.min_trade_amount)
        sides.append((order, info, price))
    filled = floor_to(available, step)
    if filled <= 0:
        return TicketFill(ticket.amount, ZERO, reason="quoted size below the minimum amount")
    legs = []
    for order, info, price in sides:
        quote = need(state.quote(order.instrument), f"{order.instrument} quote")
        if info.kind == "option":
            expiry = need(info.expiry_ms, f"{order.instrument} expiry")
            fee = fees.option_fee(price, state.index, filled, expiry, at_ms=state.ts_ms)
        else:
            fee = fees.perpetual_fee(price, filled, at_ms=state.ts_ms)
        legs.append(
            LegFill(
                order.instrument, info.kind, order.direction, filled, price, fee, state.index,
                quote.mid, quote.mark,
            )
        )  # fmt: skip
    reason = "" if filled == ticket.amount else f"filled {filled} of {ticket.amount}: quoted size"
    return TicketFill(ticket.amount, filled, tuple(legs), reason)
