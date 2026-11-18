"""What a strategy sees and returns.

A strategy gets the market at t (``MarketState``, read through ``MarketDataView(as_of=t)``) and a
read-only ``PortfolioView``, and returns tickets. It never sees the books, the risk gateway or
the fill simulator, and nothing it returns is booked without the gateway's approval (CLAUDE.md).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from optlab.domain.orders import Ticket
from optlab.simulator.market import MarketState


@dataclass(frozen=True)
class GroupView:
    group_id: str
    opened_ms: int
    legs: Mapping[str, Decimal]  # signed BTC, open legs only
    entry: Mapping[str, Decimal]  # signed entry value per leg
    thesis: str


@dataclass(frozen=True)
class PortfolioView:
    ts_ms: int
    cash: Decimal
    equity: Decimal  # at mark
    positions: Mapping[str, Decimal]  # signed BTC per instrument
    groups: Sequence[GroupView]  # open groups
    can_open: bool  # false while the kill switch, daily loss limit or a margin shortfall holds


class Strategy(Protocol):
    name: str

    def on_snapshot(self, state: MarketState, portfolio: PortfolioView) -> Sequence[Ticket]: ...


StrategyFactory = Callable[[Mapping[str, Any], int], Strategy]
_REGISTRY: dict[str, StrategyFactory] = {}


def register(name: str) -> Callable[[StrategyFactory], StrategyFactory]:
    def wrap(factory: StrategyFactory) -> StrategyFactory:
        _REGISTRY[name] = factory
        return factory

    return wrap


def make_strategy(name: str, params: Mapping[str, Any], seed: int) -> Strategy:
    # Import the modules that register strategies.
    from optlab.strategy import exercise, smoke  # noqa: F401

    if name not in _REGISTRY:
        raise ValueError(f"unknown strategy {name!r}; known: {sorted(_REGISTRY)}")
    return _REGISTRY[name](params, seed)
