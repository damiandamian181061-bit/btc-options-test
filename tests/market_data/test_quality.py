"""Hourly data-quality checks and the gap report."""

from datetime import date

import psycopg
import pytest

from optlab.domain.clock import MS_PER_HOUR, MS_PER_MINUTE, ms_to_datetime
from optlab.market_data import quality
from optlab.market_data.quality import (
    QualityConfig,
    format_gap_report,
    gap_report,
    load_quality_config,
    merge_periods,
    minute_runs,
    run_checks,
    settled_hours,
)
from tests.conftest import REPO_ROOT
from tests.market_data.synthetic import OPTIONS, populate, utc_ms

M = MS_PER_MINUTE
START = utc_ms(2026, 11, 13, 22)
END = utc_ms(2026, 11, 14, 1)


def config() -> QualityConfig:
    return load_quality_config(REPO_ROOT / "configs")


def test_the_config_file_loads_and_its_version_follows_every_value() -> None:
    c = config()
    assert c.partial_snapshot_fraction == 0.98
    assert c.indexes == ("btc_usd", "btc_usdc")
    changed = c.model_copy(update={"iv_jump": 0.2})
    assert changed.version() != c.version()
    assert c.version().startswith(quality.CHECKS_CODE_VERSION + "-")


def test_minute_runs_and_merged_periods() -> None:
    assert minute_runs([]) == []
    assert minute_runs([5 * M, 3 * M, 4 * M, 9 * M, 4 * M]) == [
        (3 * M, 6 * M, 3),
        (9 * M, 10 * M, 1),
    ]
    joined = merge_periods([(0, 60 * M, "a"), (60 * M, 70 * M, "a"), (65 * M, 80 * M, "b")])
    assert joined == [(0, 80 * M, "a, b")]
    assert merge_periods([(0, M, "a"), (2 * M, 3 * M, "a")]) == [(0, M, "a"), (2 * M, 3 * M, "a")]


def test_an_hour_is_checked_once_written_past_or_half_an_hour_late() -> None:
    h = utc_ms(2026, 11, 14, 10)
    assert settled_hours(h + 5 * M, h + MS_PER_HOUR + 2 * M, latest_ms=h + 59 * M) == []
    assert settled_hours(h + 5 * M, h + MS_PER_HOUR + 2 * M, latest_ms=h + 60 * M) == [h]
    # Recorder stopped: nothing after the hour, but 30 minutes have passed.
    assert settled_hours(h + 5 * M, h + MS_PER_HOUR + 30 * M, latest_ms=h + 20 * M) == [h]


def _damage(url: str) -> None:
    """Break the synthetic market in known ways."""
    far = next(o for o in OPTIONS if o.expiry_ms > utc_ms(2026, 11, 21))
    other = OPTIONS[3]
    t = ms_to_datetime
    with psycopg.connect(url) as conn:
        # 22:10 and 22:11 missing; 22:30 has only half its rows.
        conn.execute(
            "DELETE FROM option_quotes WHERE ts IN (%s, %s)",
            (t(START + 10 * M), t(START + 11 * M)),
        )
        conn.execute(
            "DELETE FROM option_quotes WHERE ts = %s AND instrument_name LIKE 'BTC_USDC%%'",
            (t(START + 30 * M),),
        )
        # Crossed for 3 minutes, an empty book, a stale quote and an IV spike.
        conn.execute(
            "UPDATE option_quotes SET bid = ask + 0.001 WHERE instrument_name = %s "
            "AND ts >= %s AND ts < %s",
            (far.name, t(START + 40 * M), t(START + 43 * M)),
        )
        conn.execute(
            "UPDATE option_quotes SET bid = NULL, ask = NULL "
            "WHERE instrument_name = %s AND ts = %s",
            (other.name, t(START + 75 * M)),
        )
        conn.execute(
            "UPDATE option_quotes SET exchange_ts_ms = exchange_ts_ms - 200000 "
            "WHERE instrument_name = %s AND ts = %s",
            (other.name, t(START + 80 * M)),
        )
        conn.execute(
            "UPDATE option_quotes SET mark_iv = 0.95 WHERE instrument_name = %s AND ts = %s",
            (far.name, t(START + 90 * M)),
        )
        # btc_usd missing at 00:05; btc_usdc frozen from 00:20 to 00:29.
        conn.execute(
            "DELETE FROM underlying_ticks WHERE instrument_name = 'btc_usd' AND ts = %s",
            (t(START + 125 * M),),
        )
        conn.execute(
            "UPDATE underlying_ticks SET index_price = 91000 WHERE instrument_name = 'btc_usdc' "
            "AND ts >= %s AND ts < %s",
            (t(START + 140 * M), t(START + 150 * M)),
        )


