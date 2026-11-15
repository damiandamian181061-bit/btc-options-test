"""btc-options-lab status page: is the recorder healthy and is the data complete (Phase 2)?
And the ROUGH paper service's decisions, positions, trades and equity (Phase 2B).

Run on the server as the ``dashboard`` service (bound to the server's localhost only) and open
it through an SSH tunnel; see infra/server-setup.md. Locally: ``uv run streamlit run
apps/dashboard/status.py`` with DATABASE_URL set. The database connection is read-only, so
nothing on this page can change the system.
"""

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import psycopg
import streamlit as st

from optlab.config import Settings
from optlab.domain.clock import MS_PER_DAY, ms_to_datetime
from optlab.market_data import status
from optlab.market_data.quality import format_gap_report, gap_report
from optlab.simulator.rough_store import PaperPage, page_data

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


def _minute(ms: int | None) -> str:
    return "" if ms is None else ms_to_datetime(ms).strftime("%Y-%m-%d %H:%M")


def _usdc(value: Decimal | None) -> str:
    return "" if value is None else f"{value:,.2f}"


def _num(value: Decimal | None) -> str:
    return "" if value is None else format(value.normalize(), "f")


def _pct(value: float | None) -> str:
    return "" if value is None else f"{value:.2%}"


def _paper(conn: psycopg.Connection[Any]) -> PaperPage | None:
    try:
        return page_data(conn)
    except psycopg.errors.UndefinedTable:
        return None  # migration 0003 not applied yet


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
        paper = _paper(conn)

    st.title("Recorder status")
    st.caption(
        f"Updated {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC, every minute. "
        "Paper-trading research project, not investment advice."
    )
    recorder_tab, paper_tab = st.tabs(["Recorder", "Paper trading (ROUGH)"])
    with recorder_tab:
        recorder_section(
            now, ticks, recorder, uptime_24h, uptime_7d, memory, volume, db_bytes, disconnects, days
        )
    with paper_tab:
        paper_section(paper, now)


def recorder_section(
    now: int,
    ticks: list[status.Tick],
    recorder: status.RecorderState | None,
    uptime_24h: float | None,
    uptime_7d: float | None,
    memory: list[tuple[int, int, str]],
    volume: list[status.DayVolume],
    db_bytes: int,
    disconnects: list[tuple[int, str, str]],
    days: list[Any],
) -> None:

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


def paper_section(paper: PaperPage | None, now: int) -> None:
    st.warning(
        "**ROUGH end-to-end version (Phase 2B).** Paper fills only: no orders are sent anywhere. "
        "Its profit and loss is **not a result**: the rule, fills, fees and accounting are "
        "untested shortcuts until Phase 4 replaces them (docs/rough-version.md).",
        icon=":material/construction:",
    )
    if paper is None:
        st.info("The paper service's tables do not exist yet (migration 0003).")
        return
    run = paper.runs[0] if paper.runs else None
    last = paper.equity[-1] if paper.equity else None
    open_spreads = [s for s in paper.spreads if s["status"] == "open"]
    realised = sum((s["pnl"] for s in paper.spreads if s["pnl"] is not None), Decimal(0))
    cols = st.columns(5)
    cols[0].metric(
        "Equity (rough), USDC",
        _usdc(last[1]) if last else "-",
        help="Cash plus open spreads at Deribit's mark price; paper capital 100,000 USDC",
    )
    cols[1].metric("Cash, USDC", _usdc(last[2]) if last else "-")
    cols[2].metric("Open spreads", len(open_spreads))
    cols[3].metric("Realised P&L, USDC", _usdc(realised), help="Closed and settled spreads")
    if run is None:
        cols[4].metric("Paper service", "never ran")
    else:
        cols[4].metric(
            "Paper service seen",
            _age((now - int(run["last_ms"])) / 1000) + " ago",
            help=f"Run {run['run_id']}, up {_age((now - int(run['started_ms'])) / 1000)}; "
            f"{run['minutes']} minutes processed, {run['skipped']} flagged minutes skipped",
        )
        if now - int(run["last_ms"]) > 5 * 60_000:
            st.error("The paper service has not run for over 5 minutes.", icon=":material/error:")

    st.subheader("Equity (rough), USDC")
    if paper.equity:
        frame = pd.DataFrame(
            {
                "time (UTC)": [ms_to_datetime(t).replace(tzinfo=None) for t, _, _ in paper.equity],
                "equity (USDC)": [float(e) for _, e, _ in paper.equity],
            }
        )
        st.line_chart(frame, x="time (UTC)", y="equity (USDC)", color=SERIES_COLOR, height=220)
    else:
        st.info("No equity rows yet (one per processed minute).")

    st.subheader("Daily decisions")
    if paper.decisions:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "UTC day": d["day"].isoformat(),
                        "at": _minute(d["ts_ms"]),
                        "action": d["action"],
                        "reason": d["reason"],
                        "IV30": _pct(d["iv30"]),
                        "RV30": _pct(d["rv30"]),
                        "BTC": "" if d["index_price"] is None else f"{d['index_price']:,.0f}",
                        "20-day avg": ""
                        if d["trend_average"] is None
                        else f"{d['trend_average']:,.0f}",
                        "side": d["side"] or "",
                    }
                    for d in paper.decisions
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No daily decision yet (the first comes at or after 09:00 UTC).")

    spread_rows = [
        {
            "id": s["id"],
            "side": s["side"],
            "short leg": s["short_instrument"],
            "long leg": s["long_instrument"],
            "BTC": _num(s["amount"]),
            "short delta": f"{s['short_delta']:+.3f}",
            "credit / BTC": _num(s["entry_credit"]),
            "after fees": _num(s["credit_after_fees"]),
            "max loss": _usdc(s["max_loss"]),
            "opened": _minute(s["opened_ms"]),
            "status": s["status"],
            "exit": s["exit_reason"] or "",
            "closed": _minute(s["closed_ms"]),
            "exit price / BTC": _num(s["exit_price"]),
            "P&L": _usdc(s["pnl"]),
            "thesis met": "" if s["thesis_met"] is None else ("yes" if s["thesis_met"] else "no"),
            "thesis": s["thesis"],
        }
        for s in paper.spreads
    ]
    st.subheader("Open spreads")
    opened = [r for r in spread_rows if r["status"] == "open"]
    if opened:
        st.dataframe(pd.DataFrame(opened), hide_index=True, width="stretch")
    else:
        st.write("None.")
    st.subheader("Closed and settled spreads")
    done = [r for r in spread_rows if r["status"] != "open"]
    if done:
        st.dataframe(pd.DataFrame(done), hide_index=True, width="stretch")
    else:
        st.write("None yet.")

    st.subheader("Trades (paper fills)")
    if paper.fills:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "spread": f["spread_id"],
                        "at": _minute(f["ts_ms"]),
                        "instrument": f["instrument_name"],
                        "kind": f["kind"],
                        "side": f["direction"],
                        "BTC": _num(f["amount"]),
                        "price": _num(f["price"]),
                        "fee": _num(f["fee"]),
                        "cash change": _usdc(f["cash"]),
                        "currency": f["currency"],
                    }
                    for f in paper.fills
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.write("None yet.")


page()
