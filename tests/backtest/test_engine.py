"""The backtest engine on a small made-up market (tests/backtest/market.py).

The four Phase 4 gate tests run here on made-up data and again on a real recorded week
(research/phase4_gate.py): determinism, leakage, reconciliation and the smoke trade.
"""

import itertools
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from optlab.domain.clock import MS_PER_HOUR, MS_PER_MINUTE
from optlab.domain.orders import Order, Ticket
from optlab.risk.limits import RiskConfig, load_risk_config
from optlab.simulator.config import BacktestConfig, StrategyConfig
from optlab.simulator.engine import Backtest, BacktestResult
from optlab.simulator.fees import load_fees
from optlab.simulator.market import MarketState
from optlab.strategy.base import PortfolioView, Strategy
from tests.backtest.market import (
    END,
    EXPIRY,
    PERP,
    START,
    STRIKES,
    FakeMarket,
    MemorySource,
    option_name,
)
from tests.conftest import REPO_ROOT

D = Decimal
CONFIGS = REPO_ROOT / "configs"


def config(name: str = "test", **kw: Any) -> BacktestConfig:
    base: dict[str, Any] = {
        "name": name,
        "start": datetime(2026, 11, 19, tzinfo=UTC),
        "end": datetime(2026, 11, 20, 9, tzinfo=UTC),
        "strategy": StrategyConfig(name="smoke", params={"amount": "0.1"}),
        "seed": 7,
    }
    base.update(kw)
    return BacktestConfig(**base)


def risk(**updates: Any) -> RiskConfig:
    return load_risk_config(CONFIGS, "v1").model_copy(update=updates)


def run(
    source: MemorySource,
    strategy: Strategy | None = None,
    cfg: BacktestConfig | None = None,
    risk_config: RiskConfig | None = None,
) -> BacktestResult:
    bt = Backtest(cfg or config(), source, strategy=strategy, risk=risk_config,
                  config_dir=CONFIGS)  # fmt: skip
    return bt.run()


def linear_spot(start: float, end: float) -> Callable[[int], float]:
    def spot(ts: int) -> float:
        return start + (end - start) * (ts - START) / (EXPIRY - START)

    return spot


# ---------------------------------------------------------------------------- strategies


@dataclass
class Scripted:
    """Sends the given tickets at the given minutes."""

    script: dict[int, list[Ticket]]
    name: str = "scripted"
    seen: list[int] = field(default_factory=list)

    def on_snapshot(self, state: MarketState, portfolio: PortfolioView) -> Sequence[Ticket]:
        self.seen.append(state.ts_ms)
        return self.script.get(state.ts_ms, [])


class RandomTrader:
    """Opens and closes random spreads; its choices depend on the seed and on prices at t."""

    name = "random"

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)

    def on_snapshot(self, state: MarketState, portfolio: PortfolioView) -> Sequence[Ticket]:
        out: list[Ticket] = []
        roll = self.rng.random()
        if roll < 0.08 and state.index is not None:
            kind = "put" if state.index > D("90000") else "call"
            i = self.rng.randrange(len(STRIKES) - 1)
            lo, hi = STRIKES[i], STRIKES[i + 1]
            short, long_ = (hi, lo) if kind == "put" else (lo, hi)
            amount = D(self.rng.randint(1, 120)) / D(100)
            out.append(Ticket(
                (Order(option_name(short, kind), "sell", amount),
                 Order(option_name(long_, kind), "buy", amount)),
                "open", reason="random", thesis=f"{kind} spread",
            ))  # fmt: skip
        elif roll < 0.14 and portfolio.groups:
            g = self.rng.choice(list(portfolio.groups))
            legs = tuple(
                Order(n, "buy" if q < 0 else "sell", abs(q)) for n, q in sorted(g.legs.items())
            )
            if len({o.amount for o in legs}) == 1:
                out.append(Ticket(legs, "close", group_id=g.group_id, reason="random exit"))
        return out


# ---------------------------------------------------------------------------- smoke trade


