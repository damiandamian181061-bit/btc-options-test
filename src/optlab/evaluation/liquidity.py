"""Liquidity of the two option books for the spreads the v1 rules would trade (Phase 2).

At each sampled snapshot minute, read through ``MarketDataView``, this builds the candidate
spreads of each book from the v1 entry rules (docs/brief.md, "Trading rules v1"):

- expiry 7 to 30 days away;
- short leg: a put with delta -0.30 to -0.15 (bull put) or a call with delta 0.15 to 0.30
  (bear call), using Deribit's own delta as recorded;
- long leg: the nearest further-out strike of the same expiry and type whose distance from
  the short strike is 2% to 6% of the forward (the short leg's ``underlying_price``).

Then each spread is checked against the v1 liquidity rule: both legs two-sided, each leg's
bid-ask spread at most 10% of its mid, quotes under 2 minutes old, and top-of-book size at
least the order size (the short leg's bid size and the long leg's ask size, since we sell at
the bid and buy at the ask). The order size is what the sizing rule allows: maximum loss
(width minus credit) at most 2% of 100,000 USDC equity. Inverse premiums (BTC) are turned into
USD at the index price; USDC is counted as USD. The credit here is before fees.

This module only measures; choosing the book is Damian's call, informed by the report.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

import pandas as pd

from optlab.domain.clock import MS_PER_DAY
from optlab.market_data.view import MarketDataView

DTE_MIN, DTE_MAX = 7.0, 30.0
PUT_DELTA = (-0.30, -0.15)
CALL_DELTA = (0.15, 0.30)
WIDTH_MIN, WIDTH_MAX = 0.02, 0.06
MAX_SPREAD_OF_MID = 0.10
MAX_QUOTE_AGE_MS = 120_000
EQUITY_USD = 100_000.0
MAX_LOSS_SHARE = 0.02
MIN_CREDIT_OF_WIDTH = 0.20
BOOKS = ("linear", "inverse")
BOOK_NAMES = {"linear": "BTC_USDC (linear)", "inverse": "BTC (inverse)"}

_LEG_COLUMNS = (
    "instrument_name",
    "bid",
    "ask",
    "bid_size",
    "ask_size",
    "delta",
    "underlying_price",
    "index_price",
    "exchange_ts_ms",
)


def snapshot_frame(view: MarketDataView) -> pd.DataFrame:
    """The option chain at the view's time with each option's book, type, strike and expiry."""
    chain = view.option_chain().to_pandas()
    if chain.empty:
        return chain
    instruments = view.instruments().to_pandas()[
        ["instrument_name", "book", "option_type", "strike", "expiry_ms", "min_trade_amount"]
    ]
    frame = chain.merge(instruments, on="instrument_name", how="inner")
    frame["dte"] = (frame["expiry_ms"] - frame["ts_ms"]) / MS_PER_DAY
    return frame


def _leg_quality(frame: pd.DataFrame, prefix: str, ts_ms: pd.Series) -> pd.DataFrame:
    bid, ask = frame[f"{prefix}bid"], frame[f"{prefix}ask"]
    two_sided = bid.notna() & ask.notna() & (bid > 0) & (ask > 0)
    mid = (bid + ask) / 2
    frame[f"{prefix}two_sided"] = two_sided
    frame[f"{prefix}spread_of_mid"] = ((ask - bid) / mid).where(two_sided)
    frame[f"{prefix}fresh"] = (ts_ms - frame[f"{prefix}exchange_ts_ms"]) < MAX_QUOTE_AGE_MS
    return frame


def candidate_spreads(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per candidate spread (short leg with its nearest valid long leg), with checks."""
    if frame.empty:
        return pd.DataFrame()
    window = frame[(frame["dte"] >= DTE_MIN) & (frame["dte"] <= DTE_MAX)]
    out: list[pd.DataFrame] = []
    for option_type, (lo, hi), side in (
        ("put", PUT_DELTA, "bull put"),
        ("call", CALL_DELTA, "bear call"),
    ):
        legs = window[window["option_type"] == option_type]
        shorts = legs[(legs["delta"] >= lo) & (legs["delta"] <= hi)]
        if shorts.empty:
            continue
        keys = ["ts_ms", "book", "expiry_ms"]
        pairs = shorts.merge(
            legs[[*keys, "strike", *_LEG_COLUMNS]], on=keys, suffixes=("", "_long")
        )
        further = (
            pairs["strike_long"] < pairs["strike"]
            if option_type == "put"
            else pairs["strike_long"] > pairs["strike"]
        )
        pairs = pairs[further].copy()
        pairs["width"] = (pairs["strike"] - pairs["strike_long"]).abs()
        pairs["width_of_forward"] = pairs["width"] / pairs["underlying_price"]
        pairs = pairs[
            (pairs["width_of_forward"] >= WIDTH_MIN) & (pairs["width_of_forward"] <= WIDTH_MAX)
        ]
        if pairs.empty:
            continue
        nearest = pairs.loc[pairs.groupby(["ts_ms", "instrument_name"])["width"].idxmin()].copy()
        nearest["side"] = side
        out.append(nearest)
    if not out:
        return pd.DataFrame()
    spreads = pd.concat(out, ignore_index=True)
    spreads = spreads.rename(columns={c: f"short_{c}" for c in _LEG_COLUMNS})
    spreads = spreads.rename(columns={f"{c}_long": f"long_{c}" for c in _LEG_COLUMNS})
    return check_spreads(spreads)


