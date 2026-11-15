"""ROUGH (Phase 2B): the rough daily credit-spread rule (configs/strategies/rough.yaml).

Once a day, on BTC_USDC options and Deribit's own Greeks and mark IVs:

1. Filters: the 30-day ATM implied vol must be above the 30-day realised vol, and BTC must be
   above (bull put) or below (bear call) its 20-day average.
2. Candidates: every short leg of the chosen type with |delta| 0.15 to 0.30 and 7 to 30 days to
   expiry, each with its long leg: the nearest further-out strike 2% to 6% of the forward away.
3. Checks (v1 rules, confirmed for the rough version): both legs two-sided and not crossed,
   bid-ask at most 10% of mid, quotes under 2 minutes old, credit after fees at least 20% of
   the width.
4. Choice: among the candidates passing every check, the |delta| closest to 0.20 (ties: the
   nearer expiry, then the strike). Sizing and limits are the risk gateway's job
   (``optlab.risk.rough``).
5. Exits, every minute: buy back when the executable price (short ask - long bid) is at most
   50% of the entry credit (short bid - long ask), or at least 2 times it, both before fees.

Every shortcut is listed in docs/rough-version.md. Phase 5 replaces this with the v1 candidate
generator and fixed-rule selector.
"""

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from optlab.domain.clock import MS_PER_DAY, ms_to_datetime
from optlab.domain.money import dec
from optlab.ledger.rough import Side, option_type_of
from optlab.simulator.rough_fees import FeeSchedule
from optlab.volatility.rough import (
    DAYS_PER_YEAR,
    atm_iv,
    iv_at_tenor,
    moving_average,
    realised_vol,
    strike_ivs,
    tau_years,
)

RULE_FILE = Path("strategies") / "rough.yaml"
ExitReason = Literal["take profit", "stop loss"]


class RoughRule(BaseModel):
    """The rough rule's parameters (configs/strategies/rough.yaml)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: Literal["rough"]
    book: Literal["linear"]
    index_name: str
    decision_hour_utc: int
    iv_tenor_days: float
    rv_returns: int
    trend_days: int
    iv_minus_rv_min: float
    dte_min_days: float
    dte_max_days: float
    short_delta_target: float
    short_delta_min: float
    short_delta_max: float
    width_min_of_forward: float
    width_max_of_forward: float
    max_spread_of_mid: float
    max_quote_age_s: float
    min_credit_of_width: float
    take_profit_of_credit: Decimal
    stop_loss_of_credit: Decimal


def load_rule(config_dir: Path = Path("configs")) -> RoughRule:
    data = yaml.safe_load((config_dir / RULE_FILE).read_text(encoding="utf-8"))
    return RoughRule.model_validate(data)


@dataclass(frozen=True)
class OptionQuote:
    """One option of the book at a snapshot minute (quote joined with its instrument)."""

    instrument_name: str
    option_type: str
    strike: float
    expiry_ms: int
    bid: float | None
    ask: float | None
    bid_size: float
    ask_size: float
    mark_price: float | None
    mark_iv: float | None
    delta: float | None
    underlying_price: float | None
    index_price: float | None
    exchange_ts_ms: int
    min_trade_amount: float


# ------------------------------------------------------------------------------------ filters


@dataclass(frozen=True)
class Signals:
    iv30: float | None
    rv30: float | None
    index_price: float | None
    trend_average: float | None
    side: Side | None
    reason: str  # why there is no side; "" when the filters pass


def atm_iv_30(chain: Iterable[OptionQuote], ts_ms: int, rule: RoughRule) -> float | None:
    """30-day ATM IV: each expiry's mark IV at its forward, then total variance in time."""
    by_expiry: dict[int, list[OptionQuote]] = defaultdict(list)
    for o in chain:
        if o.expiry_ms > ts_ms:
            by_expiry[o.expiry_ms].append(o)
    points: list[tuple[float, float]] = []
    for expiry, options in by_expiry.items():
        forwards = [o.underlying_price for o in options if o.underlying_price]
        if not forwards:
            continue
        iv = atm_iv(strike_ivs((o.strike, o.mark_iv) for o in options), statistics.median(forwards))
        if iv is not None:
            points.append((tau_years(expiry, ts_ms), iv))
    return iv_at_tenor(points, rule.iv_tenor_days / DAYS_PER_YEAR)


