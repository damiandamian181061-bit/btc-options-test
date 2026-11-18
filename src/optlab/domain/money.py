"""Exact decimals for money (CLAUDE.md: the cash ledger uses exact decimals)."""

import math
from decimal import ROUND_HALF_EVEN, Decimal

# Divisions (a partial close's share of the entry value, funding) are the only amounts that can
# have endless decimals. They are rounded to 1e-12 of a currency unit (half to even) before they
# are booked, so every sum the ledger forms is exact (CLAUDE.md: exact decimals).
QUANTUM = Decimal("1E-12")


def quantize(value: Decimal) -> Decimal:
    """``value`` rounded to 1e-12 (half to even): for the results of divisions only."""
    return value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


def dec(value: float | int | str | Decimal) -> Decimal:
    """A Decimal holding exactly the number a float prints as (``repr``), not its binary value.

    Prices arrive as floats (860.0, 64722.17); ``Decimal(64722.17)`` would carry binary noise,
    ``dec(64722.17)`` is exactly 64722.17. NaN and infinities are refused.
    """
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"not a finite number: {value!r}")
        result = Decimal(repr(value))
    else:
        result = Decimal(value)
    if not result.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return result
