"""btc-options-lab status page (Phase 2): is the recorder healthy and is the data complete?

Run on the server as the ``dashboard`` service (bound to the server's localhost only) and open
it through an SSH tunnel; see infra/server-setup.md. Locally: ``uv run streamlit run
apps/dashboard/status.py`` with DATABASE_URL set. The database connection is read-only, so
nothing on this page can change the system.
"""

import time
from datetime import UTC, datetime

import pandas as pd
import streamlit as st

from optlab.config import Settings
from optlab.domain.clock import MS_PER_DAY, ms_to_datetime
from optlab.market_data import status
from optlab.market_data.quality import format_gap_report, gap_report

SERIES_COLOR = "#2a78d6"  # categorical slot 1 of the dataviz palette (one series)

st.set_page_config(
    page_title="Recorder status", page_icon=":material/monitor_heart:", layout="wide"
)


@st.cache_resource
def _database_url() -> str:
    settings = Settings()  # refuses LIVE_TRADING=true
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is not set")
    return settings.database_url.get_secret_value()


def _when(ms: int | None) -> str:
    return "never" if ms is None else ms_to_datetime(ms).strftime("%Y-%m-%d %H:%M:%S UTC")


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _mb(nbytes: int | None) -> str:
    return "-" if nbytes is None else f"{nbytes / 1e6:,.0f} MB"


@st.fragment(run_every="60s")
def page() -> None:
    now = time.time_ns() // 1_000_000
    with status.connect(_database_url()) as conn:
        ticks = status.last_ticks(conn, now)
        recorder = status.recorder_state(conn, now)
        uptime_24h = status.recording_uptime(conn, now, 24)
        uptime_7d = status.recording_uptime(conn, now, 24 * 7)
        memory = status.memory_series(conn, now - MS_PER_DAY)
        volume = status.daily_volume(conn)
        db_bytes = status.database_bytes(conn)
        disconnects = status.recent_disconnects(conn, now - MS_PER_DAY)
        days = gap_report(conn, now)

    st.title("Recorder status")
    st.caption(
        f"Updated {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC, every minute. "
        "Paper-trading research project, not investment advice."
    )

    # Warnings first: anything stale, any day over the raw-stream warning level.
    stale = [t.stream for t in ticks if t.stale(now)]
    if stale:
        st.error(f"No fresh data for over 2 minutes: {', '.join(stale)}", icon=":material/error:")
    over = [v for v in volume if v.raw_over_warning]
    if recorder and recorder.raw_bytes_day >= status.RAW_WARN_BYTES:
        st.warning(
            f"Raw stream for {recorder.raw_day} is at {_mb(recorder.raw_bytes_day)}, above the "
            "1 GB daily warning level. All data is kept.",
            icon=":material/warning:",
        )
    for v in over:
        st.warning(
            f"Raw stream for {v.day} reached {_mb(v.raw_bytes)}, above the 1 GB daily warning "
            "level. All data was kept.",
            icon=":material/warning:",
        )

    cols = st.columns(5)
    if recorder is None:
        cols[0].metric("Recorder", "no status yet")
    else:
        cols[0].metric(
            "Recorder up for",
            _age((now - recorder.started_ms) / 1000),
            help=f"Run {recorder.run_id}; {recorder.runs_7d} runs in the last 7 days",
        )
        cols[1].metric(
            "Memory",
            _mb(recorder.rss_bytes),
            help=f"Peak this run {_mb(recorder.peak_rss_bytes)}; container limit 1 GB",
        )
        cols[2].metric(
            "Connections up",
            f"{recorder.connections_up} of {recorder.connections}",
            help=f"{recorder.msgs_per_s:,.0f} messages a second",
        )
        cols[3].metric("Raw files today", _mb(recorder.raw_bytes_day), help="Warning at 1 GB")
    cols[4].metric(
        "Minutes recorded, 24 h",
        "-" if uptime_24h is None else f"{uptime_24h:.2%}",
        help="7 days: " + ("-" if uptime_7d is None else f"{uptime_7d:.2%}"),
    )

    st.subheader("Last tick per stream")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "stream": t.stream,
                    "last": _when(t.last_ms),
                    "age": _age(t.age_s(now)),
                    "status": "stale" if t.stale(now) else "ok",
                    "detail": t.detail,
                }
                for t in ticks
            ]
        ),
        hide_index=True,
        width="stretch",
    )

    st.subheader("Recorder memory, last 24 hours")
    if memory:
        frame = pd.DataFrame(
            {
                "time (UTC)": [ms_to_datetime(t).replace(tzinfo=None) for t, _, _ in memory],
                "resident memory (MiB)": [b / 2**20 for _, b, _ in memory],
            }
        )
        st.line_chart(
            frame, x="time (UTC)", y="resident memory (MiB)", color=SERIES_COLOR, height=220
        )
    else:
        st.info("No memory readings yet (the recorder writes one a minute).")

    st.subheader("Recent gaps")
    st.markdown(format_gap_report(days[-7:]))

    st.subheader("Data volume")
    st.caption(f"Database {_mb(db_bytes)} on disk.")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "UTC day": v.day.isoformat(),
                    "option rows": v.option_rows,
                    "minutes": f"{v.present_minutes}/{v.expected_minutes}",
                    "export": _mb(v.export_bytes),
                    "raw stream": _mb(v.raw_bytes) + (" (over 1 GB)" if v.raw_over_warning else ""),
                    "in R2": "yes" if v.in_r2 else "not yet",
                    "dataset version": (v.dataset_version or "")[:12],
                }
                for v in volume
            ]
        ),
        hide_index=True,
        width="stretch",
    )

    st.subheader("Disconnects, last 24 hours")
    if disconnects:
        st.dataframe(
            pd.DataFrame(
                [{"time": _when(t), "connection": s, "reason": d} for t, s, d in disconnects]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.write("None.")


page()
