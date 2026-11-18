"""Double-entry ledger and the booking rules of docs/accounting.md (signed off 18 Nov 2026).

The worked example of docs/accounting.md is reproduced line by line: a bull put spread, its mark
a day later, and the three settlement outcomes, including the two-step booking through the
expiry's future. Every amount is exact.
"""

import random
from decimal import Decimal

import pytest

from optlab.ledger.books import (
    CASH,
    FEES_DELIVERY,
    FEES_TRADING,
    FUNDING,
    PNL_HEDGE,
    PNL_OPTIONS,
    Books,
    Direction,
    intrinsic,
    position_account,
    settlement_account,
)
from optlab.ledger.journal import Journal, UnbalancedEntryError

D = Decimal
P60 = "BTC_USDC-27NOV26-60000-P"
P58 = "BTC_USDC-27NOV26-58000-P"
FUT = "BTC_USDC-27NOV26"
PERP = "BTC_USDC-PERPETUAL"
T0 = 1_795_000_000_000


def test_an_entry_must_balance() -> None:
    journal = Journal()
    with pytest.raises(UnbalancedEntryError):
        journal.post(T0, "test", "USDC", [("cash:USDC", D("1")), ("capital", D("-0.99"))])
    journal.post(T0, "test", "USDC", [("cash:USDC", D("1")), ("capital", D("-1"))])
    assert journal.trial_balance() == {"USDC": D(0)}


def bull_put(books: Books) -> None:
    """docs/accounting.md: sell 0.5 60,000 P at 800, buy 0.5 58,000 P at 450, fee 9.75 each."""
    books.option_fill(T0, P60, "sell", D("0.5"), D("800"), D("9.75"))
    books.option_fill(T0, P58, "buy", D("0.5"), D("450"), D("9.75"))


def test_worked_example_entry_and_mark() -> None:
    books = Books(D("100000"), "USDC", T0)
    bull_put(books)
    assert books.cash == D("100155.50")
    assert books.balance(position_account(P60)) == D("-400.0")
    assert books.balance(position_account(P58)) == D("225.0")
    assert books.balance(FEES_TRADING) == D("19.50")
    marks = {P60: D("950"), P58: D("520")}
    assert books.option_value(marks) == D("-215.0")
    assert books.option_unrealised(marks) == D("-40.0")
    assert books.equity(marks) == D("99940.50")


def settle(books: Books, delivery: str) -> None:
    p = D(delivery)
    for name, strike in ((P60, D("60000")), (P58, D("58000"))):
        value = intrinsic("put", strike, p)
        fee = min(D("0.00015") * p, D("0.125") * value) * D("0.5") if value > 0 else D(0)
        books.settle_option(T0 + 1, name, "put", strike, p, FUT, fee)


@pytest.mark.parametrize(
    ("delivery", "pnl", "delivery_fees"),
    [("66000", "155.50", "0"), ("59000", "-348.925", "4.425"), ("57000", "-853.05", "8.55")],
)
def test_worked_example_settlement(delivery: str, pnl: str, delivery_fees: str) -> None:
    books = Books(D("100000"), "USDC", T0)
    bull_put(books)
    settle(books, delivery)
    assert books.equity({}) - D("100000") == D(pnl)
    assert books.balance(FEES_DELIVERY) == D(delivery_fees)
    assert books.open_positions() == {}
    assert books.balance(settlement_account(FUT)) == 0
    assert books.journal.trial_balance() == {"USDC": D(0)}


def test_the_59000_case_journal_by_journal() -> None:
    books = Books(D("100000"), "USDC", T0)
    bull_put(books)
    n = len(books.journal.entries)
    settle(books, "59000")
    lines = [(e.kind, sorted(e.postings)) for e in books.journal.entries[n:]]
    assert lines == [
        # 60,000 P closes at intrinsic 500 into the future: Dr position 400, Dr pnl 100,
        # Cr settlement 500.
        (
            "exercise",
            sorted(
                [
                    (position_account(P60), D("400.0")),
                    (PNL_OPTIONS, D("100.0")),
                    (settlement_account(FUT), D("-500.0")),
                ]
            ),
        ),
        # The future (long 0.5 at 60,000) settles at 59,000: Dr settlement 500, Cr cash 500.
        ("delivery", sorted([(settlement_account(FUT), D("500.0")), (CASH, D("-500.0"))])),
        ("delivery fee", sorted([(FEES_DELIVERY, D("4.425")), (CASH, D("-4.425"))])),
        # 58,000 P expires worthless: Dr pnl 225, Cr position 225.
        ("expiry", sorted([(PNL_OPTIONS, D("225.0")), (position_account(P58), D("-225.0"))])),
    ]
    assert -books.balance(PNL_OPTIONS) == D("-325.0")


