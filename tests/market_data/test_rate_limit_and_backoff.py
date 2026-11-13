"""Token buckets wait exactly as long as needed; reconnect delays grow, cap and jitter."""

import random

import msgspec
import pytest

from optlab.market_data.deribit.channels import (
    MAX_CHANNELS_PER_REQUEST,
    MAX_REQUEST_BYTES,
    batches,
    stream_key,
    ticker,
    trades,
)
from optlab.market_data.deribit.connection import Backoff
from optlab.market_data.deribit.rate_limit import RateLimits, TokenBucket
from tests.market_data.helpers import ManualClock


def test_bucket_allows_a_burst_then_refills() -> None:
    clock = ManualClock()
    bucket = TokenBucket(capacity=3, refill_per_s=2.0, clock=clock)
    assert [bucket.try_acquire() for _ in range(4)] == [True, True, True, False]
    clock.advance(0.5)  # one token back
    assert bucket.try_acquire()
    assert not bucket.try_acquire()
    clock.advance(10)  # never more than capacity
    assert [bucket.try_acquire() for _ in range(4)] == [True, True, True, False]


async def test_acquire_waits_for_the_missing_tokens() -> None:
    clock = ManualClock()
    bucket = TokenBucket(capacity=2, refill_per_s=4.0, clock=clock)
    start = clock.monotonic()
    for _ in range(4):
        await bucket.acquire()
    # two from the burst, then two more at 4 per second
    assert clock.monotonic() - start == pytest.approx(0.5)


def test_bucket_rejects_bad_settings() -> None:
    with pytest.raises(ValueError, match="capacity"):
        TokenBucket(capacity=0, refill_per_s=1)
    with pytest.raises(ValueError, match="refill"):
        TokenBucket(capacity=1, refill_per_s=0)


def test_methods_map_to_their_buckets() -> None:
    limits = RateLimits(ManualClock())
    assert limits.bucket_for("public/subscribe") is limits.subscribe
    assert limits.bucket_for("public/unsubscribe") is limits.subscribe
    assert limits.bucket_for("public/get_instruments") is limits.get_instruments
    assert limits.bucket_for("public/test") is limits.default
    # stricter than Deribit's documented limits
    assert limits.subscribe.refill_per_s < 3.3
    assert limits.get_instruments.refill_per_s <= 0.1
    assert limits.default.refill_per_s < 20


def test_backoff_grows_exponentially_and_caps() -> None:
    backoff = Backoff(initial_s=1, factor=2, max_s=60, jitter=0.0)
    rng = random.Random(1)
    assert [backoff.delay(a, rng) for a in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert backoff.delay(1000, rng) == 60


def test_backoff_jitter_stays_in_range() -> None:
    backoff = Backoff(initial_s=10, factor=2, max_s=60, jitter=0.2)
    rng = random.Random(7)
    delays = [backoff.delay(0, rng) for _ in range(500)]
    assert min(delays) >= 8.0
    assert max(delays) <= 12.0
    assert max(delays) - min(delays) > 2.0


def test_subscribe_batches_fit_deribits_frame_limit() -> None:
    channels = [ticker(f"BTC_USDC-27NOV26-{50_000 + 500 * i}-C") for i in range(2000)]
    groups = batches(channels)
    assert [c for g in groups for c in g] == channels
    for group in groups:
        assert len(group) <= MAX_CHANNELS_PER_REQUEST
        request = {"jsonrpc": "2.0", "id": 10**9, "method": "public/subscribe"}
        request["params"] = {"channels": group}
        assert len(msgspec.json.encode(request)) < MAX_REQUEST_BYTES < 32 * 1024


def test_batches_split_on_bytes_too() -> None:
    long = [f"ticker.{'X' * 990}-{i}.100ms" for i in range(60)]
    groups = batches(long, max_bytes=10_000)
    assert len(groups) > 1
    assert all(len(msgspec.json.encode(g)) < 10_000 for g in groups)


def test_stream_keys() -> None:
    assert stream_key("ticker.BTC-PERPETUAL.100ms") == "BTC-PERPETUAL"
    assert stream_key("ticker.BTC_USDC-20NOV26-70000-C.agg2") == "BTC_USDC-20NOV26-70000-C"
    assert stream_key("deribit_price_index.btc_usdc") == "btc_usdc"
    assert stream_key(trades("option", "USDC")) is None
