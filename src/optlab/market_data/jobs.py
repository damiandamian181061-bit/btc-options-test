"""``optlab-data``: the data platform's jobs, by hand or on a schedule.

    optlab-data scheduler            the jobs service: checks hourly, backup nightly
    optlab-data checks [--hour ISO]  run the data checks now (--hour re-checks one hour)
    optlab-data gap-report           every recorded day: coverage, gaps, skipped periods
    optlab-data backup [--day ISO]   export, upload and verify; prune; ping HC_PING_URL_BACKUP
    optlab-data restore-test         rebuild a throwaway database from R2 and compare
    optlab-data sync DAY... | --from ISO --to ISO | --list   pull days from R2 (laptop)
    optlab-data notify-failure MESSAGE   failure ping to HC_PING_URL_BACKUP (infra/restore-test)

Schedule (UTC): data checks at 5 minutes past every hour. The backup at 00:30: it first
brings the data checks up to date, then backs up every finished day not yet verified in R2,
removes the server's copies older than 7 days, and pings HC_PING_URL_BACKUP. A failed backup
sends a failure ping and is retried every hour until it works. Both also run once at start-up,
so a restart catches up on anything missed.

Every command loads ``optlab.config.Settings`` first, so none runs with LIVE_TRADING=true.
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
from pydantic_settings import BaseSettings, SettingsConfigDict

from optlab.config import Settings
from optlab.domain.clock import (
    MS_PER_DAY,
    MS_PER_HOUR,
    MS_PER_MINUTE,
    datetime_to_ms,
    floor_ms,
    ms_to_datetime,
)
from optlab.market_data.backup import back_up_day, days_to_back_up, prune_local
from optlab.market_data.health import HealthchecksPinger
from optlab.market_data.quality import (
    format_gap_report,
    gap_report,
    load_quality_config,
    run_checks,
)
from optlab.market_data.r2 import Rclone, RcloneError, r2_config
from optlab.market_data.restore import restore_test
from optlab.market_data.sync import available_days, sync_day

logger = logging.getLogger("optlab.jobs")

CHECK_MINUTE_MS = 5 * MS_PER_MINUTE
BACKUP_TIME_MS = 30 * MS_PER_MINUTE
RETRY_MS = MS_PER_HOUR


class JobSettings(BaseSettings):
    """Non-secret settings for the jobs (``OPTLAB_`` prefix)."""

    model_config = SettingsConfigDict(env_prefix="OPTLAB_", extra="ignore", frozen=True)

    data_dir: Path = Path("data")
    config_dir: Path = Path("configs")
    alembic_ini: Path = Path("infra/alembic.ini")
    restore_target: str = ""  # must be "throwaway" for restore-test
    log_level: str = "INFO"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def next_check_ms(after_ms: int) -> int:
    t = floor_ms(after_ms, MS_PER_HOUR) + CHECK_MINUTE_MS
    return t if t > after_ms else t + MS_PER_HOUR


def next_backup_ms(after_ms: int) -> int:
    t = floor_ms(after_ms, MS_PER_DAY) + BACKUP_TIME_MS
    return t if t > after_ms else t + MS_PER_DAY


def _database_url(settings: Settings) -> str:
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is not set")
    return settings.database_url.get_secret_value()


def _ping(settings: Settings, ok: bool, message: str) -> None:
    pinger = HealthchecksPinger("backup check", settings.hc_ping_url_backup)
    asyncio.run(pinger.success(message) if ok else pinger.fail(message))


@dataclass
class Jobs:
    settings: Settings
    job: JobSettings
    clock: Callable[[], int] = now_ms

    @property
    def url(self) -> str:
        return _database_url(self.settings)

    def checks(self, hours: Sequence[int] | None = None) -> int:
        config = load_quality_config(self.job.config_dir)
        with psycopg.connect(self.url, application_name="optlab-checks") as conn:
            return len(run_checks(conn, config, self.clock(), hours=hours))

    def backup(self, day: date | None = None) -> bool:
        """Back up every due day (or ``day``), prune, ping. True if everything worked."""
        now = self.clock()
        today = ms_to_datetime(now).date()
        try:
            self.checks()
            rclone = Rclone(r2_config(self.settings))
            with psycopg.connect(self.url) as conn:
                due = [day] if day else days_to_back_up(conn, self.job.data_dir / "raw", today)
            done = [
                back_up_day(self.url, d, self.job.data_dir, rclone, now_ms=self.clock())
                for d in due
            ]
            pruned = prune_local(self.url, self.job.data_dir, rclone, today, now_ms=self.clock())
        except Exception as exc:
            text = str(exc) if isinstance(exc, RcloneError) else f"{type(exc).__name__}: {exc}"
            logger.exception("backup failed")
            _ping(self.settings, False, f"backup failed: {text}"[:2000])
            return False
        lines = [d.describe() for d in done] or ["nothing new: every finished day is in R2"]
        if pruned:
            lines.append("server copies removed (verified in R2): " + ", ".join(map(str, pruned)))
        _ping(self.settings, True, "\n".join(lines))
        for line in lines:
            logger.info("backup: %s", line)
        return True


def run_scheduler(jobs: Jobs, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """Run forever: catch up once, then checks hourly and the backup nightly."""
    next_check = jobs.clock()
    next_backup = jobs.clock()
    while True:
        now = jobs.clock()
        if now >= next_check:
            try:
                jobs.checks()
            except Exception:
                logger.exception("data checks failed; trying again next hour")
            next_check = next_check_ms(jobs.clock())
        if now >= next_backup:
            ok = jobs.backup()
            next_backup = next_backup_ms(jobs.clock()) if ok else jobs.clock() + RETRY_MS
        wait = min(next_check, next_backup) - jobs.clock()
        if wait > 0:
            sleep(min(wait / 1000, 60.0))


# ------------------------------------------------------------------------------------ CLI


def _day(text: str) -> date:
    return date.fromisoformat(text)


def _utc(text: str) -> datetime:
    """An ISO time; without an offset it is taken as UTC."""
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="optlab-data", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scheduler", help="run the checks hourly and the backup nightly")
    checks = sub.add_parser("checks", help="run the data checks now")
    checks.add_argument("--hour", action="append", default=[], help="re-check this UTC hour")
    sub.add_parser("gap-report", help="print the gap report")
    backup = sub.add_parser("backup", help="back up finished days to R2 now")
    backup.add_argument("--day", type=_day, help="only this UTC day (YYYY-MM-DD)")
    restore = sub.add_parser("restore-test", help="restore a day into a throwaway database")
    restore.add_argument("--day", type=_day, help="UTC day (default: the latest in R2)")
    sync = sub.add_parser("sync", help="pull days from R2 into data/ (laptop)")
    sync.add_argument("days", nargs="*", type=_day)
    sync.add_argument("--from", dest="first", type=_day)
    sync.add_argument("--to", dest="last", type=_day)
    sync.add_argument("--raw", action="store_true", help="also pull the raw stream files")
    sync.add_argument("--list", action="store_true", help="list the days in R2")
    notify = sub.add_parser("notify-failure", help="send a failure ping for the backup check")
    notify.add_argument("message")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    settings = Settings()  # refuses LIVE_TRADING=true
    job = JobSettings()
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=job.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.Formatter.converter = time.gmtime
    jobs = Jobs(settings, job)

    if args.command == "scheduler":
        signal.signal(signal.SIGTERM, signal.default_int_handler)  # stop like Ctrl-C
        logger.info("jobs scheduler starting: checks at :05 hourly, backup at 00:30 UTC")
        try:
            run_scheduler(jobs)
        except KeyboardInterrupt:
            logger.info("stopped by signal")
        return 0
    if args.command == "checks":
        hours = [floor_ms(datetime_to_ms(_utc(h)), MS_PER_HOUR) for h in args.hour]
        print(f"checked {jobs.checks(hours or None)} hours")
        return 0
    if args.command == "gap-report":
        with psycopg.connect(jobs.url, options="-c default_transaction_read_only=on") as conn:
            print(format_gap_report(gap_report(conn, jobs.clock())))
        return 0
    if args.command == "backup":
        return 0 if jobs.backup(args.day) else 1
    if args.command == "restore-test":
        return _restore_test(jobs, args.day)
    if args.command == "sync":
        return _sync(settings, job, args)
    if args.command == "notify-failure":
        _ping(settings, False, args.message)
        return 0
    raise AssertionError(args.command)  # pragma: no cover


def _restore_test(jobs: Jobs, day: date | None) -> int:
    if jobs.job.restore_target != "throwaway":
        raise SystemExit(
            "restore-test runs only in its throwaway container (infra/restore-test), where "
            "OPTLAB_RESTORE_TARGET=throwaway"
        )
    rclone = Rclone(r2_config(jobs.settings))
    try:
        report = restore_test(jobs.url, rclone, alembic_ini=jobs.job.alembic_ini, day=day)
    except Exception as exc:
        logger.exception("restore test could not run")
        _ping(jobs.settings, False, f"restore test could not run: {type(exc).__name__}: {exc}")
        return 1
    print(report.format())
    if not report.ok:
        _ping(jobs.settings, False, report.format()[:2000])
        return 1
    return 0


def _sync(settings: Settings, job: JobSettings, args: argparse.Namespace) -> int:
    rclone = Rclone(r2_config(settings))
    days = available_days(rclone)
    if args.list:
        print("\n".join(d.isoformat() for d in days) or "no days in R2 yet")
        return 0
    wanted = list(args.days)
    if args.first or args.last:
        first, last = args.first or days[0], args.last or days[-1]
        wanted += [d for d in days if first <= d <= last]
    if not wanted:
        raise SystemExit("name the days to sync (or --from/--to, or --list)")
    for d in sorted(set(wanted)):
        r = sync_day(rclone, d, job.data_dir, raw=args.raw)
        raw = f", {r.raw_files} raw files" if args.raw else ""
        print(
            f"{d}: {r.files} files ({r.downloaded} downloaded), {r.bytes / 1e6:.1f} MB{raw}, "
            f"dataset version {r.dataset_version[:12]}"
        )
    return 0