@pytest.mark.parametrize("option_type", ["call", "put"])
@pytest.mark.parametrize("qty", ["1.3", "-0.7"])
@pytest.mark.parametrize("delivery", ["50000", "60000", "70000"])
def test_two_step_settlement_pays_exactly_intrinsic_value(
    option_type: str, qty: str, delivery: str
) -> None:
    """The payoff through the expiry's future equals q x intrinsic value at the delivery price."""
    books = Books(D("100000"), "USDC", T0)
    name = f"BTC_USDC-27NOV26-60000-{option_type[0].upper()}"
    q = D(qty)
    books.option_fill(T0, name, "buy" if q > 0 else "sell", abs(q), D("1234.5"), D(0))
    cash_before = books.cash
    p = D(delivery)
    books.settle_option(T0 + 1, name, option_type, D("60000"), p, FUT, D(0))
    assert books.cash - cash_before == q * intrinsic(option_type, D("60000"), p)
    assert books.balance(settlement_account(FUT)) == 0
    assert books.open_positions() == {}


def test_partial_buybacks_release_the_entry_value_in_proportion() -> None:
    books = Books(D("100000"), "USDC", T0)
    name = "BTC_USDC-27NOV26-70000-C"
    books.option_fill(T0, name, "sell", D("1"), D("1000"), D(0))
    books.option_fill(T0 + 1, name, "buy", D("0.4"), D("600"), D(0))  # +160
    assert -books.balance(PNL_OPTIONS) == D("160")
    books.option_fill(T0 + 2, name, "buy", D("0.6"), D("1200"), D(0))  # -120
    assert -books.balance(PNL_OPTIONS) == D("40")
    assert books.open_positions() == {}
    assert books.balance(position_account(name)) == 0


def test_a_fill_through_zero_closes_then_opens() -> None:
    books = Books(D("100000"), "USDC", T0)
    name = "BTC_USDC-27NOV26-70000-C"
    books.option_fill(T0, name, "buy", D("0.5"), D("1000"), D(0))
    books.option_fill(T0 + 1, name, "sell", D("1.5"), D("1100"), D(0))
    assert -books.balance(PNL_OPTIONS) == D("50")  # 0.5 x (1100 - 1000)
    pos = books.open_positions()[name]
    assert pos.qty == D("-1.0")
    assert pos.entry_value == D("-1100.0")


def test_perpetual_hedge_pnl_and_funding() -> None:
    books = Books(D("100000"), "USDC", T0)
    books.perpetual_fill(T0, PERP, "buy", D("0.2"), D("65000"), D("4.55"))
    assert books.cash == D("100000") - D("4.55")  # no premium changes hands
    assert books.hedge_unrealised({PERP: D("65500")}) == D("100.0")
    assert books.equity({PERP: D("65500")}) == D("100095.45")
    books.funding(T0 + 1, PERP, D("1.30"))  # the long pays 1.30
    assert books.balance(FUNDING) == D("1.30")
    books.perpetual_fill(T0 + 2, PERP, "sell", D("0.2"), D("66000"), D("4.62"))
    assert -books.balance(PNL_HEDGE) == D("200.0")
    assert books.equity({}) == D("100000") - D("4.55") - D("1.30") + D("200") - D("4.62")


def test_reconciliation_holds_at_every_step_of_a_random_history() -> None:
    """Change in equity = option P&L + hedge P&L - fees + funding, to the last decimal."""
    rng = random.Random(20261118)
    books = Books(D("100000"), "USDC", T0)
    names = [f"BTC_USDC-27NOV26-{k}-{t}" for k in (60000, 62000, 64000) for t in ("C", "P")]
    marks: dict[str, Decimal] = {n: D(rng.randint(100, 3000)) for n in names}
    marks[PERP] = D("65000")
    prev = books.reconcile(marks)
    for step in range(400):
        ts = T0 + step
        roll = rng.random()
        amount = D(rng.randint(1, 150)) / D(100)
        side: Direction = "buy" if rng.random() < 0.5 else "sell"
        if roll < 0.45:
            n = rng.choice(names)
            price = D(rng.randint(50, 4000)) + D(rng.randint(0, 99)) / D(100)
            books.option_fill(ts, n, side, amount, price, D(rng.randint(0, 2000)) / D(100))
        elif roll < 0.6:
            price = D(rng.randint(60000, 70000)) + D("0.5")
            books.perpetual_fill(ts, PERP, side, amount, price, D(rng.randint(0, 900)) / D(100))
        elif roll < 0.7:
            books.funding(ts, PERP, D(rng.randint(-500, 500)) / D(1000))
        for n in marks:
            marks[n] = max(D(0), marks[n] + D(rng.randint(-80, 80)))
        now = books.reconcile(marks)
        assert now.equity - prev.equity == (
            (now.option_pnl - prev.option_pnl)
            + (now.hedge_pnl - prev.hedge_pnl)
            - (now.fees - prev.fees)
            + (now.funding - prev.funding)
        )
        assert now.equity - D("100000") == now.option_pnl + now.hedge_pnl - now.fees + now.funding
        assert books.journal.trial_balance() == {"USDC": D(0)}
        prev = now
