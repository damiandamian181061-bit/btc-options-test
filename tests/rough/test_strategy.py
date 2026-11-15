"""ROUGH (Phase 2B) rule: filters, candidate spreads, the 0.20-delta choice and the exits.

Phase 5 replaces this with the v1 candidate generator and selector.
"""

import math
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from optlab.domain.clock import MS_PER_DAY, MS_PER_SECOND
from optlab.simulator.rough_fees import FeeSchedule, load_fee_schedule
from optlab.strategy.rough import (
    OptionQuote,
    RoughRule,
    buyback_price,
    candidates,
    exit_reason,
    load_rule,
    price_history,
    rank,
    signals,
    thesis,
)
from tests.conftest import REPO_ROOT
from tests.market_data.synthetic import utc_ms

D = Decimal
TS = utc_ms(2026, 11, 15, 9)
E1 = utc_ms(2026, 11, 27, 8)  # 11.96 days: in the 7-30 day window
E2 = utc_ms(2026, 12, 25, 8)  # 39.96 days: outside it, but brackets 30 days for the IV
E0 = utc_ms(2026, 11, 18, 8)  # 2.96 days: outside
F = 65_000.0


@pytest.fixture
def rule() -> RoughRule:
    return load_rule(REPO_ROOT / "configs")


@pytest.fixture
def fees() -> FeeSchedule:
    return load_fee_schedule(REPO_ROOT / "configs")


def q(
    strike: float,
    option_type: str = "call",
    expiry: int = E1,
    *,
    delta: float = 0.2,
    bid: float | None = 500.0,
    ask: float | None = 520.0,
    size: float = 10.0,
    iv: float | None = 0.45,
    age_s: float = 0.5,
) -> OptionQuote:
    label = {E1: "27NOV26", E2: "25DEC26", E0: "18NOV26"}[expiry]
    return OptionQuote(
        instrument_name=f"BTC_USDC-{label}-{strike:.0f}-{option_type[0].upper()}",
        option_type=option_type,
        strike=strike,
        expiry_ms=expiry,
        bid=bid,
        ask=ask,
        bid_size=size,
        ask_size=size,
        mark_price=None if bid is None or ask is None else (bid + ask) / 2,
        mark_iv=iv,
        delta=delta if option_type == "call" else -delta,
        underlying_price=F,
        index_price=64_900.0,
        exchange_ts_ms=TS - round(age_s * MS_PER_SECOND),
        min_trade_amount=0.01,
    )


def calls() -> list[OptionQuote]:
    """Bear call candidates on E1 (forward 65,000): 2% of the forward is 1,300.

    Fees are 19.47 per BTC on every leg here (0.0003 * index 64,900, under each cap), so a
    2,000-wide spread needs a credit of 400 + 38.94 = 438.94 to pass the 20% minimum.
    """
    return [
        q(66_000, delta=0.35, bid=2000, ask=2040),
        q(68_000, delta=0.27, bid=1300, ask=1330),  # / 70,000: credit 430, fails the minimum
        q(69_000, delta=0.22, bid=1100, ask=1130),  # / 71,000: credit 480, passes
        q(70_000, delta=0.19, bid=840, ask=870),  # / 72,000: credit 440, passes; closest to 0.20
        q(71_000, delta=0.16, bid=600, ask=620),  # / 74,000 (3,000 wide): credit 440, fails
        q(72_000, delta=0.13, bid=380, ask=400),
        q(74_000, delta=0.08, bid=150, ask=160),
    ]


def history(first: float, daily: float, days: int = 31, end: date = date(2026, 11, 15)):
    """Consecutive daily delivery prices ending on ``end``, moving by ``daily`` each day."""
    start = end - timedelta(days=days - 1)
    return [(start + timedelta(days=i), D(str(round(first * daily**i, 2)))) for i in range(days)]


def term_structure() -> list[OptionQuote]:
    """ATM mark IVs: 0.50 on E1 and 0.40 on E2 (strikes around the forward)."""
    return [
        q(64_000, "put", E1, iv=0.50),
        q(66_000, "call", E1, iv=0.50),
        q(64_000, "put", E2, iv=0.40),
        q(66_000, "call", E2, iv=0.40),
    ]


def test_signals_bear_call_when_iv_is_above_rv_and_btc_is_below_its_average(
    rule: RoughRule,
) -> None:
    # Falling 1% a day: RV = ln(1/0.99) * sqrt(365) = 0.1920; BTC now below its 20-day mean.
    s = signals(term_structure(), history(80_000, 0.99), 59_000.0, TS, rule)
    tau1, tau2, tau = (E1 - TS) / (365 * MS_PER_DAY), (E2 - TS) / (365 * MS_PER_DAY), 30 / 365
    w = 0.25 * tau1 + (0.16 * tau2 - 0.25 * tau1) * (tau - tau1) / (tau2 - tau1)
    assert s.iv30 == pytest.approx(math.sqrt(w / tau))
    assert s.rv30 == pytest.approx(-math.log(0.99) * math.sqrt(365), rel=1e-4)
    assert s.side == "bear call"
    assert s.reason == ""
    assert s.trend_average is not None
    assert s.index_price is not None
    assert s.index_price < s.trend_average


