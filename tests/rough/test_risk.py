"""ROUGH (Phase 2B) risk gateway: sizing to 2% of equity, cut to the quoted size, and limits."""

from dataclasses import replace
from decimal import Decimal

import pytest

from optlab.risk.rough import OpenRisk, RoughLimits, approve, floor_to, load_limits
from optlab.simulator.rough_fees import load_fee_schedule
from optlab.strategy.rough import Candidate, candidates, load_rule
from tests.conftest import REPO_ROOT
from tests.rough.test_strategy import E1, E2, TS, calls

D = Decimal


@pytest.fixture
def limits() -> RoughLimits:
    return load_limits(REPO_ROOT / "configs")


def spread(size: float = 10.0) -> Candidate:
    """The 70,000/72,000 bear call: max loss 2,000 - 401.06 = 1,598.94 per BTC."""
    chain = [replace(x, bid_size=size, ask_size=size) for x in calls()]
    screen = candidates(
        chain,
        "bear call",
        TS,
        load_rule(REPO_ROOT / "configs"),
        load_fee_schedule(REPO_ROOT / "configs"),
    )
    return next(c for c in screen.candidates if c.short.strike == 70_000)


def test_limits_come_from_the_config(limits: RoughLimits) -> None:
    assert limits.starting_equity == D("100000")
    assert (limits.max_loss_of_equity, limits.max_total_loss_of_equity) == (D("0.02"), D("0.10"))
    assert (limits.max_open_spreads, limits.max_open_per_expiry) == (5, 2)


def test_floor_to_the_contract_step() -> None:
    assert floor_to(D("1.25082"), D("0.01")) == D("1.25")
    assert floor_to(D("0.009"), D("0.01")) == D("0")
    assert floor_to(D("3"), D("0.1")) == D("3.0")


def test_size_so_the_maximum_loss_is_2_pct_of_equity(limits: RoughLimits) -> None:
    # 2% of 100,000 = 2,000; 2,000 / 1,598.94 = 1.2508 BTC, floored to 1.25.
    # Maximum loss 1.25 * 1,598.94 = 1,998.675 USDC.
    a = approve(spread(), D("100000"), [], limits)
    assert a.approved
    assert a.reason == ""
    assert a.amount == D("1.25")
    assert a.max_loss == D("1998.6750")


def test_size_is_cut_to_the_quoted_size(limits: RoughLimits) -> None:
    a = approve(spread(size=0.4), D("100000"), [], limits)
    assert a.amount == D("0.40")
    tiny = approve(spread(size=0.004), D("100000"), [], limits)
    assert not tiny.approved
    assert "under the minimum 0.01 BTC" in tiny.reason


def test_open_spread_limits(limits: RoughLimits) -> None:
    risk = D("1000")
    five = [OpenRisk(E2, risk)] * 5
    assert "5 spreads open" in approve(spread(), D("100000"), five, limits).reason
    two_here = [OpenRisk(E1, risk), OpenRisk(E1, risk)]
    assert (
        "2 spreads already open on 2026-11-27"
        in approve(spread(), D("100000"), two_here, limits).reason
    )
    assert approve(spread(), D("100000"), [OpenRisk(E1, risk), OpenRisk(E2, risk)], limits).approved


def test_total_maximum_loss_is_at_most_10_pct_of_equity(limits: RoughLimits) -> None:
    # 8,500 open + 1,998.675 = 10,498.675 > 10,000.
    open_ = [OpenRisk(E2, D("4250")), OpenRisk(E2, D("4250"))]
    a = approve(spread(), D("100000"), open_, limits)
    assert not a.approved
    assert "10% of equity" in a.reason
    assert approve(spread(), D("100000"), [OpenRisk(E2, D("8000"))], limits).approved
