"""The gate's comparison with Deribit, on made-up quotes where the answer is known."""

import numpy as np
import pandas as pd
import pytest

from optlab.domain.market import Book
from optlab.evaluation.deribit_check import (
    TOLERANCES,
    Check,
    LegPick,
    engine_rows,
    gate_checks,
    leg_choice,
    leg_steps,
    open_interest_check,
    summarise,
    surface_rows,
)
from optlab.volatility.surface import Surface, build_surface
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


def test_tolerances_are_the_agreed_ones() -> None:
    # Agreed by Damian on 18 Nov 2026 (Phase 3, add-on g). Never widened to make a test pass.
    assert TOLERANCES == {
        "engine_delta_max": 0.0001,
        "engine_gamma_max": 0.00001,
        "engine_vega_max": 0.05,
        "engine_theta_max": 0.05,
        "engine_iv_p99": 0.5,
        "surface_iv_median": 0.25,
        "surface_iv_p99": 1.0,
        "surface_delta_p99": 0.005,
        "leg_differs_share": 0.02,
        "leg_differs_strikes": 1.0,
        "forward_parity_p95_bp": 10.0,
    }


def test_check_needs_data_to_pass() -> None:
    assert Check("x", measured=0.1, limit=0.2, n=5).passed
    assert not Check("x", measured=0.3, limit=0.2, n=5).passed
    assert not Check("x", measured=0.2, limit=0.2, n=0).passed
    assert not Check("x", measured=float("nan"), limit=0.2, n=5).passed
    assert Check("x", measured=0.2, limit=0.2, n=5).passed  # the limit itself passes


def _frames() -> tuple[pd.DataFrame, pd.DataFrame, list[Surface]]:
    chain = _chain()
    surface = build_surface(chain, make_futures(chain), Book.LINEAR, T0, index_price=65_000.0)
    return engine_rows(chain, Book.LINEAR, T0), surface_rows(chain, surface), [surface]


def test_gate_passes_when_deribit_uses_our_model() -> None:
    engine, surface, surfaces = _frames()
    checks = gate_checks(engine, surface, surfaces)
    assert [c.name for c in checks] == list(TOLERANCES)
    assert all(c.n > 0 for c in checks), [c for c in checks if c.n == 0]
    assert all(c.passed for c in checks), [c for c in checks if not c.passed]


def test_gate_fails_on_a_delta_just_outside_its_tolerance() -> None:
    engine, surface, surfaces = _frames()
    engine = engine.assign(delta=engine["delta"] + 0.00011)
    checks = {c.name: c for c in gate_checks(engine, surface, surfaces)}
    assert not checks["engine_delta_max"].passed
    assert checks["engine_delta_max"].measured == pytest.approx(0.00011, rel=1e-6)
    assert checks["engine_gamma_max"].passed


def test_gate_windows_leave_out_short_expiries_and_far_wings() -> None:
    engine, surface, surfaces = _frames()
    # A wild theta under 7 days and a wild IV outside 0.05-0.50 |delta| don't count.
    short = engine["days"] < 7
    wing = engine["abs_delta"] < 0.05
    engine = engine.assign(
        theta=np.where(short, engine["theta"] + 5.0, engine["theta"]),
        mark_iv=np.where(wing, engine["mark_iv"] + 0.05, engine["mark_iv"]),
    )
    checks = {c.name: c for c in gate_checks(engine, surface, surfaces)}
    assert checks["engine_theta_max"].passed
    assert checks["engine_iv_p99"].passed


def test_leg_steps_counts_strikes_between_the_picks() -> None:
    rows = pd.DataFrame(
        {
            "ts_ms": T0,
            "expiry_ms": 1,
            "call": False,
            "strike": [50_000.0, 51_000.0, 52_000.0, 53_000.0],
        }
    )
    picks = [
        LegPick(T0, 1, False, 51_000.0, 51_000.0),  # same strike: not counted
        LegPick(T0, 1, False, 51_000.0, 52_000.0),
        LegPick(T0, 1, False, 50_000.0, 53_000.0),
        LegPick(T0, 1, False, None, 53_000.0),  # one side only: not counted
    ]
    assert leg_steps(rows, picks) == [1, 3]


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