def test_smoke_trade_by_hand() -> None:
    """One ATM call bought at the ask, held to expiry, settled through the future."""
    market = FakeMarket(spot=linear_spot(89_600.0, 91_000.0))
    result = run(market.build())
    fees = load_fees(CONFIGS)
    buy = result.fills[0]
    assert (buy.instrument, buy.kind, buy.direction) == (
        option_name(90_000, "call"),
        "trade",
        "buy",
    )
    assert buy.ts_ms == START
    # Mark 600 x sqrt(32/24) = 692.8; ask = mark + 10 = 702.8; index 89,600; 0.1 BTC.
    assert (buy.price, buy.amount, buy.index_price) == (D("702.8"), D("0.1"), D("89600.0"))
    # Fee min(0.0003 x 89,600 = 26.88, 0.125 x 702.8 = 87.85) x 0.1 = 2.688.
    assert buy.fee == D("2.688")
    sett = result.fills[1]
    assert (sett.kind, sett.direction, sett.ts_ms) == ("settlement", "sell", EXPIRY + MS_PER_MINUTE)
    # Delivery 91,000: intrinsic 1,000; fee min(0.00015 x 91,000 = 13.65, 125) x 0.1 = 1.365.
    assert (sett.delivery_price, sett.price, sett.fee) == (D("91000.00"), D("1000.00"), D("1.365"))
    final = result.final
    assert final is not None
    expected_cash = D("100000") - D("70.28") - D("2.688") + D("100.000") - D("1.365")
    assert final.cash == final.equity == expected_cash == D("100025.667")
    assert final.option_pnl == D("100.000") - D("70.28")
    assert final.fees == D("2.688") + D("1.365")
    assert fees.option_delivery_fee(D("1000"), D("91000"), D("0.1"), EXPIRY) == D("1.365")
    kinds = [e.kind for e in result.journal.entries]
    assert kinds == ["capital", "open", "fee", "exercise", "delivery", "delivery fee"]
    (group,) = result.groups
    assert group.exit_reason == "settled at expiry"
    assert group.realised == D("100.000") - D("70.28")


def test_one_btc_of_an_atm_call_breaks_the_delta_limit() -> None:
    """0.5 delta x 1 BTC x 89,600 = about 45% of equity, above the 20% limit: refused."""
    cfg = config(strategy=StrategyConfig(name="smoke", params={"amount": "1"}))
    result = run(FakeMarket(step_minutes=60).build(), cfg=cfg)
    assert [o.status for o in result.orders] == ["rejected"]
    assert [e.rule for e in result.risk_events] == ["portfolio delta"]


def test_settlement_waits_for_the_delivery_price() -> None:
    market = FakeMarket(spot=linear_spot(89_600.0, 91_000.0),
                        delivery_recorded=EXPIRY + 3 * MS_PER_MINUTE)  # fmt: skip
    result = run(market.build())
    at = {r.ts_ms: r for r in result.equity}
    # 08:00 to 08:02: the option has no quote any more; it is held at its last mark.
    for m in range(3):
        row = at[EXPIRY + m * MS_PER_MINUTE]
        assert (row.open_groups, row.stale_marks) == (1, 1)
    settled = at[EXPIRY + 3 * MS_PER_MINUTE]
    assert (settled.open_groups, settled.stale_marks) == (0, 0)
    assert result.fills[-1].ts_ms == EXPIRY + 3 * MS_PER_MINUTE


# ---------------------------------------------------------------------------- gate tests


def random_run(source: MemorySource, seed: int = 11) -> BacktestResult:
    cfg = config(strategy=StrategyConfig(name="random"), seed=seed)
    return run(
        source, RandomTrader(seed), cfg, risk(max_open_per_expiry=None, max_open_groups=None)
    )


def test_determinism_same_config_same_hash() -> None:
    source = FakeMarket(step_minutes=5).build()
    a, b = random_run(source), random_run(source)
    assert len(a.fills) > 10  # it really traded
    assert a.result_hash == b.result_hash
    assert random_run(source, seed=12).result_hash != a.result_hash


