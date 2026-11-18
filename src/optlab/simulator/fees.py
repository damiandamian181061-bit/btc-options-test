"""Deribit fees and funding for the backtest engine, from ``configs/fees/deribit.yaml``.

Everything is an exact Decimal in the book's currency (USDC for BTC_USDC options and the
BTC_USDC perpetual). Sources: Deribit's fee page (re-read 18 Nov 2026, unchanged since 11 Nov)
and its Funding Specifications page (read 18 Nov 2026); the links are in the config.

- Option trading fee per leg: ``min(rate x index, 12.5% x price) x amount``. Paper fills cross
  the spread, so they pay the taker rate; legs fill one by one and each pays (no combo discount:
  Damian, 18 Nov 2026).
- Option delivery fee at expiry, on in-the-money legs only:
  ``min(rate x delivery price, 12.5% x value) x amount``; daily (non-Friday) expiries pay none.
- Perpetual trading fee: ``rate x price x amount`` (a fraction of the traded notional).
- Funding (no fee): ``rate x size x index x elapsed / 8 hours``, with the rate from the
  perpetual's mark and the index through Deribit's damper and cap; longs pay when it is positive.

``scaled(m)`` multiplies every fee by ``m`` (cost sensitivity, 0.5 to 3 times in Phase 5); it
never scales funding, which is a transfer between traders, not a cost of trading.

Not modelled (docs/backtest.md): the combo discount, VIP tiers, block trades, liquidation and
collateral fees, and Deribit's rounding of fees.
"""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from optlab.domain.clock import MS_PER_HOUR, datetime_to_ms, ms_to_datetime
from optlab.domain.money import quantize

FEE_FILE = Path("fees") / "deribit.yaml"
FRIDAY = 4
BOOK = "linear"  # the main book (BTC_USDC); the inverse fallback is not backtested yet


def is_daily_expiry(expiry_ms: int) -> bool:
    """Deribit's "daily" options: any option that does not expire on a Friday (UTC)."""
    return ms_to_datetime(expiry_ms).weekday() != FRIDAY


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OptionTradingFees(_Frozen):
    currency: str
    maker: Decimal
    taker: Decimal
    cap_of_premium: Decimal
    daily_maker: Decimal | None = None
    daily_maker_until: date | None = None


class DeliveryFees(_Frozen):
    rate: Decimal
    cap_of_value: Decimal


class Delivery(_Frozen):
    linear: DeliveryFees
    inverse: DeliveryFees
    daily_exempt: bool


class PerpetualFees(_Frozen):
    currency: str
    maker: Decimal
    taker: Decimal


class FundingRules(_Frozen):
    source: str
    page_updated: datetime
    read_on: date
    damper: Decimal
    period_hours: int
    cap: dict[str, Decimal]

    @property
    def period_ms(self) -> int:
        return self.period_hours * MS_PER_HOUR


class FeeSchedule(_Frozen):
    """Deribit's fees for one tier, valid from ``effective_from``; ``multiplier`` scales them."""

    exchange: str
    source: str
    page_updated: datetime
    read_on: date
    confirmed: str
    effective_from: datetime
    tier: str
    trading: dict[str, OptionTradingFees]
    delivery: Delivery
    perpetual: dict[str, PerpetualFees]
    funding: FundingRules
    combo_discount: bool
    multiplier: Decimal = Decimal(1)

    @property
    def currency(self) -> str:
        return self.trading[BOOK].currency

    def scaled(self, multiplier: Decimal) -> "FeeSchedule":
        """The same schedule with every fee multiplied by ``multiplier``."""
        if multiplier < 0:
            raise ValueError("a fee multiplier cannot be negative")
        return self.model_copy(update={"multiplier": self.multiplier * multiplier})

    def _check(self, at_ms: int) -> None:
        if at_ms < datetime_to_ms(self.effective_from):
            raise ValueError(
                f"{ms_to_datetime(at_ms):%Y-%m-%d %H:%M} UTC is before the fee schedule's "
                f"effective date ({self.effective_from:%Y-%m-%d %H:%M} UTC)"
            )

    def option_fee(
        self,
        price: Decimal,
        index_price: Decimal,
        amount: Decimal,
        expiry_ms: int,
        *,
        at_ms: int,
        maker: bool = False,
    ) -> Decimal:
        """Trading fee for ``amount`` BTC of one BTC_USDC option at ``price`` USDC."""
        self._check(at_ms)
        fees = self.trading[BOOK]
        rate = fees.maker if maker else fees.taker
        if (
            maker
            and fees.daily_maker is not None
            and is_daily_expiry(expiry_ms)
            and (
                fees.daily_maker_until is None
                or ms_to_datetime(at_ms).date() <= fees.daily_maker_until
            )
        ):
            rate = fees.daily_maker
        fee = min(rate * index_price, fees.cap_of_premium * price) * amount
        return fee * self.multiplier

    def option_delivery_fee(
        self, value: Decimal, delivery_price: Decimal, amount: Decimal, expiry_ms: int
    ) -> Decimal:
        """Delivery fee for ``amount`` BTC of an option worth ``value`` USDC a BTC at expiry."""
        self._check(expiry_ms)
        if value <= 0 or (self.delivery.daily_exempt and is_daily_expiry(expiry_ms)):
            return Decimal(0)
        fees = self.delivery.linear
        fee = min(fees.rate * delivery_price, fees.cap_of_value * value) * amount
        return fee * self.multiplier

    def perpetual_fee(
        self, price: Decimal, amount: Decimal, *, at_ms: int, maker: bool = False
    ) -> Decimal:
        """Trading fee for ``amount`` BTC of the BTC_USDC perpetual at ``price`` USDC."""
        self._check(at_ms)
        fees = self.perpetual[BOOK]
        rate = fees.maker if maker else fees.taker
        return rate * price * amount * self.multiplier


def funding_rate(mark: Decimal, index: Decimal, rules: FundingRules, instrument: str) -> Decimal:
    """Deribit's funding rate per period (8 hours) from the perpetual's mark and the index."""
    if index <= 0:
        raise ValueError("the index must be positive")
    premium = quantize((mark - index) / index)
    uncapped = max(rules.damper, premium) + min(-rules.damper, premium)
    cap = rules.cap[instrument]
    return min(cap, max(-cap, uncapped))


def funding_payment(
    rate: Decimal, size: Decimal, index: Decimal, elapsed_ms: int, rules: FundingRules
) -> Decimal:
    """What a position of ``size`` BTC (negative if short) pays over ``elapsed_ms``.

    Positive means paid, negative means received. The position's notional is ``size x index``.
    Rounded to 1e-12 USDC (a division).
    """
    return quantize(rate * size * index * Decimal(elapsed_ms) / Decimal(rules.period_ms))


def load_fees(config_dir: Path = Path("configs")) -> FeeSchedule:
    data = yaml.safe_load((config_dir / FEE_FILE).read_text(encoding="utf-8"))
    return FeeSchedule.model_validate(data)
