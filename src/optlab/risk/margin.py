"""Deribit Standard Margin for BTC_USDC options and the BTC_USDC perpetual (a guard, not a rule).

Damian chose Standard Margin on 18 Nov 2026: what a new Deribit account gets, and the most
conservative choice, because a long option gives no offset to a short one (a credit spread
margins its short leg in full). Formulas and source are in ``configs/risk/v1.yaml``.

Simplifications (docs/backtest.md): no open-order margin (orders fill or vanish within the
minute); Deribit's "projected" margin near expiry is not modelled; the perpetual uses its
smallest-size tier and larger positions are refused.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from optlab.domain.money import quantize

ZERO = Decimal(0)


class MarginRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account: str
    source: str
    page_updated: datetime
    read_on: date
    short_option_im_base: Decimal
    short_option_im_floor: Decimal
    short_option_mm: Decimal
    max_short_option_notional: Decimal
    perpetual_im: Decimal
    perpetual_mm: Decimal
    perpetual_max_size: Decimal


@dataclass(frozen=True)
class OptionMarginInput:
    option_type: str  # call or put
    strike: Decimal
    qty: Decimal  # BTC, negative when short
    mark: Decimal  # the option's mark price (USDC a BTC)
    index: Decimal  # btc_usdc index
    underlying_mark: Decimal  # the expiry's future mark (the option's underlying_price)


@dataclass(frozen=True)
class Margin:
    initial: Decimal
    maintenance: Decimal
    short_notional: Decimal  # short option BTC x index


def short_option_im(
    option_type: str,
    strike: Decimal,
    index: Decimal,
    underlying_mark: Decimal,
    mark: Decimal,
    rules: MarginRules,
) -> Decimal:
    """Initial margin for 1 BTC of a short option (USDC)."""
    base, floor = rules.short_option_im_base, rules.short_option_im_floor
    if option_type == "call":
        otm = max(strike - underlying_mark, ZERO)
        # max(base - OTM/F, floor) x index, written so equal F and index stay exact.
        part = quantize(
            max(base * underlying_mark - otm, floor * underlying_mark) * index / underlying_mark
        )
    else:
        otm = max(underlying_mark - strike, ZERO)
        part = max(
            quantize((base * underlying_mark - otm) * index / underlying_mark), floor * strike
        )
    return part + mark


def short_option_mm(
    option_type: str, strike: Decimal, index: Decimal, mark: Decimal, rules: MarginRules
) -> Decimal:
    """Maintenance margin for 1 BTC of a short option (USDC)."""
    base = index if option_type == "call" else min(index, strike)
    return rules.short_option_mm * base + mark


def option_margin(legs: Iterable[OptionMarginInput], rules: MarginRules) -> Margin:
    """Initial and maintenance margin of option positions; long legs need none."""
    im = mm = notional = ZERO
    for leg in legs:
        if leg.qty >= 0:
            continue
        size = -leg.qty
        im += size * short_option_im(
            leg.option_type, leg.strike, leg.index, leg.underlying_mark, leg.mark, rules
        )
        mm += size * short_option_mm(leg.option_type, leg.strike, leg.index, leg.mark, rules)
        notional += size * leg.index
    return Margin(im, mm, notional)


def perpetual_margin(qty: Decimal, mark: Decimal, rules: MarginRules) -> Margin:
    """The BTC_USDC perpetual's margin at its smallest-size tier (50x)."""
    notional = abs(qty) * mark
    return Margin(rules.perpetual_im * notional, rules.perpetual_mm * notional, ZERO)
