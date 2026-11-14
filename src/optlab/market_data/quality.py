"""Hourly data-quality checks, written to ``data_quality`` and ``data_quality_runs``.

Each complete UTC hour is checked once, after the recorder has written past it. A finding is a
run of consecutive snapshot minutes with the same problem, inside that hour (a problem that
spans hours gives one finding per hour; the gap report joins them up). Thresholds come from
``configs/data_quality.yaml``.

| check              | scope      | skip | what it means                                          |
| ------------------ | ---------- | ---- | ------------------------------------------------------ |
| missing_snapshot   | global     | yes  | no option rows at all for a minute                     |
| partial_snapshot   | global     | yes  | rows for too few of the options listed at that minute  |
| stale_index        | index      | yes  | an index missing, old or frozen at a minute            |
| crossed_book       | instrument | no   | bid at or above ask                                    |
| empty_book         | instrument | no   | neither a bid nor an ask                               |
| stale_quote        | instrument | no   | last ticker older than ``stale_quote_s``               |
| iv_outlier         | instrument | no   | mark IV missing, out of bounds, or jumping             |

``skip`` findings are the gap periods: backtests leave those snapshot minutes out
(``MarketDataView.snapshot_times``). Instrument findings don't remove the minute; strategies
see the same problem in the row itself.

"Expected" minutes start at the first minute ever recorded, so the hours before the recorder
first ran are not reported as gaps. Times are UTC milliseconds.
"""

import hashlib
import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import yaml
from pydantic import BaseModel, ConfigDict

from optlab.domain.clock import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE, floor_ms, ms_to_datetime

logger = logging.getLogger(__name__)

CONFIG_FILE = "data_quality.yaml"
CHECKS_CODE_VERSION = "1"  # bump when a check's logic changes

MISSING = "missing_snapshot"
PARTIAL = "partial_snapshot"
STALE_INDEX = "stale_index"
CROSSED = "crossed_book"
EMPTY = "empty_book"
STALE_QUOTE = "stale_quote"
IV_OUTLIER = "iv_outlier"
INSTRUMENT_CHECKS = (CROSSED, EMPTY, STALE_QUOTE, IV_OUTLIER)
SKIP_CHECKS = (MISSING, PARTIAL, STALE_INDEX)

# An hour is checked once the recorder has written a snapshot after it, or this long after
# its end if nothing arrives (a stopped recorder must still show up as a gap).
SETTLE_MS = 30 * MS_PER_MINUTE


class QualityConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    partial_snapshot_fraction: float
    stale_quote_s: int
    iv_min: float
    iv_max: float
    iv_jump: float
    iv_jump_min_days: float
    index_stale_s: int
    index_flat_minutes: int
    indexes: tuple[str, ...]

    def version(self) -> str:
        """Short hash of the code version and every threshold, stored with each checked hour."""
        text = CHECKS_CODE_VERSION + self.model_dump_json()
        return CHECKS_CODE_VERSION + "-" + hashlib.sha256(text.encode()).hexdigest()[:10]


def load_quality_config(config_dir: Path = Path("configs")) -> QualityConfig:
    data = yaml.safe_load((config_dir / CONFIG_FILE).read_text(encoding="utf-8"))
    return QualityConfig.model_validate(data)


@dataclass(frozen=True)
class Finding:
    check_name: str
    scope: str  # global, index or instrument
    subject: str  # index or instrument name; "" for global
    start_ms: int
    end_ms: int  # first minute after the run (exclusive)
    minutes: int
    skip: bool
    detail: str


@dataclass
class HourResult:
    hour_ms: int
    expected_minutes: int
    present_minutes: int
    option_rows: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def skipped_minutes(self) -> int:
        minutes: set[int] = set()
        for f in self.findings:
            if f.skip:
                minutes.update(range(f.start_ms, f.end_ms, MS_PER_MINUTE))
        return len(minutes)