def test_signals_bull_put_in_an_uptrend(rule: RoughRule) -> None:
    s = signals(term_structure(), history(60_000, 1.01), 80_000.0, TS, rule)
    assert (s.side, s.reason) == ("bull put", "")


def test_no_trade_when_iv_is_not_above_rv(rule: RoughRule) -> None:
    # Daily moves of 4%: RV = 0.0392 * 19.1 = 0.75, above the 0.43-0.45 IV.
    s = signals(term_structure(), history(80_000, 0.96), 59_000.0, TS, rule)
    assert s.side is None
    assert s.reason.startswith("IV30 ")
    assert "is not above RV30" in s.reason


def test_no_trade_without_a_full_consecutive_price_history(rule: RoughRule) -> None:
    prices = history(80_000, 0.99)
    gap = prices[:10] + prices[11:]  # one day missing: only the last 20 are consecutive
    assert "consecutive" in signals(term_structure(), gap, 59_000.0, TS, rule).reason
    stale = history(80_000, 0.99, end=date(2026, 11, 12))
    assert "latest delivery price" in signals(term_structure(), stale, 59_000.0, TS, rule).reason
    assert price_history(prices, TS, rule)[0] == [float(p) for _, p in prices]


def test_no_trade_without_expiries_on_both_sides_of_30_days(rule: RoughRule) -> None:
    near_only = [x for x in term_structure() if x.expiry_ms == E1]
    s = signals(near_only, history(80_000, 0.99), 59_000.0, TS, rule)
    assert s.iv30 is None
    assert "30-day ATM IV" in s.reason


def test_no_trade_when_btc_is_exactly_at_its_average(rule: RoughRule) -> None:
    flat = history(70_000, 1.0)
    s = signals(term_structure(), flat, 70_000.0, TS, rule)
    # RV is 0 so IV is above it, but there is no trend.
    assert s.side is None
    assert "20-day average" in s.reason


def test_candidates_pick_the_long_leg_by_the_width_rule(rule: RoughRule, fees: FeeSchedule) -> None:
    screen = candidates(calls(), "bear call", TS, rule, fees)
    pairs = {(c.short.strike, c.long.strike) for c in screen.candidates}
    # Short legs with |delta| 0.15 to 0.30: 68,000 (0.27), 69,000, 70,000, 71,000 (0.16).
    assert pairs == {(68_000, 70_000), (69_000, 71_000), (70_000, 72_000), (71_000, 74_000)}
    assert screen.short_legs == 4
    c = next(c for c in screen.candidates if c.short.strike == 70_000)
    assert c.width == 2_000
    assert c.credit == D("440")  # 840 - 400
    # Fees per BTC: 0.0003 * 64,900 = 19.47 on each leg (caps 105 and 50).
    assert c.fees_per_btc == D("38.94")
    assert c.credit_after_fees == D("401.06")
    assert c.max_loss_per_btc == D("1598.94")
    assert c.ok


def test_rank_takes_the_closest_to_0_20_delta_that_passes_every_check(
    rule: RoughRule, fees: FeeSchedule
) -> None:
    chain = calls()
    best = rank(candidates(chain, "bear call", TS, rule, fees).candidates, rule)
    assert [(c.short.strike, c.long.strike) for c in best] == [(70_000, 72_000), (69_000, 71_000)]

    # The 72,000 long leg goes stale: the 70,000 spread fails, the next closest (0.22) wins.
    chain = [replace(x, exchange_ts_ms=TS - 121_000) if x.strike == 72_000 else x for x in chain]
    screen = candidates(chain, "bear call", TS, rule, fees)
    failed = next(c for c in screen.candidates if c.short.strike == 70_000)
    assert failed.failures == ("long leg: quote over 2 minutes old",)
    assert rank(screen.candidates, rule)[0].short.strike == 69_000


@pytest.mark.parametrize(
    ("change", "failure"),
    [
        ({"bid": None}, "short leg: not two-sided"),
        ({"bid": 870.0, "ask": 870.0}, "short leg: crossed book"),
        ({"bid": 700.0, "ask": 870.0}, "short leg: bid-ask over 10% of mid"),
    ],
)
def test_each_liquidity_check(rule: RoughRule, fees: FeeSchedule, change, failure) -> None:
    chain = [replace(x, **change) if x.strike == 70_000 else x for x in calls()]
    c = next(
        c
        for c in candidates(chain, "bear call", TS, rule, fees).candidates
        if c.short.strike == 70_000
    )
    assert failure in c.failures