def price_history(
    delivery: Sequence[tuple[date, Decimal]], ts_ms: int, rule: RoughRule
) -> tuple[list[float], str]:
    """Daily closes (delivery prices) for RV and the trend, or the reason there are not enough.

    Uses the run of consecutive days ending at the latest known price, which must be from the
    snapshot's day or the day before.
    """
    need = max(rule.rv_returns + 1, rule.trend_days)
    if not delivery:
        return [], "no delivery prices"
    rows = sorted(delivery)
    today = ms_to_datetime(ts_ms).date()
    if rows[-1][0] < today - timedelta(days=1):
        return [], f"the latest delivery price is from {rows[-1][0]}"
    run = [rows[-1]]
    for row in reversed(rows[:-1]):
        if row[0] != run[-1][0] - timedelta(days=1):
            break
        run.append(row)
    if len(run) < need:
        return [], f"needs {need} consecutive daily delivery prices, has {len(run)}"
    return [float(p) for _, p in reversed(run)], ""


def signals(
    chain: Sequence[OptionQuote],
    delivery: Sequence[tuple[date, Decimal]],
    index_price: float | None,
    ts_ms: int,
    rule: RoughRule,
) -> Signals:
    iv30 = atm_iv_30(chain, ts_ms, rule)
    prices, why = price_history(delivery, ts_ms, rule)
    rv30 = realised_vol(prices, rule.rv_returns) if prices else None
    average = moving_average(prices, rule.trend_days) if prices else None

    def no(reason: str) -> Signals:
        return Signals(iv30, rv30, index_price, average, None, reason)

    if iv30 is None:
        return no("no 30-day ATM IV: needs BTC_USDC expiries on both sides of 30 days")
    if rv30 is None or average is None:
        return no(f"no 30-day realised vol: {why}")
    if not iv30 - rv30 > rule.iv_minus_rv_min:
        return no(f"IV30 {iv30:.2%} is not above RV30 {rv30:.2%}")
    if index_price is None:
        return no(f"no {rule.index_name} index price at the snapshot")
    if index_price == average:
        return no(f"BTC is exactly at its {rule.trend_days}-day average")
    side: Side = "bull put" if index_price > average else "bear call"
    return Signals(iv30, rv30, index_price, average, side, "")


# ------------------------------------------------------------------------------------ candidates


@dataclass(frozen=True)
class Candidate:
    side: Side
    short: OptionQuote
    long: OptionQuote
    forward: float
    width: float
    credit: Decimal | None  # short bid - long ask, per BTC, before fees
    fees_per_btc: Decimal
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def credit_after_fees(self) -> Decimal | None:
        return None if self.credit is None else self.credit - self.fees_per_btc

    @property
    def max_loss_per_btc(self) -> Decimal | None:
        """Width minus the credit after entry fees: the most a spread can lose per BTC."""
        net = self.credit_after_fees
        return None if net is None else dec(self.width) - net

    @property
    def expiry_ms(self) -> int:
        return self.short.expiry_ms


@dataclass
class Screen:
    """Every candidate spread of one side, with why each failed (if it did)."""

    candidates: list[Candidate] = field(default_factory=list)
    short_legs: int = 0  # short legs in the delta band and the expiry window
    no_long_leg: int = 0  # of those, how many had no strike 2% to 6% further out

    def summary(self) -> str:
        counts = Counter(f for c in self.candidates for f in c.failures)
        passed = sum(c.ok for c in self.candidates)
        parts = [f"{self.short_legs} short legs in the delta band, {passed} pass every check"]
        if self.no_long_leg:
            parts.append(f"no long leg 2-6% away: {self.no_long_leg}")
        parts += [f"{name}: {n}" for name, n in sorted(counts.items())]
        return "; ".join(parts)


def _leg_failures(leg: OptionQuote, label: str, ts_ms: int, rule: RoughRule) -> list[str]:
    if leg.bid is None or leg.ask is None or leg.bid <= 0 or leg.ask <= 0:
        return [f"{label}: not two-sided"]
    out: list[str] = []
    if leg.bid >= leg.ask:
        out.append(f"{label}: crossed book")
    elif (leg.ask - leg.bid) / ((leg.ask + leg.bid) / 2) > rule.max_spread_of_mid:
        out.append(f"{label}: bid-ask over {rule.max_spread_of_mid:.0%} of mid")
    if not ts_ms - leg.exchange_ts_ms < rule.max_quote_age_s * 1000:
        out.append(f"{label}: quote over {rule.max_quote_age_s / 60:.0f} minutes old")
    return out