def minute_runs(minutes: Iterable[int]) -> list[tuple[int, int, int]]:
    """Group minute timestamps into runs: (start, end exclusive, count), in time order."""
    runs: list[tuple[int, int, int]] = []
    for m in sorted(set(minutes)):
        if runs and runs[-1][1] == m:
            start, _, count = runs[-1]
            runs[-1] = (start, m + MS_PER_MINUTE, count + 1)
        else:
            runs.append((m, m + MS_PER_MINUTE, 1))
    return runs


_SNAPSHOT_COUNTS = """
WITH m AS (
    SELECT ts, count(*) AS n FROM option_quotes
    WHERE ts >= %(start)s AND ts < %(end)s GROUP BY ts
)
SELECT utc_ms(m.ts) AS ts_ms, m.n,
    (SELECT count(*) FROM instruments i
     WHERE i.kind = 'option'
       AND i.first_seen_ms <= utc_ms(m.ts)
       AND i.expiry_ms >= utc_ms(m.ts)
       AND (i.delisted_ms IS NULL OR i.delisted_ms > utc_ms(m.ts))) AS listed
FROM m ORDER BY 1
"""

_INSTRUMENT_FLAGS = """
WITH q AS (
    SELECT q.instrument_name, q.ts, q.bid, q.ask, q.mark_iv, q.exchange_ts_ms, i.expiry_ms,
           lag(q.mark_iv) OVER w AS prev_iv, lag(q.ts) OVER w AS prev_ts
    FROM option_quotes q JOIN instruments i USING (instrument_name)
    WHERE q.ts >= %(start)s - interval '1 minute' AND q.ts < %(end)s
    WINDOW w AS (PARTITION BY q.instrument_name ORDER BY q.ts)
), f AS (
    SELECT instrument_name, utc_ms(ts) AS ts_ms, bid, ask, mark_iv,
           coalesce(bid IS NOT NULL AND ask IS NOT NULL AND bid >= ask, false) AS crossed,
           (bid IS NULL AND ask IS NULL) AS empty,
           (utc_ms(ts) - exchange_ts_ms > %(stale_ms)s) AS stale,
           (mark_iv IS NULL OR mark_iv < %(iv_min)s OR mark_iv > %(iv_max)s) AS iv_bad,
           coalesce(prev_ts = ts - interval '1 minute'
                    AND abs(mark_iv - prev_iv) > %(iv_jump)s
                    AND expiry_ms - utc_ms(ts) >= %(jump_min_ms)s, false) AS iv_jump,
           prev_iv
    FROM q WHERE ts >= %(start)s
)
SELECT instrument_name, ts_ms, crossed, empty, stale, iv_bad, iv_jump, bid, ask, mark_iv, prev_iv
FROM f WHERE crossed OR empty OR stale OR iv_bad OR iv_jump
"""

_INDEX_TICKS = """
SELECT utc_ms(ts) AS ts_ms, index_price, exchange_ts_ms
FROM underlying_ticks
WHERE instrument_name = %(name)s AND kind = 'index' AND ts >= %(from)s AND ts < %(end)s
ORDER BY ts
"""


def recording_start_ms(conn: psycopg.Connection[Any]) -> int | None:
    """The first snapshot minute ever recorded, or None for an empty database."""
    row = conn.execute("SELECT utc_ms(min(ts)) FROM option_quotes").fetchone()
    return None if row is None or row[0] is None else int(row[0])


def check_hour(
    conn: psycopg.Connection[Any], hour_ms: int, config: QualityConfig, start_ms: int
) -> HourResult:
    """Run every check on one hour. ``start_ms`` is the first minute ever recorded."""
    end_ms = hour_ms + MS_PER_HOUR
    expected = list(range(max(hour_ms, start_ms), end_ms, MS_PER_MINUTE))
    expected_set = set(expected)
    params = {"start": ms_to_datetime(hour_ms), "end": ms_to_datetime(end_ms)}

    counts = {
        int(ts): (int(n), int(listed))
        for ts, n, listed in conn.execute(_SNAPSHOT_COUNTS, params)
        if int(ts) in expected_set
    }
    result = HourResult(
        hour_ms=hour_ms,
        expected_minutes=len(expected),
        present_minutes=len(counts),
        option_rows=sum(n for n, _ in counts.values()),
    )
    findings = result.findings

    for start, end, n in minute_runs(m for m in expected if m not in counts):
        findings.append(Finding(MISSING, "global", "", start, end, n, True, "no option rows"))

    partial = {
        m: (n, listed)
        for m, (n, listed) in counts.items()
        if listed and n < config.partial_snapshot_fraction * listed
    }
    for start, end, n in minute_runs(partial):
        run = [partial[m] for m in range(start, end, MS_PER_MINUTE)]
        worst = min(run, key=lambda x: x[0] / x[1])
        detail = f"as few as {worst[0]} of {worst[1]} listed options ({worst[0] / worst[1]:.0%})"
        findings.append(Finding(PARTIAL, "global", "", start, end, n, True, detail))

    findings.extend(_index_findings(conn, expected, hour_ms, end_ms, config))
    findings.extend(_instrument_findings(conn, params, config))
    return result


