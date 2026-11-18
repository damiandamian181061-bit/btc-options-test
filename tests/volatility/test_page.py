"""The status page's "Volatility surface" tab and the data behind it."""

import pytest

from optlab.domain.clock import MS_PER_MINUTE
from optlab.domain.market import Book
from optlab.market_data.status import connect
from optlab.market_data.view import DatabaseSource
from optlab.volatility import page as vol_page
from optlab.volatility import store
from optlab.volatility.surface import build_surface
from optlab.volatility.worker import CloseCache, SurfaceWorker
from tests.conftest import REPO_ROOT
from tests.market_data.synthetic import populate
from tests.market_data.test_view import START
from tests.volatility.helpers import T0, make_chain, make_futures

M = MS_PER_MINUTE


def test_smile_frames_show_our_fit_our_quotes_and_deribits_marks() -> None:
    chain = make_chain(days=(9.0, 37.0), forward=65_000.0)
    surface = build_surface(chain, make_futures(chain), Book.LINEAR, T0, index_price=65_000.0)
    smile = vol_page.smile_frames(surface.fits[0], chain, Book.LINEAR)
    assert smile is not None
    assert smile.days == pytest.approx(9.0, abs=1e-6)
    assert set(smile.curve["series"]) == {"Our fit"}
    assert set(smile.points["series"]) == {"Quote mid (ours)", "Deribit mark"}
    assert smile.curve["strike"].is_monotonic_increasing
    # Made-up quotes priced on the smile: our fit, our mids and the marks all agree.
    mids = smile.points.loc[smile.points["series"] == "Quote mid (ours)"]
    marks = smile.points.loc[smile.points["series"] == "Deribit mark"]
    assert mids["iv"].to_numpy() == pytest.approx(
        marks.set_index("strike").loc[mids["strike"], "iv"].to_numpy(), abs=1e-6
    )
    assert "Forward 65,000.00 (future, future - parity +0.0 bp)" in smile.info


def test_surface_grid_marks_extrapolated_cells() -> None:
    chain = make_chain(days=(9.0, 37.0))
    surface = build_surface(chain, make_futures(chain), Book.LINEAR, T0, index_price=65_000.0)
    rows = [(p.tenor_days, p.bucket.value, p.iv, p.extrapolated) for p in surface.points]
    import pandas as pd

    grid = vol_page.surface_grid(
        pd.DataFrame(rows, columns=["tenor_days", "bucket", "iv", "extrapolated"])
    )
    assert list(grid.index) == ["1 d", "3 d", "7 d", "14 d", "30 d", "60 d", "90 d"]
    assert list(grid.columns) == ["10d_put", "25d_put", "atm", "25d_call", "10d_call"]
    assert grid.loc["1 d", "atm"].endswith("*")  # before the first expiry (9 days)
    assert not grid.loc["30 d", "atm"].endswith("*")


@pytest.mark.db
def test_the_page_data_and_tab(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import AppTest

    populate(database_url, START, START + 15 * M)
    source = DatabaseSource(database_url)
    conn = store.connect(database_url)
    try:
        SurfaceWorker(
            source=source, conn=conn, closes=CloseCache((Book.LINEAR, Book.INVERSE))
        ).step(START + 10 * M + 5_000)
    finally:
        conn.close()
        source.close()
    with connect(database_url) as ro:  # the page's read-only connection
        ts, rows = vol_page.latest_surface(ro, Book.LINEAR)
        assert ts == START + 10 * M
        assert len(rows) == 35
        table = vol_page.expiry_table(vol_page.latest_expiries(ro, Book.LINEAR))
        assert list(table["expiry"]) == ["20 Nov 26", "27 Nov 26"]
        assert len(vol_page.feature_history(ro, Book.LINEAR, START)) == 1
    snap, smiles = vol_page.live_smiles(database_url, Book.LINEAR, START + 10 * M + 5_000)
    assert snap == START + 10 * M
    assert smiles
    assert all(not s.curve.empty for s in smiles)

    monkeypatch.setenv("DATABASE_URL", database_url)
    import streamlit as st

    st.cache_resource.clear()
    st.cache_data.clear()
    app = AppTest.from_file(str(REPO_ROOT / "apps" / "dashboard" / "status.py"), default_timeout=60)
    app.run()
    assert not app.exception, app.exception
    assert [t.label for t in app.tabs][-1] == "Volatility surface"
    headers = [h.value for h in app.subheader]
    for header in (
        "Surface, implied vol (%)",
        "Smiles, latest snapshot",
        "Expiries, latest stored minute",
        "ATM IV and realised vol, 30 days, last 24 hours (%)",
    ):
        assert header in headers
    metrics = {m.label: m.value for m in app.metric}
    assert "ATM IV 30 d" in metrics
