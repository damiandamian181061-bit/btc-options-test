"""Time source shared by live services and tests.

Every timestamp in the platform is an integer count of UTC milliseconds since the Unix epoch.
Live code uses ``SystemClock``; tests pass a clock they control.
"""

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol

MS_PER_SECOND = 1_000
MS_PER_MINUTE = 60_000
MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000


class Clock(Protocol):
    """Wall-clock time in UTC milliseconds, a monotonic timer and an async sleep."""

    def now_ms(self) -> int: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock."""

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def floor_ms(ts_ms: int, step_ms: int) -> int:
    """Round ``ts_ms`` down to a multiple of ``step_ms`` (both in milliseconds)."""
    return ts_ms - ts_ms % step_ms


def ms_to_datetime(ts_ms: int) -> datetime:
    """UTC-aware datetime for a millisecond timestamp."""
    return datetime.fromtimestamp(ts_ms / MS_PER_SECOND, tz=UTC)


def datetime_to_ms(dt: datetime) -> int:
    """Millisecond timestamp for an aware datetime."""
    if dt.tzinfo is None:
        raise ValueError("naive datetimes are not allowed: timestamps are UTC")
    return round(dt.timestamp() * MS_PER_SECOND)