def _index_findings(
    conn: psycopg.Connection[Any],
    expected: Sequence[int],
    hour_ms: int,
    end_ms: int,
    config: QualityConfig,
) -> list[Finding]:
    out: list[Finding] = []
    lookback = hour_ms - config.index_flat_minutes * MS_PER_MINUTE
    stale_ms = config.index_stale_s * 1000
    for name in config.indexes:
        ticks = conn.execute(
            _INDEX_TICKS,
            {"name": name, "from": ms_to_datetime(lookback), "end": ms_to_datetime(end_ms)},
        ).fetchall()
        by_minute = {int(ts): (price, int(ex)) for ts, price, ex in ticks}
        reasons: dict[int, str] = {}
        streak, last_price, last_ts = 0, None, None
        for ts, price, _ in ticks:
            ts = int(ts)
            same = price == last_price and last_ts == ts - MS_PER_MINUTE
            streak = streak + 1 if same else 1
            last_price, last_ts = price, ts
            if streak >= config.index_flat_minutes and ts >= hour_ms:
                reasons[ts] = f"unchanged for {config.index_flat_minutes} minutes"
        for m in expected:
            if m not in by_minute:
                reasons[m] = "missing"
            elif m - by_minute[m][1] > stale_ms:
                reasons[m] = f"older than {config.index_stale_s} s"
        for start, end, n in minute_runs(reasons):
            kinds = sorted({reasons[m] for m in range(start, end, MS_PER_MINUTE)})
            out.append(Finding(STALE_INDEX, "index", name, start, end, n, True, "; ".join(kinds)))
    return out


def _instrument_findings(
    conn: psycopg.Connection[Any], params: dict[str, Any], config: QualityConfig
) -> list[Finding]:
    flagged: dict[tuple[str, str], dict[int, str]] = defaultdict(dict)
    rows = conn.execute(
        _INSTRUMENT_FLAGS,
        {
            **params,
            "stale_ms": config.stale_quote_s * 1000,
            "iv_min": config.iv_min,
            "iv_max": config.iv_max,
            "iv_jump": config.iv_jump,
            "jump_min_ms": round(config.iv_jump_min_days * MS_PER_DAY),
        },
    )
    for name, ts, crossed, empty, stale, iv_bad, iv_jump, bid, ask, iv, prev_iv in rows:
        ts = int(ts)
        if crossed:
            flagged[(CROSSED, name)][ts] = f"bid {bid} >= ask {ask}"
        if empty:
            flagged[(EMPTY, name)][ts] = "no bid and no ask"
        if stale:
            flagged[(STALE_QUOTE, name)][ts] = f"last ticker older than {config.stale_quote_s} s"
        if iv_bad:
            flagged[(IV_OUTLIER, name)][ts] = "mark IV missing" if iv is None else f"mark IV {iv}"
        elif iv_jump:
            flagged[(IV_OUTLIER, name)][ts] = f"mark IV {prev_iv} -> {iv} in one minute"
    out: list[Finding] = []
    for (check, name), minutes in sorted(flagged.items()):
        for start, end, n in minute_runs(minutes):
            detail = minutes[start] if n == 1 else f"{minutes[start]} (first of {n} minutes)"
            out.append(Finding(check, "instrument", name, start, end, n, False, detail))
    return out


