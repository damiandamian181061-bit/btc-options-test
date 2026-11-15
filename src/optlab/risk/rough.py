"""ROUGH (Phase 2B) risk gateway: sizes a proposed spread and checks the limits.

Nothing reaches a paper fill without an ``Approval`` from here (configs/risk/rough.yaml):

- size so the maximum loss (width minus the credit after entry fees) is at most 2% of equity,
  then cut to the quoted size (the short leg's bid size and the long leg's ask size) and floored
  to the contract step (0.01 BTC);
- at most 5 open spreads and 2 per expiry;
- the open spreads' maximum losses at most 10% of equity in total.

Shortcuts (docs/rough-version.md): no portfolio delta or vega limits, daily loss limit, kill
switch, shock filter or cooldown, and no ``risk_events`` log (a rejection is the day's decision
reason). Phase 4 replaces this with the full risk gateway.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from optlab.domain.clock import ms_to_datetime
from optlab.domain.money import dec
from optlab.strategy.rough import Candidate

LIMITS_FILE = Path("risk") / "rough.yaml"


class RoughLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: Literal["rough"]
    currency: Literal["USDC"]
    starting_equity: Decimal
    max_loss_of_equity: Decimal
    max_open_spreads: int
    max_open_per_expiry: int
    max_total_loss_of_equity: Decimal


def load_limits(config_dir: Path = Path("configs")) -> RoughLimits:
    data = yaml.safe_load((config_dir / LIMITS_FILE).read_text(encoding="utf-8"))
    return RoughLimits.model_validate(data)


@dataclass(frozen=True)
class OpenRisk:
    """An open spread, as the limits see it."""

    expiry_ms: int
    max_loss: Decimal  # USDC


@dataclass(frozen=True)
class Approval:
    amount: Decimal | None  # BTC; None when rejected
    max_loss: Decimal | None  # USDC, for the approved amount
    reason: str  # why it was rejected; "" when approved

    @property
    def approved(self) -> bool:
        return self.amount is not None


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    """``value`` rounded down to a whole number of ``step``."""
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _reject(reason: str) -> Approval:
    return Approval(None, None, reason)


def approve(
    c: Candidate, equity: Decimal, open_spreads: Sequence[OpenRisk], limits: RoughLimits
) -> Approval:
    if len(open_spreads) >= limits.max_open_spreads:
        return _reject(f"{len(open_spreads)} spreads open, the limit is {limits.max_open_spreads}")
    same = sum(1 for o in open_spreads if o.expiry_ms == c.expiry_ms)
    if same >= limits.max_open_per_expiry:
        day = ms_to_datetime(c.expiry_ms).date()
        return _reject(
            f"{same} spreads already open on {day}, the limit is {limits.max_open_per_expiry}"
        )
    per_btc = c.max_loss_per_btc
    if per_btc is None or per_btc <= 0:
        return _reject("no maximum loss to size against")
    step = dec(c.short.min_trade_amount)
    by_risk = limits.max_loss_of_equity * equity / per_btc
    quoted = min(dec(c.short.bid_size), dec(c.long.ask_size))
    amount = floor_to(min(by_risk, quoted), step)
    if amount < step:
        binding = "quoted size" if quoted < by_risk else "2% risk size"
        return _reject(f"size {amount} BTC is under the minimum {step} BTC ({binding})")
    max_loss = amount * per_btc
    total = sum((o.max_loss for o in open_spreads), Decimal(0)) + max_loss
    if total > limits.max_total_loss_of_equity * equity:
        return _reject(
            f"total maximum loss {total:,.2f} USDC would exceed "
            f"{limits.max_total_loss_of_equity:.0%} of equity"
        )
    return Approval(amount, max_loss, "")