def _check(
    side: Side,
    short: OptionQuote,
    long: OptionQuote,
    forward: float,
    ts_ms: int,
    rule: RoughRule,
    fees: FeeSchedule,
) -> Candidate:
    width = abs(short.strike - long.strike)
    failures = _leg_failures(short, "short leg", ts_ms, rule)
    failures += _leg_failures(long, "long leg", ts_ms, rule)
    credit: Decimal | None = None
    fee = Decimal(0)
    if short.bid is not None and long.ask is not None:
        credit = dec(short.bid) - dec(long.ask)
        if short.index_price is None:
            failures.append("no index price for the fee")
        else:
            index, one = dec(short.index_price), Decimal(1)
            for price in (short.bid, long.ask):
                fee += fees.trading_fee(
                    rule.book, dec(price), index, one, short.expiry_ms, at_ms=ts_ms
                )
        if credit <= 0:
            failures.append("no credit")
        elif credit - fee < dec(rule.min_credit_of_width) * dec(width):
            failures.append(f"credit after fees under {rule.min_credit_of_width:.0%} of the width")
    return Candidate(side, short, long, forward, width, credit, fee, tuple(failures))


def candidates(
    chain: Sequence[OptionQuote], side: Side, ts_ms: int, rule: RoughRule, fees: FeeSchedule
) -> Screen:
    option_type = option_type_of(side)
    screen = Screen()
    by_expiry: dict[int, list[OptionQuote]] = defaultdict(list)
    for o in chain:
        days = (o.expiry_ms - ts_ms) / MS_PER_DAY
        if o.option_type == option_type and rule.dte_min_days <= days <= rule.dte_max_days:
            by_expiry[o.expiry_ms].append(o)
    for expiry in sorted(by_expiry):
        legs = sorted(by_expiry[expiry], key=lambda o: o.strike)
        for short in legs:
            if short.delta is None or not (
                rule.short_delta_min <= abs(short.delta) <= rule.short_delta_max
            ):
                continue
            screen.short_legs += 1
            forward = short.underlying_price
            further = [
                o
                for o in legs
                if forward
                and (o.strike < short.strike if option_type == "put" else o.strike > short.strike)
                and rule.width_min_of_forward
                <= abs(short.strike - o.strike) / forward
                <= rule.width_max_of_forward
            ]
            if not further or not forward:
                screen.no_long_leg += 1
                continue
            long = min(further, key=lambda o: abs(short.strike - o.strike))
            screen.candidates.append(_check(side, short, long, forward, ts_ms, rule, fees))
    return screen


def rank(cands: Iterable[Candidate], rule: RoughRule) -> list[Candidate]:
    """Candidates that pass every check, |delta| closest to the target first."""

    def key(c: Candidate) -> tuple[float, int, float, str]:
        delta = abs(c.short.delta or 0.0)
        return (
            abs(delta - rule.short_delta_target),
            c.expiry_ms,
            c.short.strike,
            c.short.instrument_name,
        )

    return sorted((c for c in cands if c.ok), key=key)


# ------------------------------------------------------------------------------------ exits


def buyback_price(
    short: OptionQuote, long: OptionQuote, amount: Decimal, ts_ms: int, rule: RoughRule
) -> tuple[Decimal | None, str]:
    """Executable price per BTC to close the spread now (short ask - long bid), or why not.

    Both legs must be quoted on the side we trade, not crossed, under 2 minutes old, and show
    at least the position's size.
    """
    if short.ask is None:
        return None, f"{short.instrument_name}: no ask"
    if long.bid is None:
        return None, f"{long.instrument_name}: no bid"
    for leg in (short, long):
        if leg.bid is not None and leg.ask is not None and leg.bid >= leg.ask:
            return None, f"{leg.instrument_name}: crossed book"
        if not ts_ms - leg.exchange_ts_ms < rule.max_quote_age_s * 1000:
            return None, f"{leg.instrument_name}: quote over 2 minutes old"
    if dec(short.ask_size) < amount or dec(long.bid_size) < amount:
        return None, "quoted size is smaller than the position"
    return dec(short.ask) - dec(long.bid), ""


def exit_reason(entry_credit: Decimal, buyback: Decimal, rule: RoughRule) -> ExitReason | None:
    if buyback <= rule.take_profit_of_credit * entry_credit:
        return "take profit"
    if buyback >= rule.stop_loss_of_credit * entry_credit:
        return "stop loss"
    return None


def thesis(side: Side, short_strike: float, expiry_ms: int) -> str:
    when = ms_to_datetime(expiry_ms)
    where = "above" if side == "bull put" else "below"
    return (
        f"BTC stays {where} {short_strike:,.0f} until expiry on {when.day} {when:%b %Y %H:%M} "
        "UTC; buy back at half the credit."
    )