def settled_hours(start_ms: int, now_ms: int, latest_ms: int | None) -> list[int]:
    """Every hour from the first recorded one that is complete and ready to check.

    Ready means the recorder has written a snapshot after the hour, or ``SETTLE_MS`` has passed
    since it ended (so a stopped recorder still shows up as a gap).
    """
    hours: list[int] = []
    hour = floor_ms(start_ms, MS_PER_HOUR)
    while True:
        end = hour + MS_PER_HOUR
        if not ((latest_ms is not None and latest_ms >= end) or now_ms >= end + SETTLE_MS):
            return hours
        hours.append(hour)
        hour = end


def _latest_ms(conn: psycopg.Connection[Any]) -> int | None:
    row = conn.execute("SELECT utc_ms(max(ts)) FROM option_quotes").fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _checked_hours(conn: psycopg.Connection[Any]) -> set[int]:
    rows = conn.execute("SELECT utc_ms(hour_ts) FROM data_quality_runs").fetchall()
    return {int(r[0]) for r in rows}


def hours_due(conn: psycopg.Connection[Any], now_ms: int, start_ms: int) -> list[int]:
    """Settled hours that have not been checked yet."""
    checked = _checked_hours(conn)
    return [h for h in settled_hours(start_ms, now_ms, _latest_ms(conn)) if h not in checked]


def save_hour(
    conn: psycopg.Connection[Any],
    result: HourResult,
    config: QualityConfig,
    checked_ms: int,
    *,
    replace: bool = False,
) -> None:
    """Store one hour's findings and its run row in a single transaction."""
    hour = ms_to_datetime(result.hour_ms)
    with conn.transaction():
        if replace:
            conn.execute("DELETE FROM data_quality WHERE hour_ts = %s", (hour,))
            conn.execute("DELETE FROM data_quality_runs WHERE hour_ts = %s", (hour,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO data_quality (hour_ts, check_name, scope, subject, start_ts, "
                "end_ts, minutes, skip, detail, detected_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        hour,
                        f.check_name,
                        f.scope,
                        f.subject,
                        ms_to_datetime(f.start_ms),
                        ms_to_datetime(f.end_ms),
                        f.minutes,
                        f.skip,
                        f.detail[:500],
                        checked_ms,
                    )
                    for f in result.findings
                ],
            )
        conn.execute(
            "INSERT INTO data_quality_runs (hour_ts, checked_ms, checks_version, "
            "expected_minutes, present_minutes, option_rows, findings, skipped_minutes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                hour,
                checked_ms,
                config.version(),
                result.expected_minutes,
                result.present_minutes,
                result.option_rows,
                len(result.findings),
                result.skipped_minutes,
            ),
        )


def run_checks(
    conn: psycopg.Connection[Any],
    config: QualityConfig,
    now_ms: int,
    *,
    hours: Sequence[int] | None = None,
) -> list[HourResult]:
    """Check every complete hour not checked yet (or exactly ``hours``, replacing old results)."""
    start = recording_start_ms(conn)
    if start is None:
        logger.info("data checks: nothing recorded yet")
        return []
    if hours is None:
        todo, replace = hours_due(conn, now_ms, start), False
    else:
        todo, replace = list(hours), True
    results: list[HourResult] = []
    for hour in todo:
        result = check_hour(conn, hour, config, start)
        save_hour(conn, result, config, now_ms, replace=replace)
        skips = [f for f in result.findings if f.skip]
        log = logger.warning if skips else logger.info
        log(
            "data checks %s: %d/%d minutes, %d findings, %d minutes to skip",
            ms_to_datetime(hour).strftime("%Y-%m-%d %H:00"),
            result.present_minutes,
            result.expected_minutes,
            len(result.findings),
            result.skipped_minutes,
        )
        results.append(result)
    return results


# ------------------------------------------------------------------------------------ report


