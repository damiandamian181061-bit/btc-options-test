"""Tickets: what a strategy asks for. Strategies create them and nothing else.

A ``Ticket`` is one or more legs sent together: a spread is a two-leg ticket. The engine passes
each ticket through the risk gateway before anything is booked (CLAUDE.md: strategies never
touch execution; only the risk gateway passes orders on).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Direction = Literal["buy", "sell"]
Intent = Literal["open", "close"]


@dataclass(frozen=True)
class Order:
    instrument: str
    direction: Direction
    amount: Decimal  # BTC


@dataclass(frozen=True)
class Ticket:
    """Legs sent together. ``open`` starts a new position group; ``close`` reduces ``group_id``."""

    orders: tuple[Order, ...]
    intent: Intent
    group_id: str | None = None  # required for close
    reason: str = ""  # why the strategy sent it (the exit reason for a close)
    thesis: str = ""  # for an open: what has to happen for the trade to work

    def __post_init__(self) -> None:
        if not self.orders:
            raise ValueError("a ticket needs at least one order")
        if any(o.amount <= 0 for o in self.orders):
            raise ValueError("order amounts must be positive")
        if len({o.amount for o in self.orders}) > 1:
            raise ValueError("the legs of a ticket must have the same amount")
        if len({o.instrument for o in self.orders}) != len(self.orders):
            raise ValueError("an instrument appears twice in one ticket")
        if self.intent == "close" and self.group_id is None:
            raise ValueError("a close ticket names the group it closes")

    @property
    def amount(self) -> Decimal:
        return self.orders[0].amount


def signed_legs(orders: Sequence[Order]) -> dict[str, Decimal]:
    """Signed BTC per instrument (buy +, sell -)."""
    return {o.instrument: o.amount if o.direction == "buy" else -o.amount for o in orders}
