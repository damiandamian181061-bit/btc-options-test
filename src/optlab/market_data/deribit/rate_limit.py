"""Token-bucket rate limits for requests to Deribit.

Deribit limits unauthenticated requests per IP address, and the per-IP numbers are not
published. These buckets are shared by every connection and sit below the documented
per-account limits (docs.deribit.com, "Rate limits", checked 12 Nov 2026):

- most public methods: burst 100, 20 per second. We use burst 50, 10 per second.
- ``public/subscribe`` and ``public/unsubscribe``: burst 10, about 3.3 per second. We use burst 5,
  2 per second.
- ``public/get_instruments``: the method page says 1 per second, burst 50; a July 2025 changelog
  says 1 per 10 seconds, burst 5. We use the stricter one.

Going over a limit makes Deribit reply ``too_many_requests`` (10028) and close the session.
"""

from dataclasses import dataclass, field

from optlab.domain.clock import Clock, SystemClock


class TokenBucket:
    """``capacity`` tokens, refilled continuously at ``refill_per_s``. ``acquire`` waits."""

    def __init__(self, capacity: float, refill_per_s: float, clock: Clock | None = None) -> None:
        if capacity < 1 or refill_per_s <= 0:
            raise ValueError("capacity must be at least 1 and the refill rate positive")
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self._clock = clock or SystemClock()
        self._tokens = capacity
        self._updated = self._clock.monotonic()

    def _refill(self) -> None:
        now = self._clock.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.refill_per_s)
        self._updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    async def acquire(self, tokens: float = 1.0) -> None:
        if tokens > self.capacity:
            raise ValueError("cannot take more tokens than the bucket holds")
        while not self.try_acquire(tokens):
            await self._clock.sleep((tokens - self._tokens) / self.refill_per_s)


@dataclass
class RateLimits:
    """One bucket per method family, shared by all connections."""

    clock: Clock = field(default_factory=SystemClock)
    default: TokenBucket = field(init=False)
    subscribe: TokenBucket = field(init=False)
    get_instruments: TokenBucket = field(init=False)

    def __post_init__(self) -> None:
        self.default = TokenBucket(50, 10.0, self.clock)
        self.subscribe = TokenBucket(5, 2.0, self.clock)
        self.get_instruments = TokenBucket(5, 0.1, self.clock)

    def bucket_for(self, method: str) -> TokenBucket:
        if method in ("public/subscribe", "public/unsubscribe"):
            return self.subscribe
        if method == "public/get_instruments":
            return self.get_instruments
        return self.default

    async def acquire(self, method: str) -> None:
        await self.bucket_for(method).acquire()