@pytest.mark.db
def test_checks_find_each_problem_and_mark_gaps_to_skip(database_url: str) -> None:
    populate(database_url, START, END)
    _damage(database_url)
    far = next(o for o in OPTIONS if o.expiry_ms > utc_ms(2026, 11, 21))
    other = OPTIONS[3]
    now = END + 31 * M
    with psycopg.connect(database_url) as conn:
        results = run_checks(conn, config(), now)
        assert [r.hour_ms for r in results] == [START, START + MS_PER_HOUR, START + 2 * MS_PER_HOUR]
        assert run_checks(conn, config(), now) == []  # each hour once

        rows = conn.execute(
            "SELECT check_name, subject, utc_ms(start_ts), utc_ms(end_ts), minutes, skip "
            "FROM data_quality ORDER BY start_ts, check_name, subject"
        ).fetchall()
        runs = conn.execute(
            "SELECT utc_ms(hour_ts), expected_minutes, present_minutes, findings, skipped_minutes "
            "FROM data_quality_runs ORDER BY 1"
        ).fetchall()

    found = {(c, s, (a - START) // M, (b - START) // M, n, skip) for c, s, a, b, n, skip in rows}
    assert found == {
        ("missing_snapshot", "", 10, 12, 2, True),
        ("partial_snapshot", "", 30, 31, 1, True),
        ("crossed_book", far.name, 40, 43, 3, False),
        ("empty_book", other.name, 75, 76, 1, False),
        ("stale_quote", other.name, 80, 81, 1, False),
        ("iv_outlier", far.name, 90, 92, 2, False),  # the jump up and the jump back
        ("stale_index", "btc_usd", 125, 126, 1, True),
        ("stale_index", "btc_usdc", 144, 150, 6, True),  # from the 5th unchanged minute
    }
    assert runs == [
        (START, 60, 58, 3, 3),
        (START + MS_PER_HOUR, 60, 60, 3, 0),
        (START + 2 * MS_PER_HOUR, 60, 60, 2, 7),
    ]


@pytest.mark.db
def test_the_gap_report_covers_every_recorded_day(database_url: str) -> None:
    populate(database_url, START, END)
    _damage(database_url)
    now = END + 31 * M
    with psycopg.connect(database_url) as conn:
        assert all(not d.covered for d in gap_report(conn, now))
        run_checks(conn, config(), now)
        days = gap_report(conn, now)
        text = format_gap_report(days)

    assert [d.day for d in days] == [date(2026, 11, 13), date(2026, 11, 14)]
    first, second = days
    assert (first.hours_checked, first.hours_due) == (2, 2)
    assert (second.hours_checked, second.hours_due) == (1, 1)
    assert first.missing_minutes == 2
    assert first.skipped_minutes == 3
    assert first.longest_gap_minutes == 3  # 22:09 to 22:12
    assert second.skipped_minutes == 7
    assert "2026-11-13 22:10 to 22:12 UTC (2 min): missing_snapshot" in text
    assert "INCOMPLETE" not in text


@pytest.mark.db
def test_a_stopped_recorder_shows_up_as_missing_minutes(database_url: str) -> None:
    populate(database_url, START, START + 20 * M)  # stopped at 22:20
    with psycopg.connect(database_url) as conn:
        results = run_checks(conn, config(), START + MS_PER_HOUR + 30 * M)
        assert len(results) == 1
        missing = [f for f in results[0].findings if f.check_name == "missing_snapshot"]
        assert [(f.start_ms - START) // M for f in missing] == [20]
        assert missing[0].minutes == 40
        assert results[0].skipped_minutes == 40


@pytest.mark.db
def test_a_recheck_replaces_an_hours_results(database_url: str) -> None:
    populate(database_url, START, END)
    with psycopg.connect(database_url) as conn:
        run_checks(conn, config(), END + 31 * M)
        conn.execute("DELETE FROM option_quotes WHERE ts = %s", (ms_to_datetime(START + 5 * M),))
        run_checks(conn, config(), END + 31 * M, hours=[START])
        counts = conn.execute(
            "SELECT count(*) FILTER (WHERE check_name = 'missing_snapshot'), "
            "(SELECT count(*) FROM data_quality_runs) FROM data_quality"
        ).fetchone()
    assert counts == (1, 3)
