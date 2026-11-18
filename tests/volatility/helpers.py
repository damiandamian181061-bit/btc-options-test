"""Synthetic option chains for the surface tests: made-up quotes priced by Black-76 on a known
smile, so every fit and forward has an exact answer to come back to (no vendor data)."""

from collections.abc import Callable, Sequence

import numpy as np
import pandas as pd

from optlab.domain.clock import MS_PER_DAY
from optlab.domain.market import Book
from optlab.pricing import black76 as b76

T0 = 1_795_000_000_000 - 1_795_000_000_000 % 60_000  # a snapshot minute (Nov 2026)
YEAR_MS = 365 * MS_PER_DAY


def skew_smile(atm: float = 0.45, skew: float = -0.15, curve: float = 0.6) -> Callable[..., float]:
    """IV as a function of log-moneyness k and tau: a smooth, arbitrage-free-looking smile."""

    def iv(k: float, tau: float) -> float:
        x = k / np.sqrt(max(tau, 1e-6))
        return float(atm + skew * x * 0.1 + curve * (x * 0.1) ** 2)

    return iv


def make_chain(
    *,
    book: Book = Book.LINEAR,
    forward: float = 65_000.0,
    days: Sequence[float] = (2.0, 9.0, 37.0),
    forwards: Sequence[float] | None = None,
    smile: Callable[..., float] | None = None,
    half_spread: float = 0.01,
    strikes_per_side: int = 12,
    strike_step: float = 1_000.0,
    ts_ms: int = T0,
) -> pd.DataFrame:
    """An option chain in the shape ``optlab.volatility.chain.option_chain`` returns."""
    iv_of = smile or skew_smile()
    rows = []
    for i, d in enumerate(days):
        f = forwards[i] if forwards is not None else forward
        expiry = ts_ms + round(d * MS_PER_DAY)
        tau = (expiry - ts_ms) / YEAR_MS
        centre = round(f / strike_step) * strike_step
        for j in range(-strikes_per_side, strikes_per_side + 1):
            k = centre + j * strike_step
            sigma = iv_of(np.log(k / f), tau)
            for call in (True, False):
                g = b76.greeks(f, k, tau, sigma, call)
                usd = float(g.price)
                premium = usd / f if book == Book.INVERSE else usd
                tick = 0.0001 if book == Book.INVERSE else 5.0
                bid = premium * (1 - half_spread)
                ask = premium * (1 + half_spread)
                rows.append(
                    {
                        "instrument_name": f"X-{d:g}D-{k:.0f}-{'C' if call else 'P'}",
                        "expiry_ms": expiry,
                        "strike": k,
                        "call": call,
                        "bid": bid if bid > 0 else None,
                        "ask": ask if ask > 0 else None,
                        "bid_size": 10.0,
                        "ask_size": 10.0,
                        "mark_price": premium,
                        "mark_iv": sigma,
                        "delta": float(g.delta),
                        "gamma": float(g.gamma),
                        "vega": float(g.vega),
                        "theta": float(g.theta),
                        "underlying_price": f,
                        "index_price": forward,
                        "open_interest": 1.0,
                        "state": "open",
                        "tick_size": tick,
                        "exchange_ts_ms": ts_ms - 1_000,
                    }
                )
    return pd.DataFrame(rows)


def make_futures(chain: pd.DataFrame, *, book: Book = Book.LINEAR) -> pd.DataFrame:
    """A dated future per expiry, marked at the chain's forward."""
    prefix = "BTC_USDC" if book == Book.LINEAR else "BTC"
    out = (
        chain.groupby("expiry_ms", as_index=False)["underlying_price"]
        .first()
        .rename(columns={"underlying_price": "mark_price"})
    )
    out["instrument_name"] = [f"{prefix}-F{i}" for i in range(len(out))]
    out["bid"] = out["mark_price"] - 2.5
    out["ask"] = out["mark_price"] + 2.5
    return out[["instrument_name", "expiry_ms", "mark_price", "bid", "ask"]]
