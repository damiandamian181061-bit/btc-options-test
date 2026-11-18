"""A forward per expiry: the matching dated future's mark price, else put-call parity.

Deribit lists a dated future for every BTC_USDC option expiry, and the recorder stores each one
every minute, so for the main book the forward is that future's mark price. Put-call parity on
near-the-money pairs is the fallback (an expiry with no future, or a future with no mark) and
the cross-check: ``difference_bp`` reports how far the two differ.

Parity on the forward with no discounting (Deribit's options carry an interest rate of 0):

- linear (USDC premiums): C - P = F - K, so F = K + (C - P);
- inverse (BTC premiums, each the USD price divided by F): C - P = 1 - K/F, so
  F = K / (1 - (C - P)).

Each pair uses the mid of the best bid and ask of both options. Only pairs where both options
are two-sided, not crossed and no wider than ``max_spread`` of their mid count; the ``pairs``
strikes nearest ``near`` (the future, else the index) give one forward each, and the median is
taken, so one bad quote doesn't move it.
"""

import math
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import pandas as pd

from optlab.domain.market import Book


class ForwardSource(StrEnum):
    FUTURE = "future"
    PARITY = "parity"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Forward:
    expiry_ms: int
    forward: float | None
    source: ForwardSource
    future: float | None  # the dated future's mark price, if listed and marked
    future_name: str | None
    parity: float | None  # the put-call parity forward, if any pair was usable
    parity_pairs: int

    @property
    def difference_bp(self) -> float | None:
        """(future / parity - 1) in basis points, when both exist."""
        if self.future is None or self.parity is None:
            return None
        return (self.future / self.parity - 1.0) * 1e4


def _usable(bid: pd.Series, ask: pd.Series, max_spread: float) -> pd.Series:
    mid = (bid + ask) / 2
    return bid.notna() & ask.notna() & (bid > 0) & (ask >= bid) & ((ask - bid) <= max_spread * mid)


def parity_forward(
    chain: pd.DataFrame,
    book: Book,
    *,
    near: float,
    pairs: int = 3,
    max_spread: float = 0.2,
) -> tuple[float | None, int]:
    """Forward of one expiry's chain from put-call parity, and the number of pairs used."""
    cols = ["strike", "bid", "ask"]
    calls = chain.loc[chain["call"].astype(bool), cols].set_index("strike")
    puts = chain.loc[~chain["call"].astype(bool), cols].set_index("strike")
    both = calls.merge(
        puts, left_index=True, right_index=True, how="inner", suffixes=("_c", "_p")
    ).astype(float)
    ok = _usable(both.bid_c, both.ask_c, max_spread) & _usable(both.bid_p, both.ask_p, max_spread)
    both = both[ok]
    if both.empty or not near > 0:
        return None, 0
    strikes = both.index.to_numpy(dtype=float)
    order = np.argsort(np.abs(np.log(strikes / near)), kind="stable")[:pairs]
    chosen = both.iloc[order]
    k = chosen.index.to_numpy(dtype=float)
    diff = ((chosen.bid_c + chosen.ask_c) - (chosen.bid_p + chosen.ask_p)).to_numpy() / 2
    if book == Book.LINEAR:
        each = k + diff
    else:
        with np.errstate(divide="ignore"):
            each = np.where(diff < 1, k / (1 - diff), np.nan)
    each = each[np.isfinite(each) & (each > 0)]
    if each.size == 0:
        return None, 0
    return float(np.median(each)), int(each.size)


def expiry_forwards(
    chain: pd.DataFrame,
    futures: pd.DataFrame,
    book: Book,
    *,
    index_price: float | None,
    pairs: int = 3,
    max_spread: float = 0.2,
) -> dict[int, Forward]:
    """The forward of every expiry in ``chain``, by expiry (UTC ms), in expiry order.

    ``futures`` has one row per dated future of the book: instrument_name, expiry_ms,
    mark_price (from ``optlab.volatility.chain.futures``).
    """
    marks: dict[int, tuple[str, float]] = {}
    for row in futures.itertuples(index=False):
        mark = row.mark_price
        if mark is not None and isinstance(mark, (int, float)) and math.isfinite(mark) and mark > 0:
            marks[int(row.expiry_ms)] = (str(row.instrument_name), float(mark))
    out: dict[int, Forward] = {}
    for expiry, group in sorted(chain.groupby("expiry_ms"), key=lambda kv: int(kv[0])):
        e = int(expiry)
        name, mark = marks.get(e, (None, None))
        near = mark if mark is not None else index_price
        parity, n = (
            parity_forward(group, book, near=near, pairs=pairs, max_spread=max_spread)
            if near is not None
            else (None, 0)
        )
        if mark is not None:
            fwd, source = mark, ForwardSource.FUTURE
        elif parity is not None:
            fwd, source = parity, ForwardSource.PARITY
        else:
            fwd, source = None, ForwardSource.NONE
        out[e] = Forward(
            expiry_ms=e,
            forward=fwd,
            source=source,
            future=mark,
            future_name=name,
            parity=parity,
            parity_pairs=n,
        )
    return out