@dataclass
class DayReport:
    day: date
    hours_checked: int
    hours_due: int
    expected_minutes: int
    present_minutes: int
    option_rows: int
    skip_periods: list[tuple[int, int, str]]  # merged across hours: (start, end, checks)
    counts: dict[str, int]  # findings by check

    @property
    def missing_minutes(self) -> int:
        return self.expected_minutes - self.present_minutes

    @property
    def skipped_minutes(self) -> int:
        return sum((end - start) // MS_PER_MINUTE for start, end, _ in self.skip_periods)

    @property
    def longest_gap_minutes(self) -> int:
        """Longest time between two usable snapshot minutes (1 when nothing is skipped)."""
        spans = [(end - start) // MS_PER_MINUTE for start, end, _ in self.skip_periods]
        return max(spans, default=0) + 1

    @property
    def covered(self) -> bool:
        return self.hours_checked == self.hours_due


def merge_periods(findings: Iterable[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Join overlapping or touching (start, end, check) periods; checks are listed once each."""
    merged: list[tuple[int, int, set[str]]] = []
    for start, end, check in sorted(findings):
        if merged and start <= merged[-1][1]:
            s, e, checks = merged[-1]
            merged[-1] = (s, max(e, end), checks | {check})
        else:
            merged.append((start, end, {check}))
    return [(s, e, ", ".join(sorted(c))) for s, e, c in merged]


def gap_report(conn: psycopg.Connection[Any], now_ms: int) -> list[DayReport]:
    """One row per UTC day from the first recorded day to today."""
    start = recording_start_ms(conn)
    if start is None:
        return []
    due_all = settled_hours(start, now_ms, _latest_ms(conn))
    runs = conn.execute(
        "SELECT utc_ms(hour_ts), expected_minutes, present_minutes, option_rows "
        "FROM data_quality_runs"
    ).fetchall()
    findings = conn.execute(
        "SELECT utc_ms(start_ts), utc_ms(end_ts), check_name, skip FROM data_quality"
    ).fetchall()
    days: list[DayReport] = []
    day_ms = floor_ms(start, MS_PER_DAY)
    while day_ms <= now_ms:
        day_end = day_ms + MS_PER_DAY
        due = {h for h in due_all if day_ms <= h < day_end}
        day_runs = [r for r in runs if day_ms <= int(r[0]) < day_end]
        day_findings = [f for f in findings if day_ms <= int(f[0]) < day_end]
        counts: dict[str, int] = defaultdict(int)
        for _, _, check, _ in day_findings:
            counts[check] += 1
        days.append(
            DayReport(
                day=ms_to_datetime(day_ms).date(),
                hours_checked=sum(1 for r in day_runs if int(r[0]) in due),
                hours_due=len(due),
                expected_minutes=sum(int(r[1]) for r in day_runs),
                present_minutes=sum(int(r[2]) for r in day_runs),
                option_rows=sum(int(r[3]) for r in day_runs),
                skip_periods=merge_periods(
                    (int(s), int(e), c) for s, e, c, skip in day_findings if skip
                ),
                counts=dict(counts),
            )
        )
        day_ms = day_end
    return days


def format_gap_report(days: Sequence[DayReport]) -> str:
    """The gap report as Markdown: one row per day, then every skipped period."""
    lines = [
        "| UTC day | hours checked | minutes recorded / expected | option rows | skipped minutes "
        "| longest gap | findings |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for d in days:
        counts = ", ".join(f"{k} {v}" for k, v in sorted(d.counts.items())) or "none"
        lines.append(
            f"| {d.day} | {d.hours_checked}/{d.hours_due}{'' if d.covered else ' (INCOMPLETE)'} "
            f"| {d.present_minutes}/{d.expected_minutes} | {d.option_rows:,} | {d.skipped_minutes} "
            f"| {d.longest_gap_minutes} min | {counts} |"
        )
    periods = [p for d in days for p in d.skip_periods]
    lines.append("")
    if not periods:
        lines.append("Skipped periods: none.")
    else:
        lines.append("Skipped periods (backtests leave these minutes out):")
        lines.append("")
        for start, end, checks in periods:
            lines.append(
                f"- {ms_to_datetime(start):%Y-%m-%d %H:%M} to {ms_to_datetime(end):%H:%M} UTC "
                f"({(end - start) // MS_PER_MINUTE} min): {checks}"
            )
    return "\n".join(lines)
