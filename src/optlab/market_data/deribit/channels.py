"""Deribit public channel names, and splitting subscription lists into request-sized batches."""

import msgspec

# Deribit rejects WebSocket frames over 32 kB (error -32600). Stay well under it.
MAX_REQUEST_BYTES = 24_000
MAX_CHANNELS_PER_REQUEST = 200

TICKER_PREFIX = "ticker."
TRADES_PREFIX = "trades."
PRICE_INDEX_PREFIX = "deribit_price_index."

PUBLIC_PREFIXES = (TICKER_PREFIX, TRADES_PREFIX, PRICE_INDEX_PREFIX)


def ticker(instrument_name: str, interval: str = "100ms") -> str:
    return f"{TICKER_PREFIX}{instrument_name}.{interval}"


def trades(kind: str, currency: str, interval: str = "100ms") -> str:
    """All trades of one kind settled in one currency, e.g. ``trades.option.USDC.100ms``."""
    return f"{TRADES_PREFIX}{kind}.{currency}.{interval}"


def price_index(index_name: str) -> str:
    return f"{PRICE_INDEX_PREFIX}{index_name}"


def stream_key(channel: str) -> str | None:
    """The instrument or index a channel carries, for channels this module builds.

    ``ticker.BTC-PERPETUAL.100ms`` gives ``BTC-PERPETUAL``; ``deribit_price_index.btc_usd`` gives
    ``btc_usd``. Trade channels cover many instruments and give None.
    """
    if channel.startswith(TICKER_PREFIX):
        body = channel[len(TICKER_PREFIX) :]
        return body.rsplit(".", 1)[0] if "." in body else None
    if channel.startswith(PRICE_INDEX_PREFIX):
        return channel[len(PRICE_INDEX_PREFIX) :]
    return None


def batches(
    channels: list[str],
    max_bytes: int = MAX_REQUEST_BYTES,
    max_channels: int = MAX_CHANNELS_PER_REQUEST,
) -> list[list[str]]:
    """Split channels so each subscribe request stays under the frame-size and count limits."""
    # Room for the rest of the request: {"jsonrpc":"2.0","id":N,"method":...,"params":...}
    overhead = 100
    out: list[list[str]] = []
    current: list[str] = []
    size = overhead
    for channel in channels:
        item = len(msgspec.json.encode(channel)) + 1
        if overhead + item > max_bytes:
            raise ValueError(f"channel name too long for one request: {channel[:60]}...")
        if current and (size + item > max_bytes or len(current) >= max_channels):
            out.append(current)
            current, size = [], overhead
        current.append(channel)
        size += item
    if current:
        out.append(current)
    return out