def test_leakage_changing_data_after_t_leaves_everything_at_t_unchanged() -> None:
    cut = START + 13 * MS_PER_HOUR

    def scramble(table: str, row: dict[str, Any]) -> dict[str, Any] | None:
        if row["ts_ms"] <= cut:
            return row
        if row["ts_ms"] % (15 * MS_PER_MINUTE) == 0:
            return None  # drop some later minutes altogether
        out = dict(row)
        for k in ("bid", "ask", "mark_price", "index_price", "underlying_price"):
            if out.get(k) is not None:
                out[k] = out[k] * 1.07
        return out

    base = random_run(FakeMarket(step_minutes=5).build())
    other = random_run(FakeMarket(step_minutes=5, edit=scramble).build())

    def upto(result: BacktestResult) -> dict[str, list[Any]]:
        return {
            "orders": [r for r in result.orders if r.ts_ms <= cut],
            "fills": [r for r in result.fills if r.ts_ms <= cut],
            "positions": [r for r in result.positions if r.ts_ms <= cut],
            "equity": [r for r in result.equity if r.ts_ms <= cut],
            "events": [r for r in result.risk_events if r.ts_ms <= cut],
        }

    assert upto(base)["fills"]  # decisions before the cut exist
    assert upto(base) == upto(other)
    assert base.result_hash != other.result_hash  # and the later data did matter


def test_reconciliation_at_every_step() -> None:
    result = random_run(FakeMarket(step_minutes=5).build())
    rows = result.equity
    assert len(rows) > 300
    for prev, now in itertools.pairwise(rows):
        assert now.equity - prev.equity == (
            (now.option_pnl - prev.option_pnl)
            + (now.hedge_pnl - prev.hedge_pnl)
            - (now.fees - prev.fees)
            + (now.funding - prev.funding)
        )
    assert result.journal.trial_balance() == {"USDC": 0}


# ---------------------------------------------------------------------------- fills


def test_a_ticket_larger_than_the_quoted_size_fills_in_part() -> None:
    k = option_name(90_000, "call")
    strategy = Scripted({START: [Ticket((Order(k, "buy", D("1")),), "open")]})
    result = run(FakeMarket(size=0.37, step_minutes=60).build(), strategy)
    (order,) = result.orders
    assert (order.status, order.requested, order.filled) == ("partial", D("1"), D("0.37"))
    assert "quoted size" in order.detail
    assert result.fills[0].amount == D("0.37")


def test_spread_legs_fill_the_same_amount_each_at_its_own_price() -> None:
    short, long_ = option_name(90_000, "put"), option_name(88_000, "put")
    t = Ticket((Order(short, "sell", D("0.5")), Order(long_, "buy", D("0.5"))), "open")
    result = run(FakeMarket(step_minutes=60).build(), Scripted({START: [t]}))
    sell, buy = result.fills[:2]
    assert sell.mark is not None
    assert buy.mark is not None
    assert sell.price == sell.mark - 10  # the bid
    assert buy.price == buy.mark + 10  # the ask
    assert sell.amount == buy.amount == D("0.5")


# ---------------------------------------------------------------------------- the gateway


def test_limits_reject_and_log_risk_events() -> None:
    call = option_name(90_000, "call")
    spread = (Order(option_name(90_000, "put"), "sell", D("0.1")),
              Order(option_name(88_000, "put"), "buy", D("0.1")))  # fmt: skip
    script = {
        START: [Ticket(spread, "open", reason="first")],
        START + MS_PER_HOUR: [
            Ticket(spread, "open", reason="second"),  # a second group: max_open_groups 1
            Ticket((Order(call, "sell", D("0.1")),), "open", reason="naked"),  # unbounded
        ],
    }
    result = run(FakeMarket(step_minutes=60).build(), Scripted(script), None,
                 risk(max_open_groups=1))  # fmt: skip
    status = [(o.reason, o.status) for o in result.orders if o.leg == 0]
    assert status == [("first", "filled"), ("second", "rejected"), ("naked", "rejected")]
    rules = {(e.rule, e.action) for e in result.risk_events}
    assert ("open groups", "rejected") in rules
    assert ("max loss per group", "rejected") in rules
    assert all(e.group_id for e in result.risk_events if e.action == "rejected")


