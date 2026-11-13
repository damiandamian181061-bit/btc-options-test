"""Deribit exchange adapter: the only code that sees Deribit's JSON.

Everything outside this package works with the typed records in ``optlab.domain.market``.
Public market data only: there is no API key and no private method anywhere in the adapter.
"""

from optlab.market_data.deribit.client import DeribitClient, SubscriptionChange
from optlab.market_data.deribit.connection import (
    PUBLIC_WS_URL,
    Backoff,
    DeribitError,
    DeribitRpcError,
    NotConnectedError,
)

__all__ = [
    "PUBLIC_WS_URL",
    "Backoff",
    "DeribitClient",
    "DeribitError",
    "DeribitRpcError",
    "NotConnectedError",
    "SubscriptionChange",
]
