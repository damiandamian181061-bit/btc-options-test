"""What a backtest records, and the hash that proves two runs gave the same result.

The rows match the results tables (migration 0005): ``backtest_orders``, ``backtest_fills``,
``backtest_positions``, ``backtest_equity`` and ``risk_events``. ``result_hash`` is the SHA-256
of every row in a canonical form (Decimals as plain strings, in order), so the determinism test
compares one string.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, fields, is_dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class OrderRecord:
    ticket_id: str
    group_id: str | None
    ts_ms: int
    leg: int
    instrument: str
    direction: str
    intent: str
    requested: Decimal
    filled: Decimal
    status: str  # filled, partial, unfilled or rejected
    reason: str  # the strategy's reason
    detail: str  # why it filled in part, not at all, or was rejected


@dataclass(frozen=True)
class FillRecord:
    fill_id: int
    ticket_id: str | None  # None for settlements
    group_id: str | None
    ts_ms: int
    instrument: str
    kind: str  # trade or settlement
    direction: str  # buy or sell (settlement: the side that closes the position)
    amount: Decimal
    price: Decimal  # settlement: intrinsic value a BTC
    fee: Decimal  # trading or delivery fee
    index_price: Decimal | None
    mid: Decimal | None
    mark: Decimal | None
    delivery_price: Decimal | None = None


@dataclass(frozen=True)
class PositionRecord:
    ts_ms: int
    group_id: str
    instrument: str
    qty: Decimal  # after the change
    entry_value: Decimal


@dataclass(frozen=True)
class EquityRecord:
    ts_ms: int
    equity: Decimal  # at Deribit's mark
    liquidation_equity: Decimal  # longs at the bid, shorts at the ask
    cash: Decimal
    option_value: Decimal
    hedge_unrealised: Decimal
    option_pnl: Decimal  # since the start: realised + unrealised
    hedge_pnl: Decimal
    fees: Decimal
    funding: Decimal  # received (+) or paid (-)
    open_groups: int
    max_loss: Decimal | None  # sum of the open groups' maximum losses (None: unbounded)
    initial_margin: Decimal
    maintenance_margin: Decimal
    delta: Decimal
    vega: Decimal
    stale_marks: int  # open positions valued at an earlier minute's mark


@dataclass(frozen=True)
class GroupRecord:
    group_id: str
    opened_ms: int
    closed_ms: int | None
    thesis: str
    reason: str
    exit_reason: str
    realised: Decimal
    fees: Decimal


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")  # 400.0 and 400.00 hash the same
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def to_row(record: Any) -> dict[str, Any]:
    return asdict(record)


def result_hash(sections: dict[str, list[Any]]) -> str:
    """SHA-256 over every record of every section, in order, in a canonical JSON form."""
    h = hashlib.sha256()
    for name in sorted(sections):
        h.update(name.encode())
        for record in sections[name]:
            h.update(json.dumps(_plain(record), sort_keys=True, separators=(",", ":")).encode())
            h.update(b"\n")
    return h.hexdigest()