def test_minimum_credit_after_fees(rule: RoughRule, fees: FeeSchedule) -> None:
    # 20% of a 2,000 width is 400. Credit 450 - 19.47 * 2 = 411.06 passes; 430 fails.
    chain = [
        q(70_000, delta=0.19, bid=640, ask=660),
        q(72_000, delta=0.13, bid=180, ask=190),
    ]
    ok = candidates(chain, "bear call", TS, rule, fees).candidates[0]
    assert ok.credit_after_fees == D("411.06")
    assert ok.ok
    chain[0] = replace(chain[0], bid=620.0)
    low = candidates(chain, "bear call", TS, rule, fees).candidates[0]
    assert low.failures == ("credit after fees under 20% of the width",)


def test_when_the_0_20_delta_spread_fails_the_minimum_credit_the_next_passing_one_wins(
    rule: RoughRule, fees: FeeSchedule
) -> None:
    # 70,000 / 72,000 (delta 0.19) with the short bid down to 800: credit 400, after fees
    # 361.06, under 400. The next closest to 0.20 that passes is 69,000 / 71,000 (0.22).
    chain = [replace(x, bid=800.0) if x.strike == 70_000 else x for x in calls()]
    screen = candidates(chain, "bear call", TS, rule, fees)
    closest = next(c for c in screen.candidates if c.short.strike == 70_000)
    assert closest.failures == ("credit after fees under 20% of the width",)
    chosen = rank(screen.candidates, rule)
    assert [(c.short.strike, c.long.strike) for c in chosen] == [(69_000, 71_000)]


def test_when_no_spread_passes_the_minimum_credit_there_is_none_to_choose(
    rule: RoughRule, fees: FeeSchedule
) -> None:
    # Every price 20% lower (bid-ask still under 10% of mid): the credits drop to 344-384 on the
    # 2,000-wide spreads and 352 on the 3,000-wide one, so none reaches the 20% minimum.
    chain = [
        replace(x, bid=x.bid * 0.8, ask=x.ask * 0.8) if x.bid and x.ask else x for x in calls()
    ]
    screen = candidates(chain, "bear call", TS, rule, fees)
    assert rank(screen.candidates, rule) == []
    assert screen.summary() == (
        "4 short legs in the delta band, 0 pass every check; "
        "credit after fees under 20% of the width: 4"
    )


def test_only_expiries_7_to_30_days_away_and_the_right_option_type(
    rule: RoughRule, fees: FeeSchedule
) -> None:
    chain = [*calls(), q(70_000, expiry=E0), q(72_000, expiry=E0), q(70_000, expiry=E2)]
    chain += [q(60_000, "put", delta=0.2), q(58_000, "put", delta=0.1)]
    screen = candidates(chain, "bear call", TS, rule, fees)
    assert {c.short.expiry_ms for c in screen.candidates} == {E1}
    assert all(c.short.option_type == "call" for c in screen.candidates)
    puts = candidates(chain, "bull put", TS, rule, fees).candidates
    assert [(c.short.strike, c.long.strike) for c in puts] == [(60_000, 58_000)]


def test_exit_rules(rule: RoughRule) -> None:
    credit = D("220")
    assert exit_reason(credit, D("110"), rule) == "take profit"  # 50% of the credit
    assert exit_reason(credit, D("110.01"), rule) is None
    assert exit_reason(credit, D("439.99"), rule) is None
    assert exit_reason(credit, D("440"), rule) == "stop loss"  # 2 times the credit


def test_buyback_needs_executable_quotes_covering_the_position(rule: RoughRule) -> None:
    short, long = q(70_000, bid=100, ask=110, size=1.0), q(72_000, bid=20, ask=25, size=1.0)
    assert buyback_price(short, long, D("1"), TS, rule) == (D("90"), "")  # 110 - 20
    price, why = buyback_price(short, long, D("1.5"), TS, rule)
    assert price is None
    assert "size" in why
    price, why = buyback_price(short, replace(long, bid=None), D("1"), TS, rule)
    assert price is None
    assert "no bid" in why
    stale = replace(short, exchange_ts_ms=TS - 120_000)
    assert buyback_price(stale, long, D("1"), TS, rule)[0] is None


def test_thesis_names_the_short_strike_and_the_expiry() -> None:
    assert thesis("bear call", 70_000, E1) == (
        "BTC stays below 70,000 until expiry on 27 Nov 2026 08:00 UTC; buy back at half the credit."
    )
    assert thesis("bull put", 60_000, E1).startswith("BTC stays above 60,000")
