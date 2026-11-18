"""The gate's comparison with Deribit, on made-up quotes where the answer is known."""

import numpy as np
import pandas as pd
import pytest

from optlab.domain.market import Book
from optlab.evaluation.deribit_check import (
    engine_rows,
    leg_choice,
    open_interest_check,
    summarise,
    surface_rows,
)
from optlab.volatility.surface import build_surface
from tests.volatility.helpers import T0, make_chain, make_futures


def _chain(book: Book = Book.LINEAR) -> pd.DataFrame:
    chain = make_chain(book=book, days=(9.0, 20.0, 37.0))
    chain["exchange_ts_ms"] = T0  # "Deribit" priced at the snapshot, with our own model
    return chain


@pytest.mark.parametrize("book", [Book.LINEAR, Book.INVERSE])
def test_engine_matches_a_chain_priced_by_the_same_model(book: Book) -> None:
    rows = engine_rows(_chain(book), book, T0)
    assert len(rows) > 100
    ok = rows.dropna(subset=["our_iv"])
    assert len(ok) > 0.9 * len(rows)
    assert np.allclose(ok["our_iv"], ok["mark_iv"], atol=1e-7)
    for col in ("delta", "gamma", "vega", "theta"):
        assert np.allclose(rows[f"our_{col}"], rows[col], rtol=1e-12, atol=1e-15), col


def test_engine_sees_a_wrong_mark_iv() -> None:
    chain = _chain()
    chain["mark_iv"] += 0.01  # Deribit's IV one vol point above the price it quotes
    rows = engine_rows(chain, Book.LINEAR, T0)
    table = summarise(rows, "our_iv", "mark_iv", scale=100, by="days")
    assert table["median"].to_numpy() == pytest.approx(1.0, abs=1e-6)
    assert table["n"].sum() == rows["our_iv"].notna().sum()


def test_summary_buckets() -> None:
    rows = pd.DataFrame(
        {"abs_delta": [0.02, 0.2, 0.2, 0.6], "days": [2, 10, 10, 40], "a": [1.0, 2, 3, 4], "b": 0.0}
    )
    t = summarise(rows, "a", "b")
    assert list(t["n"]) == [1, 2, 1]
    assert t["max"].max() == 4.0


def test_leg_choice_agrees_when_the_deltas_agree() -> None:
    chain = _chain()
    surface = build_surface(chain, make_futures(chain), Book.LINEAR, T0, index_price=65_000.0)
    rows = surface_rows(chain, surface)
    picks = leg_choice(rows)
    assert {p.expiry_ms for p in picks} == {int(e) for e in sorted(chain.expiry_ms.unique())[:2]}
    assert all(p.by_deribit is not None for p in picks)
    assert all(p.by_deribit == p.by_ours for p in picks)
    # Shift Deribit's deltas: the picks move to another strike.
    shifted = rows.assign(delta=rows["delta"] * 1.3)
    assert any(p.by_deribit != p.by_ours for p in leg_choice(shifted))


def test_open_interest_lag() -> None:
    minutes = [T0 + i * 60_000 for i in range(6)]
    oi = pd.DataFrame(
        {
            "ts_ms": minutes * 2,
            "instrument_name": ["A"] * 6 + ["B"] * 6,
            "open_interest": [1, 1, 1, 2, 2, 2] + [5] * 6,
        }
    )
    trades = pd.DataFrame(
        {"instrument_name": ["A"], "recv_ts_ms": [T0 + 60_000 + 30_000], "amount": [1.0]}
    )
    out = open_interest_check(oi, trades)
    # Received 00:01:30, so it belongs to the 00:02 snapshot; OI moved at 00:03: one minute late.
    assert out["lag_median_min"] == 1.0
    assert out["traded_minutes"] == 1
    assert out["changed_share_traded"] == 0.0
    assert out["changed_share_untraded"] == pytest.approx(1 / 9)
