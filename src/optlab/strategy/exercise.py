"""``exercise``: random trades that put the engine through its paces. Its results mean nothing.

The Phase 4 gate runs determinism, leakage and reconciliation on a real week with this strategy,
because the smoke trade alone books one fill and one settlement. Each minute, with small seeded
probabilities, it opens a credit spread (bull put or bear call, neighbouring strikes of one
BTC_USDC expiry at least ``min_days`` away, both legs quoted), opens or closes a BTC_USDC
perpetual hedge, or closes an open group at the quotes of the minute. Its choices depend only on
the seed and on what is known at t, so a leak would show as a different decision.
"""

import random
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from optlab.domain.clock import MS_PER_DAY
from optlab.domain.orders import Direction, Order, Ticket
from optlab.simulator.market import MarketState
from optlab.strategy.base import PortfolioView, register

PERP = "BTC_USDC-PERPETUAL"


class Exercise:
    name = "exercise"

    def __init__(self, seed: int, p_open: float, p_close: float, p_hedge: float,
                 min_days: float) -> None:  # fmt: skip
        self.rng = random.Random(seed)  # noqa: S311  (seeded and repeatable; not security)
        self.p_open, self.p_close, self.p_hedge = p_open, p_close, p_hedge
        self.min_days = min_days

    def on_snapshot(self, state: MarketState, portfolio: PortfolioView) -> Sequence[Ticket]:
        roll = self.rng.random()
        if roll < self.p_open:
            return self._spread(state)
        roll -= self.p_open
        if roll < self.p_hedge:
            return self._hedge(state, portfolio)
        roll -= self.p_hedge
        if roll < self.p_close and portfolio.groups:
            g = self.rng.choice(list(portfolio.groups))
            legs = tuple(
                Order(n, "buy" if q < 0 else "sell", abs(q)) for n, q in sorted(g.legs.items())
            )
            if len({o.amount for o in legs}) == 1:
                return [Ticket(legs, "close", group_id=g.group_id, reason="random exit")]
        return []

    def _spread(self, state: MarketState) -> list[Ticket]:
        kind = self.rng.choice(["put", "call"])
        earliest = state.ts_ms + int(self.min_days * MS_PER_DAY)
        by_expiry: dict[int, list[tuple[Decimal, str]]] = {}
        for name in sorted(state.options):
            info = state.instruments.get(name)
            q = state.options[name]
            if info is None or info.option_type != kind or info.expiry_ms is None:
                continue
            if info.expiry_ms < earliest or info.strike is None:
                continue
            if q.bid is None or q.ask is None or q.bid_size <= 0 or q.ask_size <= 0:
                continue
            by_expiry.setdefault(info.expiry_ms, []).append((info.strike, name))
        choices = [sorted(v) for _, v in sorted(by_expiry.items()) if len(v) >= 2]
        if not choices:
            return []
        strikes = self.rng.choice(choices)
        i = self.rng.randrange(len(strikes) - 1)
        (lo, lo_name), (hi, hi_name) = strikes[i], strikes[i + 1]
        short, long_ = (hi_name, lo_name) if kind == "put" else (lo_name, hi_name)
        amount = Decimal(self.rng.randint(1, 50)) / Decimal(100)
        side = "bull put" if kind == "put" else "bear call"
        return [Ticket((Order(short, "sell", amount), Order(long_, "buy", amount)), "open",
                       reason="random entry", thesis=f"{side} {lo}/{hi} (exercise)")]  # fmt: skip

    def _hedge(self, state: MarketState, portfolio: PortfolioView) -> list[Ticket]:
        if PERP not in state.underlyings:
            return []
        amount = Decimal(self.rng.randint(1, 200)) / Decimal(1000)
        direction: Direction = "buy" if self.rng.random() < 0.5 else "sell"
        return [Ticket((Order(PERP, direction, amount),), "open", reason="random hedge",
                       thesis="perpetual (exercise)")]  # fmt: skip


@register("exercise")
def _exercise(params: Mapping[str, Any], seed: int) -> Exercise:
    return Exercise(
        seed,
        float(params.get("p_open", 0.004)),
        float(params.get("p_close", 0.004)),
        float(params.get("p_hedge", 0.002)),
        float(params.get("min_days", 1.0)),
    )