def test_initial_margin_rejects_what_the_balance_cannot_carry() -> None:
    spread = (Order(option_name(90_000, "put"), "sell", D("1")),
              Order(option_name(88_000, "put"), "buy", D("1")))  # fmt: skip
    small = risk(starting_equity=D("5000"), max_loss_of_equity=None,
                 max_total_loss_of_equity=None, max_delta_of_equity=None,
                 max_vega_of_equity=None)  # fmt: skip
    result = run(FakeMarket(step_minutes=60).build(),
                 Scripted({START: [Ticket(spread, "open")]}), None, small)  # fmt: skip
    assert [o.status for o in result.orders] == ["rejected", "rejected"]
    (event,) = result.risk_events
    assert event.rule == "initial margin"
    assert event.value is not None
    assert event.value > D("9000")  # above 0.10 x 90,000 + the mark


def test_a_close_must_reduce_its_own_group() -> None:
    k = option_name(90_000, "call")
    script = {
        START: [Ticket((Order(k, "buy", D("0.2")),), "open")],
        START + MS_PER_HOUR: [Ticket((Order(k, "buy", D("0.2")),), "close", group_id="g1")],
    }
    result = run(FakeMarket(step_minutes=60).build(), Scripted(script))
    assert [o.status for o in result.orders] == ["filled", "rejected"]
    assert result.risk_events[-1].rule == "close"


def test_the_daily_loss_limit_stops_new_trades_until_the_next_utc_day() -> None:
    k = option_name(90_000, "call")
    buy = Ticket((Order(k, "buy", D("0.1")),), "open")
    # Each buy loses its spread and fee at once (about 0.004% of equity). The third comes at
    # 00:00 on the next UTC day, before any loss that day.
    script = {START: [buy], START + MS_PER_HOUR: [buy], START + 24 * MS_PER_HOUR: [buy]}
    tight = risk(daily_loss_limit=D("0.00002"), max_open_groups=None, max_open_per_expiry=None)
    result = run(FakeMarket(step_minutes=60, spot=lambda t: 90_000.0).build(),
                 Scripted(script), None, tight)  # fmt: skip
    assert [o.status for o in result.orders] == ["filled", "rejected", "filled"]
    triggered = [e for e in result.risk_events if e.action == "triggered"]
    assert triggered[0].rule == "daily loss limit"


# ---------------------------------------------------------------------------- funding


def test_perpetual_funding_follows_deribits_formula() -> None:
    # Constant index 90,000, perpetual mark 0.05% above it: rate 0.05% - 0.025% = 0.025% per
    # 8 hours. A 1 BTC long pays 0.00025 x 90,000 = 22.5 USDC every 8 hours.
    buy = Ticket((Order(PERP, "buy", D("1")),), "open", reason="hedge")
    open_ = risk(max_loss_of_equity=None, max_total_loss_of_equity=None, max_delta_of_equity=None)
    cfg = config(end=datetime(2026, 11, 19, 8, 1, tzinfo=UTC))
    result = run(FakeMarket(spot=lambda t: 90_000.0).build(), Scripted({START: [buy]}), cfg,
                 open_)  # fmt: skip
    final = result.final
    assert final is not None
    assert final.funding == D("-22.5")  # 8 hours of funding paid
    fill = result.fills[0]
    assert (fill.price, fill.fee) == (D("90050.0"), D("0.00035") * D("90050.0"))
    assert final.hedge_pnl == D("1") * D("90045.0") - D("90050.0")  # marked at mark


# ---------------------------------------------------------------------------- skipped minutes


def test_no_decisions_in_minutes_the_data_checks_skip() -> None:
    gap = (START + 2 * MS_PER_HOUR, START + 3 * MS_PER_HOUR)
    strategy = Scripted({})
    result = run(FakeMarket(step_minutes=10, skips=[gap]).build(), strategy)
    assert not [t for t in strategy.seen if gap[0] <= t < gap[1]]
    # ...but those minutes are still valued and recorded.
    assert [r for r in result.equity if gap[0] <= r.ts_ms < gap[1]]
    assert result.minutes == len(range(START, END, 10 * MS_PER_MINUTE))
