"""Realised volatility, close to close, from daily closes (docs/formulas.md).

The daily close is the book's price index (``btc_usdc`` or ``btc_usd``) in the 08:00 UTC
snapshot, the minute Deribit's expiries settle. Our own recording began on 13 Nov 2026, so for
days before that, and any day whose 08:00 snapshot is missing, Deribit's 08:00 delivery price
for the day stands in (``CloseSource.DELIVERY``). A delivery price is the 30-minute average of
the index up to 08:00, so it is slightly smoother; every result says how many closes it used.

    r_d = ln(C_d / C_{d-1}),   realised vol over n returns = sqrt(365 * mean(r_d^2))

with zero mean (over a month the mean daily return is noise, and assuming zero keeps the
estimate unbiased for a driftless price), annualised over 365 days because BTC trades every day.
The n + 1 closes must be on consecutive days.

A close for day d is known from 08:00 UTC on d (our own snapshot), or from when the recorder
fetched the delivery price (08:01 UTC or later): ``closes_known_at`` keeps only those.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from optlab.domain.clock import datetime_to_ms

CLOSE_HOUR_UTC = 8
DAYS_PER_YEAR = 365.0


class CloseSource(StrEnum):
    INDEX = "index"  # our own 08:00 snapshot of the index
    DELIVERY = "delivery"  # Deribit's 08:00 delivery price


@dataclass(frozen=True, slots=True)
class DailyClose:
    day: date
    price: float
    source: CloseSource


@dataclass(frozen=True, slots=True)
class RealisedVol:
    vol: float  # annualised, decimal
    returns: int  # daily returns used
    from_delivery: int  # closes (of returns + 1) that were delivery prices


def close_ms(day: date) -> int:
    """08:00 UTC on ``day``, in UTC milliseconds."""
    return datetime_to_ms(datetime(day.year, day.month, day.day, CLOSE_HOUR_UTC, tzinfo=UTC))


def merge_closes(index: Mapping[date, float], delivery: Mapping[date, float]) -> list[DailyClose]:
    """One close per day, our own index close where we have it, else the delivery price."""
    out: list[DailyClose] = []
    for day in sorted(set(index) | set(delivery)):
        if day in index:
            out.append(DailyClose(day, float(index[day]), CloseSource.INDEX))
        else:
            out.append(DailyClose(day, float(delivery[day]), CloseSource.DELIVERY))
    return out


def closes_known_at(closes: Sequence[DailyClose], ts_ms: int) -> list[DailyClose]:
    """Closes whose 08:00 UTC is at or before ``ts_ms``."""
    return [c for c in closes if close_ms(c.day) <= ts_ms]


def realised_vol(closes: Sequence[DailyClose], returns: int) -> RealisedVol | None:
    """Close-to-close vol over the last ``returns`` daily returns; None without enough days."""
    if returns < 1 or len(closes) < returns + 1:
        return None
    window = list(closes[-(returns + 1) :])
    if any(b.day - a.day != timedelta(days=1) for a, b in pairwise(window)):
        return None
    if any(not c.price > 0 for c in window):
        return None
    r = [math.log(b.price / a.price) for a, b in pairwise(window)]
    vol = math.sqrt(DAYS_PER_YEAR * sum(x * x for x in r) / len(r))
    from_delivery = sum(1 for c in window if c.source == CloseSource.DELIVERY)
    return RealisedVol(vol=vol, returns=returns, from_delivery=from_delivery)


def available_returns(closes: Sequence[DailyClose], limit: int) -> int:
    """Consecutive daily returns ending at the last close, at most ``limit``."""
    n = 0
    for a, b in pairwise(reversed(list(closes))):
        if a.day - b.day != timedelta(days=1) or n >= limit:
            break
        n += 1
    return n
