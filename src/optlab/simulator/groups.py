"""Position groups: the strategy's trades (a spread, a straddle, a hedge), on top of the books.

The books keep positions per instrument, as the exchange does. A group keeps what one trade
holds, so the gateway can count open spreads and their maximum losses, and reports can show each
trade with its thesis, entry, exit and P&L. Entry values follow the books' convention: an
option's premium (long +, short -), a perpetual's notional.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from optlab.domain.money import quantize

ZERO = Decimal(0)


@dataclass
class Group:
    group_id: str
    opened_ms: int
    thesis: str = ""
    reason: str = ""
    legs: dict[str, Decimal] = field(default_factory=dict)  # signed BTC
    entry: dict[str, Decimal] = field(default_factory=dict)  # signed entry value
    fees: Decimal = ZERO  # trading and delivery fees paid by this group
    realised: Decimal = ZERO  # realised P&L before fees
    closed_ms: int | None = None
    exit_reason: str = ""

    @property
    def is_open(self) -> bool:
        return any(q != 0 for q in self.legs.values())

    def open_legs(self) -> dict[str, Decimal]:
        return {n: q for n, q in self.legs.items() if q != 0}

    def apply(self, instrument: str, signed_qty: Decimal, price: Decimal) -> None:
        """A fill of ``signed_qty`` (buy +) at ``price``.

        Options and perpetuals realise the same way: the closing value less the released share
        of the entry value (premium for options, notional for perpetuals).
        """
        qty = self.legs.get(instrument, ZERO)
        entry = self.entry.get(instrument, ZERO)
        remaining = signed_qty
        if qty != 0 and (qty > 0) != (signed_qty > 0):
            closing = min(abs(signed_qty), abs(qty))
            released = entry if closing == abs(qty) else quantize(entry * closing / abs(qty))
            sign = Decimal(1) if signed_qty > 0 else Decimal(-1)
            # Closing value received (selling out a long) or paid (buying back a short).
            self.realised += -sign * price * closing - released
            qty += sign * closing
            entry -= released
            remaining = signed_qty - sign * closing
        if remaining != 0:
            qty += remaining
            entry += remaining * price
        self.legs[instrument] = qty
        self.entry[instrument] = entry

    def settle(self, instrument: str, value: Decimal) -> None:
        """An option leg settles at its intrinsic value a BTC."""
        qty = self.legs.get(instrument, ZERO)
        if qty == 0:
            return
        self.realised += qty * value - self.entry.get(instrument, ZERO)
        self.legs[instrument] = ZERO
        self.entry[instrument] = ZERO
