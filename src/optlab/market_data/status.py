"""What the status page shows, read from the database (read-only).

- the last tick per stream (option snapshots by book, indexes, perpetuals, futures, trades,
  the raw stream) and how old it is;
- the recorder: current run, uptime, restarts, memory, connections;
- recent gaps (the gap report and skipped periods);
- data volume per day (rows, export and raw bytes, whether it is in R2), with days whose raw
  stream passed the 1 GB warning level flagged.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg

from optlab.domain.clock import MS_PER_DAY, MS_PER_HOUR, ms_to_datetime

RAW_WARN_BYTES = 1_000_000_000  # warning level decided 14 Nov; data is never dropped
STALE_AFTER_MS = 2 * 60_000


@dataclass(frozen=True)
class Tick:
    stream: str
    last_ms: int | None
    detail: str

    def age_s(self, now_ms: int) -> float | None:
        return None if self.last_ms is None else (now_ms - self.last_ms) / 1000

    def stale(self, now_ms: int) -> bool:
        return self.last_ms is None or now_ms - self.last_ms > STALE_AFTER_MS


@dataclass(frozen=True)
class RecorderState:
    run_id: str
    started_ms: int
    reported_ms: int
    rss_bytes: int | None
    peak_rss_bytes: int | None
    connections_up: int
    connections: int
    msgs_per_s: float
    raw_day: date
    raw_bytes_day: int
    runs_7d: int


@dataclass(frozen=True)
class DayVolume:
    day: date
    option_rows: int
    expected_minutes: int
    present_minutes: int
    export_bytes: int | None
    raw_bytes: int | None
    in_r2: bool
    dataset_version: str | None

    @property
    def raw_over_warning(self) -> bool:
        return self.raw_bytes is not None and self.raw_bytes >= RAW_WARN_BYTES


def connect(database_url: str) -> psycopg.Connection[Any]:
    """A read-only connection: the status page can never change anything."""
    return psycopg.connect(
        database_url,
        autocommit=True,
        application_name="optlab-status",
        options="-c default_transaction_read_only=on",
    )


def last_ticks(conn: psycopg.Connection[Any], now_ms: int) -> list[Tick]:
    since = ms_to_datetime(now_ms - MS_PER_DAY)
    ticks: list[Tick] = []
    for book in ("linear", "inverse"):
        row = conn.execute(
            "WITH last AS (SELECT max(q.ts) AS ts FROM option_quotes q "
            "JOIN instruments i USING (instrument_name) WHERE i.book = %s AND q.ts >= %s) "
            "SELECT utc_ms(last.ts), (SELECT count(*) FROM option_quotes q "
            "JOIN instruments i USING (instrument_name) WHERE q.ts = last.ts AND i.book = %s) "
            "FROM last",
            (book, since, book),
        ).fetchone()
        label = "BTC_USDC options" if book == "linear" else "inverse BTC options"
        last = None if row is None or row[0] is None else int(row[0])
        ticks.append(Tick(label, last, f"{row[1] if row else 0} options in the last snapshot"))
    for kind, label in (("index", "indexes"), ("perpetual", "perpetuals"), ("future", "futures")):
        row = conn.execute(
            "SELECT utc_ms(max(ts)), count(DISTINCT instrument_name) FROM underlying_ticks "
            "WHERE kind = %s AND ts >= %s",
            (kind, since),
        ).fetchone()
        last = None if row is None or row[0] is None else int(row[0])
        ticks.append(Tick(label, last, f"{row[1] if row else 0} instruments in the last day"))
    row = conn.execute(
        "SELECT max(recv_ts_ms), count(*) FILTER (WHERE recv_ts_ms >= %s) FROM option_trades "
        "WHERE ts >= %s",
        (now_ms - MS_PER_HOUR, since),
    ).fetchone()
    last = None if row is None or row[0] is None else int(row[0])
    ticks.append(Tick("option trades", last, f"{row[1] if row else 0} in the last hour"))
    row = conn.execute(
        "SELECT utc_ms(ts), raw_bytes_day FROM recorder_status ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row is not None:
        ticks.append(
            Tick("raw stream (files)", int(row[0]), f"{int(row[1]) / 1e6:.0f} MB so far today")
        )
    return ticks


def recorder_state(conn: psycopg.Connection[Any], now_ms: int) -> RecorderState | None:
    row = conn.execute(
        "SELECT run_id, started_ms, utc_ms(ts), rss_bytes, peak_rss_bytes, connections_up, "
        "connections, msgs_per_s, raw_day, raw_bytes_day FROM recorder_status "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    runs = conn.execute(
        "SELECT count(DISTINCT run_id) FROM recorder_status WHERE ts >= %s",
        (ms_to_datetime(now_ms - 7 * MS_PER_DAY),),
    ).fetchone()
    return RecorderState(
        run_id=row[0],
        started_ms=int(row[1]),
        reported_ms=int(row[2]),
        rss_bytes=row[3],
        peak_rss_bytes=row[4],
        connections_up=int(row[5]),
        connections=int(row[6]),
        msgs_per_s=float(row[7]),
        raw_day=row[8],
        raw_bytes_day=int(row[9]),
        runs_7d=int(runs[0]) if runs else 0,
    )


def recording_uptime(conn: psycopg.Connection[Any], now_ms: int, hours: int) -> float | None:
    """Share of expected snapshot minutes recorded over the last ``hours`` checked hours."""
    row = conn.execute(
        "SELECT sum(present_minutes), sum(expected_minutes) FROM data_quality_runs "
        "WHERE hour_ts >= %s",
        (ms_to_datetime(now_ms - hours * MS_PER_HOUR),),
    ).fetchone()
    if row is None or not row[1]:
        return None
    return float(row[0]) / float(row[1])


def memory_series(conn: psycopg.Connection[Any], since_ms: int) -> list[tuple[int, int, str]]:
    """(time, resident bytes, run id) of the recorder since ``since_ms``."""
    rows = conn.execute(
        "SELECT utc_ms(ts), rss_bytes, run_id FROM recorder_status "
        "WHERE ts >= %s AND rss_bytes IS NOT NULL ORDER BY ts",
        (ms_to_datetime(since_ms),),
    ).fetchall()
    return [(int(t), int(b), str(r)) for t, b, r in rows]


def daily_volume(conn: psycopg.Connection[Any]) -> list[DayVolume]:
    rows = conn.execute(
        "WITH runs AS (SELECT (hour_ts AT TIME ZONE 'UTC')::date AS day, sum(option_rows) AS r, "
        "sum(expected_minutes) AS e, sum(present_minutes) AS p FROM data_quality_runs GROUP BY 1), "
        "status AS (SELECT raw_day AS day, max(raw_bytes_day) AS raw FROM recorder_status "
        "GROUP BY 1) "
        "SELECT d.day, coalesce(runs.r, 0), coalesce(runs.e, 0), coalesce(runs.p, 0), "
        "x.export_bytes, coalesce(x.raw_bytes, status.raw), x.uploaded_ms IS NOT NULL, "
        "x.dataset_version "
        "FROM (SELECT day FROM runs UNION SELECT day FROM dataset_exports "
        "UNION SELECT day FROM status) d "
        "LEFT JOIN runs USING (day) LEFT JOIN dataset_exports x USING (day) "
        "LEFT JOIN status USING (day) ORDER BY d.day DESC"
    ).fetchall()
    return [
        DayVolume(
            day=r[0],
            option_rows=int(r[1]),
            expected_minutes=int(r[2]),
            present_minutes=int(r[3]),
            export_bytes=None if r[4] is None else int(r[4]),
            raw_bytes=None if r[5] is None else int(r[5]),
            in_r2=bool(r[6]),
            dataset_version=r[7],
        )
        for r in rows
    ]


def database_bytes(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT pg_database_size(current_database())").fetchone()
    return int(row[0]) if row else 0


def recent_disconnects(conn: psycopg.Connection[Any], since_ms: int) -> list[tuple[int, str, str]]:
    rows = conn.execute(
        "SELECT ts_ms, source, detail FROM connection_events "
        "WHERE event = 'disconnected' AND ts >= %s ORDER BY ts DESC LIMIT 50",
        (ms_to_datetime(since_ms),),
    ).fetchall()
    return [(int(t), str(s), str(d)) for t, s, d in rows]
