"""The status page's queries, and the page itself rendered by Streamlit's test harness."""

from datetime import date

import psycopg
import pytest

from optlab.domain.clock import MS_PER_MINUTE, ms_to_datetime
from optlab.market_data import status
from optlab.market_data.quality import load_quality_config, run_checks
from optlab.market_data.rows import RecorderStatusRow
from tests.conftest import REPO_ROOT
from tests.market_data.synthetic import OPTIONS, copy_rows, populate, utc_ms

M = MS_PER_MINUTE
START = utc_ms(2026, 11, 13, 22)
END = utc_ms(2026, 11, 14, 1)
NOW = END + 30_000


def build(url: str) -> None:
    populate(url, START, END)
    rows = [
        RecorderStatusRow(
            ms_to_datetime(START + i * M + 7_000), "run-a" if i < 100 else "run-b",
            START if i < 100 else START + 100 * M, (300 + i) * 2**20, 425 * 2**20, 5, 5, 1626,
            1700.0, 40, 0, 0, ms_to_datetime(START + i * M).date(),
            1_200_000_000 if i == 119 else 5_000_000,
        )
        for i in range(180)
    ]  # fmt: skip
    with psycopg.connect(url) as conn:
        copy_rows(conn, "recorder_status", RecorderStatusRow._fields, rows)
        run_checks(conn, load_quality_config(REPO_ROOT / "configs"), END + 31 * M)


@pytest.mark.db
def test_status_queries(database_url: str) -> None:
    build(database_url)
    with status.connect(database_url) as conn:
        ticks = {t.stream: t for t in status.last_ticks(conn, NOW)}
        recorder = status.recorder_state(conn, NOW)
        volume = status.daily_volume(conn)
        memory = status.memory_series(conn, END - 60 * M)
        uptime = status.recording_uptime(conn, NOW, 24)
        disconnects = status.recent_disconnects(conn, START - M)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM option_quotes")

    linear = ticks["BTC_USDC options"]
    assert linear.last_ms == END - M
    assert (
        linear.detail == f"{sum(o.book == 'linear' for o in OPTIONS)} options in the last snapshot"
    )
    assert not linear.stale(NOW)
    assert ticks["indexes"].detail == "2 instruments in the last day"
    assert ticks["raw stream (files)"].last_ms == START + 179 * M + 7_000
    assert ticks["option trades"].stale(NOW + 10 * M)

    assert recorder is not None
    assert (recorder.run_id, recorder.started_ms, recorder.runs_7d) == ("run-b", START + 100 * M, 2)
    assert recorder.rss_bytes == 479 * 2**20
    assert len(memory) == 60
    assert uptime == 1.0
    assert disconnects[0][1] == "deribit-ws-1"

    by_day = {v.day: v for v in volume}
    assert by_day[date(2026, 11, 13)].raw_over_warning  # 1.2 GB on the 13th
    assert not by_day[date(2026, 11, 14)].raw_over_warning
    assert by_day[date(2026, 11, 13)].option_rows == 120 * len(OPTIONS)
    assert not by_day[date(2026, 11, 13)].in_r2


@pytest.mark.db
def test_the_status_page_renders(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import AppTest

    build(database_url)
    monkeypatch.setenv("DATABASE_URL", database_url)
    app = AppTest.from_file(str(REPO_ROOT / "apps" / "dashboard" / "status.py"), default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    assert app.title[0].value == "Recorder status"
    headers = [h.value for h in app.subheader]
    assert headers[:3] == ["Last tick per stream", "Recorder memory, last 24 hours", "Recent gaps"]
    assert any("above the 1 GB daily warning level" in w.value for w in app.warning)
