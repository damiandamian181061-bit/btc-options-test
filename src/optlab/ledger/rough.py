"""ROUGH (Phase 2B): a simple single-entry cash ledger for paper credit spreads.

Every amount is an exact Decimal in USDC (BTC_USDC options). A spread is two legs of the same
size in BTC:

- entry: sell the short leg at its bid, buy the long leg at its ask;
- exit (take profit or stop): buy the short leg back at its ask, sell the long leg at its bid;
- settlement: each leg is closed at its intrinsic value at the delivery price, and each
  in-the-money leg pays the delivery fee once (docs/brief.md, Settlement).

Each step is a ``Fill`` whose ``cash`` is the change in cash, fee included. Cash is the starting
capital plus the sum of all fills; equity adds the open spreads at Deribit's mark price.

Shortcuts (docs/rough-version.md): single-entry, not double-entry; no reconciliation; settlement
pays intrinsic value directly instead of booking the expiry's future. Phase 4 replaces this with
the double-entry ledger and its reconciliation test.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from optlab.simulator.rough_fees import FeeSchedule

Side = Literal["bull put", "bear call"]
OptionType = Literal["call", "put"]
FillKind = Literal["entry", "exit", "settlement"]
Direction = Literal["buy", "sell"]
BOOK: Literal["linear"] = "linear"


def option_type_of(side: Side) -> OptionType:
    return "put" if side == "bull put" else "call"


def intrinsic(option_type: OptionType, strike: Decimal, price: Decimal) -> Decimal:
    """Value per BTC at expiry of an option struck at ``strike`` when the price is ``price``."""
    value = price - strike if option_type == "call" else strike - price
    return max(value, Decimal(0))


@dataclass(frozen=True)
class Fill:
    instrument_name: str
    kind: FillKind
    direction: Direction
    amount: Decimal  # BTC
    price: Decimal  # USDC per BTC (settlement: intrinsic value at the delivery price)
    fee: Decimal  # USDC
    currency: str

    @property
    def cash(self) -> Decimal:
        """Change in cash: the premium received or paid, less the fee."""
        premium = self.amount * self.price
        return (premium if self.direction == "sell" else -premium) - self.fee


@dataclass(frozen=True)
class SpreadLegs:
    side: Side
    expiry_ms: int
    short_name: str
    long_name: str
    short_strike: Decimal
    long_strike: Decimal
    amount: Decimal  # BTC, the same on both legs

    @property
    def option_type(self) -> OptionType:
        return option_type_of(self.side)

    @property
    def width(self) -> Decimal:
        return abs(self.short_strike - self.long_strike)


def cash_of(fills: Iterable[Fill]) -> Decimal:
    return sum((f.cash for f in fills), Decimal(0))


def _trade(
    legs: SpreadLegs,
    name: str,
    kind: FillKind,
    direction: Direction,
    price: Decimal,
    index_price: Decimal,
    fees: FeeSchedule,
    at_ms: int,
) -> Fill:
    fee = fees.trading_fee(BOOK, price, index_price, legs.amount, legs.expiry_ms, at_ms=at_ms)
    currency = fees.trading[BOOK].currency
    return Fill(name, kind, direction, legs.amount, price, fee, currency)


def entry_fills(
    legs: SpreadLegs,
    short_bid: Decimal,
    long_ask: Decimal,
    index_price: Decimal,
    fees: FeeSchedule,
    *,
    at_ms: int,
) -> tuple[Fill, Fill]:
    """Open the spread: sell the short leg at its bid, buy the long leg at its ask (taker)."""
    return (
        _trade(legs, legs.short_name, "entry", "sell", short_bid, index_price, fees, at_ms),
        _trade(legs, legs.long_name, "entry", "buy", long_ask, index_price, fees, at_ms),
    )


def exit_fills(
    legs: SpreadLegs,
    short_ask: Decimal,
    long_bid: Decimal,
    index_price: Decimal,
    fees: FeeSchedule,
    *,
    at_ms: int,
) -> tuple[Fill, Fill]:
    """Close the spread: buy the short leg back at its ask, sell the long leg at its bid."""
    return (
        _trade(legs, legs.short_name, "exit", "buy", short_ask, index_price, fees, at_ms),
        _trade(legs, legs.long_name, "exit", "sell", long_bid, index_price, fees, at_ms),
    )


def settlement_fills(
    legs: SpreadLegs, delivery_price: Decimal, fees: FeeSchedule
) -> tuple[Fill, Fill]:
    """Settle at the delivery price: each leg closes at its intrinsic value.

    An in-the-money leg pays the delivery fee once (when it becomes a position in the expiry's
    future, which then cash-settles at the same price, so the payoff is the same).
    """
    currency = fees.trading[BOOK].currency

    def close(name: str, strike: Decimal, direction: Direction) -> Fill:
        value = intrinsic(legs.option_type, strike, delivery_price)
        fee = fees.delivery_fee(BOOK, value, delivery_price, legs.amount, legs.expiry_ms)
        return Fill(name, "settlement", direction, legs.amount, value, fee, currency)

    return (
        close(legs.short_name, legs.short_strike, "buy"),
        close(legs.long_name, legs.long_strike, "sell"),
    )


def open_value(legs: SpreadLegs, short_mark: Decimal, long_mark: Decimal) -> Decimal:
    """The open spread at mark: what we hold (long leg) less what we owe (short leg)."""
    return legs.amount * (long_mark - short_mark)
