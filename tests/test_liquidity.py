"""Candidate v1 spreads and the liquidity rule, on a hand-built option chain."""

import pandas as pd
import pytest

from optlab.domain.clock import MS_PER_DAY
from optlab.evaluation import liquidity as liq

T = 1_794_607_200_000  # 2026-11-13 22:00 UTC
F = 90_000.0


def opt(name: str, book: str, kind: str, strike: float, days: float, delta: float, **q: float):
    row = {
        "ts_ms": T,
        "instrument_name": name,
        "book": book,
        "option_type": kind,
        "strike": strike,
        "expiry_ms": T + round(days * MS_PER_DAY),
        "dte": days,
        "bid": 300.0,
        "ask": 310.0,
        "bid_size": 5.0,
        "ask_size": 5.0,
        "delta": delta,
        "underlying_price": F,
        "index_price": F - 50,
        "exchange_ts_ms": T - 1_000,
    }
    row.update(q)
    return row


def chain() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # Linear puts, 13 days: short 86k, longs 84k (2.2% away) and 82k (4.4%).
            opt("L-86P", "linear", "put", 86_000, 13, -0.20, bid=950.0, ask=980.0),
            opt("L-84P", "linear", "put", 84_000, 13, -0.12, bid=500.0, ask=520.0),
            opt("L-82P", "linear", "put", 82_000, 13, -0.08, bid=250.0, ask=280.0),
            opt("L-88P", "linear", "put", 88_000, 13, -0.35),  # closer in: never the long leg
            # Linear calls: short 96k; the 97k long is only 1.1% away, so 98k is used, and
            # its quote is wide (18% of mid).
            opt("L-96C", "linear", "call", 96_000, 13, 0.20, bid=800.0, ask=820.0),
            opt("L-97C", "linear", "call", 97_000, 13, 0.15),
            opt("L-98C", "linear", "call", 98_000, 13, 0.10, bid=150.0, ask=180.0),
            # Too near expiry: ignored.
            opt("L-86P-5d", "linear", "put", 86_000, 5, -0.20),
            opt("L-84P-5d", "linear", "put", 84_000, 5, -0.10),
            # Inverse put (prices in BTC): thin bid size for the order the sizing allows.
            opt("I-86P", "inverse", "put", 86_000, 13, -0.22, bid=0.0120, ask=0.0125, bid_size=1.0),
            opt("I-84P", "inverse", "put", 84_000, 13, -0.13, bid=0.0058, ask=0.0060),
            # Inverse call with an empty bid on the short leg.
            opt("I-96C", "inverse", "call", 96_000, 13, 0.25, bid=float("nan")),
            opt("I-98C", "inverse", "call", 98_000, 13, 0.11, bid=0.002, ask=0.0021),
        ]
    )


def test_candidates_follow_the_v1_entry_rules() -> None:
    spreads = liq.candidate_spreads(chain())
    pairs = {
        (r.short_instrument_name, r.long_instrument_name, r.side) for r in spreads.itertuples()
    }
    assert pairs == {
        ("L-86P", "L-84P", "bull put"),  # nearest valid long leg, not 82k
        ("L-96C", "L-98C", "bear call"),  # 97k is under 2% away
        ("I-86P", "I-84P", "bull put"),
        ("I-96C", "I-98C", "bear call"),
    }


def test_liquidity_rule_and_order_size() -> None:
    s = liq.candidate_spreads(chain()).set_index("short_instrument_name")
    put = s.loc["L-86P"]
    assert put["credit_usd"] == pytest.approx(950 - 520)
    assert put["order_btc"] == pytest.approx(2_000 / (2_000 - 430))
    assert put["liquid"]
    assert put["credit_ok"]  # 430 >= 20% of the 2,000 width

    call = s.loc["L-96C"]
    assert call["long_spread_of_mid"] == pytest.approx(30 / 165)
    assert not call["tight"]
    assert not call["liquid"]

    inverse_put = s.loc["I-86P"]
    credit = (0.0120 - 0.0060) * (F - 50)  # BTC premiums at the index price
    assert inverse_put["credit_usd"] == pytest.approx(credit)
    assert inverse_put["order_btc"] == pytest.approx(2_000 / (2_000 - credit))
    assert not inverse_put["deep"]  # 1.0 BTC bid against a ~1.3 BTC order
    assert not inverse_put["liquid"]

    assert not s.loc["I-96C"]["two_sided"]


def test_stale_quotes_fail_the_rule() -> None:
    frame = chain()
    frame.loc[frame["instrument_name"] == "L-84P", "exchange_ts_ms"] = T - 121_000
    s = liq.candidate_spreads(frame).set_index("short_instrument_name")
    assert not s.loc["L-86P"]["fresh"]
    assert not s.loc["L-86P"]["liquid"]


def test_summary_counts_minutes_and_shares() -> None:
    later = chain().assign(ts_ms=T + 60_000)
    later.loc[later["instrument_name"] == "L-84P", "ask_size"] = 0.5  # too small: not deep
    spreads = pd.concat([liq.candidate_spreads(chain()), liq.candidate_spreads(later)])
    linear = liq.summarize(spreads, [T, T + 60_000, T + 120_000], "linear")
    assert linear.minutes == 3
    assert linear.minutes_with_candidate == 2
    assert linear.minutes_liquid == 1
    assert linear.minutes_liquid_bull_put == 1
    assert linear.minutes_liquid_bear_call == 0
    assert linear.share["liquid"] == pytest.approx(1 / 4)
    assert linear.expiries == ["2026-11-26"]
    inverse = liq.summarize(spreads, [T], "inverse")
    assert inverse.minutes_liquid == 0
    assert liq.leg_names(spreads, "inverse") == {"I-86P", "I-84P", "I-96C", "I-98C"}


def test_empty_chains_give_empty_results() -> None:
    assert liq.candidate_spreads(pd.DataFrame()).empty
    assert liq.summarize(pd.DataFrame(), [T], "linear").minutes_liquid == 0
