"""The backtest's books: positions and the booking rules of docs/accounting.md.

Accounts (one currency per book; the main book, BTC_USDC, is all USDC):

| account                 | holds                                                         |
| ----------------------- | ------------------------------------------------------------- |
| ``capital``             | paper capital                                                 |
| ``cash:USDC``           | cash                                                          |
| ``position:<option>``   | an open option at its entry value (debit long, credit short)  |
| ``settlement:<future>`` | the future an in-the-money option becomes at expiry, briefly  |
| ``pnl:options``         | realised option P&L (credit = gain)                           |
| ``pnl:hedge``           | realised perpetual P&L (credit = gain)                        |
| ``fees:trading``        | trading fees                                                  |
| ``fees:delivery``       | delivery fees                                                 |
| ``funding``             | funding paid (debit) or received (credit)                     |

Options are booked at their premium: selling one credits its position account with the premium
received, buying debits it with the premium paid. A close releases the share of the entry value
it closes (``entry value x closed / open``) and books the difference from the closing premium to
``pnl:options``.

Perpetuals exchange no premium: a trade only moves cash for its fee. Their entry notional
(``size x price``) is kept with the position, outside the journal, as a futures exchange does;
a close books the realised P&L (closing notional minus the released entry notional) between
cash and ``pnl:hedge``. Unrealised hedge P&L is ``size x mark - entry notional``.

Settlement at Deribit's delivery price P, in two steps (docs/accounting.md):

1. out of the money: the entry value goes to ``pnl:options`` ("expiry");
2. in the money: the option closes at its intrinsic value into ``settlement:<future>``
   ("exercise": the option became a position in its expiry's future at the strike), then the
   future cash-settles at P ("delivery"), leaving the settlement account at zero. One delivery
   fee per in-the-money option ("delivery fee"); the future pays none.

Equity = cash + options at mark + perpetuals' unrealised P&L (+ settlement balances, zero
outside a settlement). ``reconcile`` splits its change into option P&L, hedge P&L, fees and
funding; the identity holds exactly at every step.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from optlab.domain.money import quantize
from optlab.ledger.journal import Journal

Direction = Literal["buy", "sell"]
Kind = Literal["option", "perpetual"]

CAPITAL = "capital"
CASH = "cash:USDC"
PNL_OPTIONS = "pnl:options"
PNL_HEDGE = "pnl:hedge"
FEES_TRADING = "fees:trading"
FEES_DELIVERY = "fees:delivery"
FUNDING = "funding"
ZERO = Decimal(0)


def position_account(instrument: str) -> str:
    return f"position:{instrument}"


def settlement_account(future: str) -> str:
    return f"settlement:{future}"


def intrinsic(option_type: str, strike: Decimal, price: Decimal) -> Decimal:
    """Value per BTC at expiry of an option struck at ``strike`` when the price is ``price``."""
    value = price - strike if option_type == "call" else strike - price
    return max(value, ZERO)


@dataclass
class Position:
    instrument: str
    kind: Kind
    qty: Decimal = ZERO  # BTC; negative when short
    entry_value: Decimal = ZERO  # options: premium (signed); perpetuals: entry notional (signed)


@dataclass(frozen=True)
class Reconciliation:
    """Equity and its parts at one moment, all since the start (USDC)."""

    equity: Decimal
    cash: Decimal
    option_pnl: Decimal  # realised + unrealised
    hedge_pnl: Decimal  # realised + unrealised
    fees: Decimal  # trading + delivery (positive = paid)
    funding: Decimal  # positive = received


@dataclass
class Books:
    capital: Decimal
    currency: str
    start_ms: int
    journal: Journal = field(default_factory=Journal)
    positions: dict[str, Position] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.currency != "USDC":
            raise NotImplementedError("only the BTC_USDC book (USDC) is backtested")
        self.journal.post(
            self.start_ms,
            "capital",
            self.currency,
            [(CASH, self.capital), (CAPITAL, -self.capital)],
        )

    # ------------------------------------------------------------------ balances

    def balance(self, account: str) -> Decimal:
        return self.journal.balance(account, self.currency)

    @property
    def cash(self) -> Decimal:
        return self.balance(CASH)

    def open_positions(self) -> dict[str, Position]:
        return {name: p for name, p in self.positions.items() if p.qty != 0}

    def _position(self, instrument: str, kind: Kind) -> Position:
        pos = self.positions.get(instrument)
        if pos is None:
            pos = self.positions[instrument] = Position(instrument, kind)
        elif pos.kind != kind:
            raise ValueError(f"{instrument} is a {pos.kind}, not a {kind}")
        return pos

    # ------------------------------------------------------------------ trades

    def option_fill(
        self,
        ts_ms: int,
        instrument: str,
        direction: Direction,
        amount: Decimal,
        price: Decimal,
        fee: Decimal,
        *,
        ref: str = "",
    ) -> None:
        """One option fill at ``price`` (premium per BTC) for ``amount`` BTC, and its fee."""
        if amount <= 0 or price < 0 or fee < 0:
            raise ValueError("amount must be positive, price and fee not negative")
        pos = self._position(instrument, "option")
        sign = Decimal(1) if direction == "buy" else Decimal(-1)
        remaining = amount
        account = position_account(instrument)
        if pos.qty != 0 and (pos.qty > 0) != (sign > 0):  # reduces the open position first
            closing = min(remaining, abs(pos.qty))
            released = self._release(pos, closing)
            premium = price * closing
            if pos.qty + sign * closing == 0:
                released = pos.entry_value  # the last of it: exactly what is left
            if sign < 0:  # selling out a long: Dr cash, Cr position, gain to pnl
                lines = [(CASH, premium), (account, -released), (PNL_OPTIONS, released - premium)]
            else:  # buying back a short: Dr position, Cr cash, loss to pnl
                lines = [(account, -released), (CASH, -premium), (PNL_OPTIONS, premium + released)]
            self.journal.post(ts_ms, "close", self.currency, lines, memo=instrument, ref=ref)
            pos.qty += sign * closing
            pos.entry_value -= released
            remaining -= closing
        if remaining > 0:
            premium = price * remaining
            lines = [(account, sign * premium), (CASH, -sign * premium)]
            self.journal.post(ts_ms, "open", self.currency, lines, memo=instrument, ref=ref)
            pos.qty += sign * remaining
            pos.entry_value += sign * premium
        self._fee(ts_ms, FEES_TRADING, fee, instrument, ref)

    def perpetual_fill(
        self,
        ts_ms: int,
        instrument: str,
        direction: Direction,
        amount: Decimal,
        price: Decimal,
        fee: Decimal,
        *,
        ref: str = "",
    ) -> None:
        """One perpetual fill: no premium; a close realises P&L into ``pnl:hedge``."""
        if amount <= 0 or price <= 0 or fee < 0:
            raise ValueError("amount and price must be positive, fee not negative")
        pos = self._position(instrument, "perpetual")
        sign = Decimal(1) if direction == "buy" else Decimal(-1)
        remaining = amount
        if pos.qty != 0 and (pos.qty > 0) != (sign > 0):
            closing = min(remaining, abs(pos.qty))
            released = self._release(pos, closing)
            if pos.qty + sign * closing == 0:
                released = pos.entry_value
            # Realised = closing notional - released entry notional, for a long (sign < 0);
            # released entry notional - closing notional for a short.
            realised = -sign * (price * closing) - released
            if realised != 0:
                lines = [(CASH, realised), (PNL_HEDGE, -realised)]
                self.journal.post(ts_ms, "hedge close", self.currency, lines, memo=instrument)
            pos.qty += sign * closing
            pos.entry_value -= released
            remaining -= closing
        if remaining > 0:
            pos.qty += sign * remaining
            pos.entry_value += sign * price * remaining
        self._fee(ts_ms, FEES_TRADING, fee, instrument, ref)

    def funding(self, ts_ms: int, instrument: str, paid: Decimal) -> None:
        """Funding on a perpetual: ``paid`` > 0 is paid by us, < 0 received."""
        if paid != 0:
            lines = [(FUNDING, paid), (CASH, -paid)]
            self.journal.post(ts_ms, "funding", self.currency, lines, memo=instrument)

    def settle_option(
        self,
        ts_ms: int,
        instrument: str,
        option_type: str,
        strike: Decimal,
        delivery_price: Decimal,
        future: str,
        delivery_fee: Decimal,
        *,
        ref: str = "",
    ) -> Decimal:
        """Settle an expired option at the delivery price; returns its intrinsic value a BTC."""
        pos = self.positions.get(instrument)
        if pos is None or pos.qty == 0:
            return ZERO
        account = position_account(instrument)
        value = intrinsic(option_type, strike, delivery_price)
        if value == 0:
            if delivery_fee != 0:
                raise ValueError("an option that expires worthless pays no delivery fee")
            lines = [(PNL_OPTIONS, pos.entry_value), (account, -pos.entry_value)]
            self.journal.post(ts_ms, "expiry", self.currency, lines, memo=instrument, ref=ref)
        else:
            amount = value * pos.qty  # signed: + we receive, - we pay
            sett = settlement_account(future)
            # Step 1: the option becomes a position in the expiry's future at the strike.
            long_future = (option_type == "call") == (pos.qty > 0)
            memo = (
                f"{instrument} -> {'long' if long_future else 'short'} {abs(pos.qty)} {future} "
                f"at {strike}, settled at {delivery_price}"
            )
            lines = [(sett, amount), (account, -pos.entry_value)]
            lines.append((PNL_OPTIONS, pos.entry_value - amount))
            self.journal.post(ts_ms, "exercise", self.currency, lines, memo=memo, ref=ref)
            # Step 2: the future cash-settles at the delivery price.
            lines = [(CASH, amount), (sett, -amount)]
            self.journal.post(ts_ms, "delivery", self.currency, lines, memo=memo, ref=ref)
            self._fee(ts_ms, FEES_DELIVERY, delivery_fee, instrument, ref, kind="delivery fee")
        pos.qty = ZERO
        pos.entry_value = ZERO
        return value

    # ------------------------------------------------------------------ valuation

    def option_value(self, marks: Mapping[str, Decimal]) -> Decimal:
        """Open options at mark: long positive, short negative."""
        return sum(
            (p.qty * marks[n] for n, p in self.open_positions().items() if p.kind == "option"),
            ZERO,
        )

    def option_unrealised(self, marks: Mapping[str, Decimal]) -> Decimal:
        opts = [(n, p) for n, p in self.open_positions().items() if p.kind == "option"]
        return sum((p.qty * marks[n] - p.entry_value for n, p in opts), ZERO)

    def hedge_unrealised(self, marks: Mapping[str, Decimal]) -> Decimal:
        perps = [(n, p) for n, p in self.open_positions().items() if p.kind == "perpetual"]
        return sum((p.qty * marks[n] - p.entry_value for n, p in perps), ZERO)

    def settlement_balance(self) -> Decimal:
        return sum(self.journal.balances("settlement:", self.currency).values(), ZERO)

    def equity(self, marks: Mapping[str, Decimal]) -> Decimal:
        """Cash plus open options at mark plus the perpetuals' unrealised P&L."""
        return (
            self.cash
            + self.option_value(marks)
            + self.hedge_unrealised(marks)
            + self.settlement_balance()
        )

    def reconcile(self, marks: Mapping[str, Decimal]) -> Reconciliation:
        """Equity, and the P&L parts that must explain it (checked by the engine every step)."""
        return Reconciliation(
            equity=self.equity(marks),
            cash=self.cash,
            option_pnl=-self.balance(PNL_OPTIONS) + self.option_unrealised(marks),
            hedge_pnl=-self.balance(PNL_HEDGE) + self.hedge_unrealised(marks),
            fees=self.balance(FEES_TRADING) + self.balance(FEES_DELIVERY),
            funding=-self.balance(FUNDING),
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _release(pos: Position, closing: Decimal) -> Decimal:
        """The share of the entry value that ``closing`` BTC of the position carries.

        A division, so rounded to 1e-12 USDC (``optlab.domain.money.quantize``); the rounding
        stays in the booked amounts, and the last close releases exactly what is left.
        """
        return quantize(pos.entry_value * closing / abs(pos.qty))

    def _fee(
        self, ts_ms: int, account: str, fee: Decimal, instrument: str, ref: str, kind: str = "fee"
    ) -> None:
        if fee != 0:
            lines = [(account, fee), (CASH, -fee)]
            self.journal.post(ts_ms, kind, self.currency, lines, memo=instrument, ref=ref)
