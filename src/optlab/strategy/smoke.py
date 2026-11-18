"""Strategies for checking the engine, not for trading.

- ``smoke``: the Phase 4 smoke trade. At the first usable minute it buys ``amount`` BTC of the
  BTC_USDC call nearest the money (strike closest to the option's forward) on the first expiry
  at least ``min_days`` away, and holds it to expiry. Damian checks its numbers by hand.
- ``no_trade``: never trades (Set A's no-trade benchmark is this, in Phase 5).
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from optlab.domain.clock import MS_PER_DAY, ms_to_datetime
from optlab.domain.orders import Order, Ticket
from optlab.simulator.market import MarketState
from optlab.strategy.base import PortfolioView, register


class SmokeTrade:
    name = "smoke"

    def __init__(self, amount: Decimal, min_days: Decimal) -> None:
        self.amount = amount
        self.min_days = min_days
        self.sent = False

    def on_snapshot(self, state: MarketState, portfolio: PortfolioView) -> Sequence[Ticket]:
        if self.sent or portfolio.groups:
            return []
        earliest = state.ts_ms + int(self.min_days * MS_PER_DAY)
        calls = []
        for name in state.options:
            info = state.instruments.get(name)
            if info is None or info.option_type != "call" or info.expiry_ms is None:
                continue
            if info.expiry_ms < earliest:
                continue
            quote = state.options[name]
            if quote.ask is None or quote.underlying_price is None or info.strike is None:
                continue
            calls.append((info.expiry_ms, abs(info.strike - quote.underlying_price), info.strike,
                          name))  # fmt: skip
        if not calls:
            return []
        expiry = min(c[0] for c in calls)
        _, _, strike, name = min(c for c in calls if c[0] == expiry)
        self.sent = True
        thesis = (
            f"smoke trade: hold {name} to expiry on {ms_to_datetime(expiry):%d %b %Y %H:%M} UTC"
            f" (strike {strike}, nearest the forward)"
        )
        order = Order(name, "buy", self.amount)
        return [Ticket((order,), "open", reason="smoke trade", thesis=thesis)]


class NoTrade:
    name = "no_trade"

    def on_snapshot(self, state: MarketState, portfolio: PortfolioView) -> Sequence[Ticket]:
        return []


@register("smoke")
def _smoke(params: Mapping[str, Any], seed: int) -> SmokeTrade:
    return SmokeTrade(Decimal(str(params.get("amount", "1"))),
                      Decimal(str(params.get("min_days", "1"))))  # fmt: skip


@register("no_trade")
def _no_trade(params: Mapping[str, Any], seed: int) -> NoTrade:
    return NoTrade()