def check_spreads(s: pd.DataFrame) -> pd.DataFrame:
    """Add the liquidity checks, the order size and the credit to candidate spreads."""
    _leg_quality(s, "short_", s["ts_ms"])
    _leg_quality(s, "long_", s["ts_ms"])
    usd = s["short_index_price"].where(s["book"] == "inverse", 1.0)
    s["credit_usd"] = (s["short_bid"] - s["long_ask"]) * usd
    s["max_loss_usd"] = s["width"] - s["credit_usd"]
    s["order_btc"] = (EQUITY_USD * MAX_LOSS_SHARE / s["max_loss_usd"]).where(s["max_loss_usd"] > 0)
    s["two_sided"] = s["short_two_sided"] & s["long_two_sided"]
    s["tight"] = (s["short_spread_of_mid"] <= MAX_SPREAD_OF_MID) & (
        s["long_spread_of_mid"] <= MAX_SPREAD_OF_MID
    )
    s["fresh"] = s["short_fresh"] & s["long_fresh"]
    s["deep"] = (s["short_bid_size"] >= s["order_btc"]) & (s["long_ask_size"] >= s["order_btc"])
    s["liquid"] = s["two_sided"] & s["tight"] & s["fresh"] & s["deep"]
    s["credit_ok"] = s["credit_usd"] >= MIN_CREDIT_OF_WIDTH * s["width"]
    return s


@dataclass
class BookSummary:
    book: str
    minutes: int = 0
    minutes_with_candidate: int = 0
    minutes_liquid: int = 0
    minutes_liquid_bull_put: int = 0
    minutes_liquid_bear_call: int = 0
    minutes_liquid_and_credit: int = 0
    spreads: int = 0
    share: dict[str, float] = field(default_factory=dict)
    medians: dict[str, float] = field(default_factory=dict)
    expiries: list[str] = field(default_factory=list)
    trades: int = 0
    trade_btc: float = 0.0
    trade_instruments: int = 0
    book_trades: int = 0


def summarize(spreads: pd.DataFrame, minutes: Iterable[int], book: str) -> BookSummary:
    minutes = list(minutes)
    out = BookSummary(book=book, minutes=len(minutes))
    s = spreads[spreads["book"] == book] if not spreads.empty else spreads
    if s.empty:
        return out
    per_minute = s.groupby("ts_ms")
    liquid = s[s["liquid"]]
    out.minutes_with_candidate = per_minute.ngroups
    out.minutes_liquid = liquid["ts_ms"].nunique()
    out.minutes_liquid_bull_put = liquid[liquid["side"] == "bull put"]["ts_ms"].nunique()
    out.minutes_liquid_bear_call = liquid[liquid["side"] == "bear call"]["ts_ms"].nunique()
    out.minutes_liquid_and_credit = liquid[liquid["credit_ok"]]["ts_ms"].nunique()
    out.spreads = len(s)
    for check in ("two_sided", "tight", "fresh", "deep", "liquid", "credit_ok"):
        out.share[check] = float(s[check].mean())
    for column in (
        "short_spread_of_mid",
        "long_spread_of_mid",
        "short_bid_size",
        "long_ask_size",
        "order_btc",
        "width_of_forward",
        "credit_usd",
    ):
        out.medians[column] = float(s[column].median())
    for column in ("short_spread_of_mid", "long_spread_of_mid"):
        out.medians[column + "_p90"] = float(s[column].quantile(0.9))
    out.expiries = sorted(
        pd.to_datetime(s["expiry_ms"].unique(), unit="ms", utc=True).strftime("%Y-%m-%d")
    )
    return out


def add_trades(
    summary: BookSummary, trades: pd.DataFrame, legs: set[str], book_names: set[str]
) -> None:
    """Trades on instruments that were candidate legs, and all trades in the book's window."""
    if trades.empty:
        return
    on_legs = trades[trades["instrument_name"].isin(legs)]
    summary.trades += len(on_legs)
    summary.trade_btc += float(on_legs["amount"].sum())
    summary.trade_instruments += int(on_legs["instrument_name"].nunique())
    summary.book_trades += int(trades["instrument_name"].isin(book_names).sum())


def leg_names(spreads: pd.DataFrame, book: str) -> set[str]:
    if spreads.empty:
        return set()
    s = spreads[spreads["book"] == book]
    return set(s["short_instrument_name"]) | set(s["long_instrument_name"])
