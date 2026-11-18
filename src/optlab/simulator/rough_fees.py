"""ROUGH (Phase 2B): Deribit option fees from ``configs/fees/deribit.yaml``, in exact decimals.

- Trading fee per leg: ``min(rate * underlying, cap * price) * amount``, in the premium's
  currency. The underlying is the index price for linear (USDC) options and 1 BTC per contract
  for inverse options. Paper fills cross the spread, so they pay the taker rate.
- Delivery fee at expiry, in-the-money legs only: ``min(rate * delivery price, cap * value) *
  amount``; daily (non-Friday) expiries pay none.

Shortcuts (docs/rough-version.md): Standard tier only; no combo discount; no rounding to
Deribit's fee precision. Phase 4 replaces this with the backtest engine's fee schedule.
"""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from optlab.domain.clock import datetime_to_ms, ms_to_datetime

FEE_FILE = Path("fees") / "deribit.yaml"
Book = Literal["linear", "inverse"]
FRIDAY = 4


def is_daily_expiry(expiry_ms: int) -> bool:
    """Deribit's "daily" options: any option that does not expire on a Friday (UTC)."""
    return ms_to_datetime(expiry_ms).weekday() != FRIDAY


class TradingFees(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    currency: str
    maker: Decimal
    taker: Decimal
    cap_of_premium: Decimal
    daily_maker: Decimal | None = None
    daily_maker_until: date | None = None


class DeliveryFees(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rate: Decimal
    cap_of_value: Decimal


class Delivery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    linear: DeliveryFees
    inverse: DeliveryFees
    daily_exempt: bool

    def for_book(self, book: Book) -> DeliveryFees:
        return self.linear if book == "linear" else self.inverse


class FeeSchedule(BaseModel):
    """One exchange's option fees, valid from ``effective_from``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exchange: str
    source: str
    page_updated: datetime
    read_on: date
    confirmed: str
    effective_from: datetime
    tier: str
    trading: dict[Book, TradingFees]
    delivery: Delivery

    @property
    def effective_from_ms(self) -> int:
        return datetime_to_ms(self.effective_from)

    def _check(self, at_ms: int) -> None:
        if at_ms < self.effective_from_ms:
            raise ValueError(
                f"{ms_to_datetime(at_ms):%Y-%m-%d %H:%M} UTC is before the fee schedule's "
                f"effective date ({self.effective_from:%Y-%m-%d %H:%M} UTC)"
            )

    def trading_fee(
        self,
        book: Book,
        price: Decimal,
        index_price: Decimal,
        amount: Decimal,
        expiry_ms: int,
        *,
        at_ms: int,
        maker: bool = False,
    ) -> Decimal:
        """Fee for trading ``amount`` BTC of one option at ``price`` (premium currency)."""
        self._check(at_ms)
        fees = self.trading[book]
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
        underlying = index_price if book == "linear" else Decimal(1)
        return min(rate * underlying, fees.cap_of_premium * price) * amount

    def delivery_fee(
        self, book: Book, value: Decimal, delivery_price: Decimal, amount: Decimal, expiry_ms: int
    ) -> Decimal:
        """Delivery fee for ``amount`` BTC of an option worth ``value`` per BTC at expiry.

        ``value`` is in the premium currency (USDC for linear). An option that expires worthless
        pays nothing (the cap is 12.5% of its value), and so does a daily option.
        """
        self._check(expiry_ms)
        if self.delivery.daily_exempt and is_daily_expiry(expiry_ms):
            return Decimal(0)
        fees = self.delivery.for_book(book)
        underlying = delivery_price if book == "linear" else Decimal(1)
        return min(fees.rate * underlying, fees.cap_of_value * value) * amount


def load_fee_schedule(config_dir: Path = Path("configs")) -> FeeSchedule:
    data = yaml.safe_load((config_dir / FEE_FILE).read_text(encoding="utf-8"))
    # The file also holds the Phase 4 sections (perpetuals, funding), which this ignores.
    return FeeSchedule.model_validate(
        {k: v for k, v in data.items() if k in FeeSchedule.model_fields}
    )
