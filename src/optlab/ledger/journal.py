"""A double-entry journal in exact decimals (docs/accounting.md, principle 1).

Every entry is a list of postings in one currency whose amounts sum to exactly zero: a positive
amount is a debit, a negative one a credit. An account's balance is the sum of its postings, so
debit-normal accounts (cash, fees, long positions) have positive balances and credit-normal ones
(capital, gains, short positions) negative ones. Nothing is rounded.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal


class UnbalancedEntryError(ValueError):
    """An entry whose debits and credits differ."""


@dataclass(frozen=True, slots=True)
class Entry:
    seq: int
    ts_ms: int
    kind: str
    currency: str
    postings: tuple[tuple[str, Decimal], ...]  # (account, amount): + debit, - credit
    memo: str = ""
    ref: str = ""


class Journal:
    """Append-only list of balanced entries, with running balances per (account, currency)."""

    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self._balances: dict[tuple[str, str], Decimal] = defaultdict(Decimal)

    def post(
        self,
        ts_ms: int,
        kind: str,
        currency: str,
        postings: Iterable[tuple[str, Decimal]],
        *,
        memo: str = "",
        ref: str = "",
    ) -> Entry:
        lines = tuple((account, Decimal(amount)) for account, amount in postings)
        if len(lines) < 2:
            raise UnbalancedEntryError(f"{kind}: an entry needs at least two postings")
        total = sum((amount for _, amount in lines), Decimal(0))
        if total != 0:
            raise UnbalancedEntryError(f"{kind}: debits and credits differ by {total}")
        entry = Entry(len(self.entries), ts_ms, kind, currency, lines, memo, ref)
        self.entries.append(entry)
        for account, amount in lines:
            self._balances[(account, currency)] += amount
        return entry

    def balance(self, account: str, currency: str) -> Decimal:
        return self._balances.get((account, currency), Decimal(0))

    def balances(self, prefix: str, currency: str) -> dict[str, Decimal]:
        """Every account starting with ``prefix``, with its balance (zero balances included)."""
        return {
            account: amount
            for (account, cur), amount in self._balances.items()
            if cur == currency and account.startswith(prefix)
        }

    def trial_balance(self) -> dict[str, Decimal]:
        """The sum of all balances per currency: always zero for a sound journal."""
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for (_, currency), amount in self._balances.items():
            totals[currency] += amount
        return dict(totals)

    def since(self, seq: int) -> Sequence[Entry]:
        return self.entries[seq:]
