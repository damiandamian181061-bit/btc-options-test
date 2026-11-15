"""The jobs scheduler, the backup ping, the restore-test guard and the laptop sync."""

from datetime import date
from pathlib import Path

import psycopg
import pytest

from optlab.config import Settings
from optlab.domain.clock import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE
from optlab.market_data import jobs as jobs_module
from optlab.market_data.backup import back_up_day
from optlab.market_data.export import EXPORT_DIR, manifest_path
from optlab.market_data.jobs import (
    Jobs,
    JobSettings,
    main,
    next_backup_ms,
    next_check_ms,
    run_scheduler,
)
from optlab.market_data.quality import load_quality_config, run_checks
from optlab.market_data.r2 import R2NotConfiguredError
from optlab.market_data.sync import available_days, sync_day
from tests.conftest import REPO_ROOT
from tests.market_data.synthetic import populate, utc_ms
from tests.market_data.test_backup import local_r2, write_raw

M = MS_PER_MINUTE
DAY = date(2026, 11, 13)


def test_checks_run_at_five_past_and_the_backup_at_half_past_midnight() -> None:
    t = utc_ms(2026, 11, 14, 10, 3)
    assert next_check_ms(t) == utc_ms(2026, 11, 14, 10, 5)
    assert next_check_ms(utc_ms(2026, 11, 14, 10, 5)) == utc_ms(2026, 11, 14, 11, 5)
    assert next_backup_ms(t) == utc_ms(2026, 11, 15, 0, 30)
    assert next_backup_ms(utc_ms(2026, 11, 15, 0, 10)) == utc_ms(2026, 11, 15, 0, 30)


class FakeJobs:
    def __init__(self, start_ms: int, results: list[bool]) -> None:
        self.now = start_ms
        self.results = results
        self.log: list[tuple[str, int]] = []

    def clock(self) -> int:
        return self.now

    def checks(self, hours: object = None) -> int:
        self.log.append(("checks", self.now))
        return 0

    def backup(self, day: object = None) -> bool:
        self.log.append(("backup", self.now))
        return self.results.pop(0) if self.results else True


def test_the_scheduler_catches_up_then_keeps_time_and_retries_failed_backups() -> None:
    start = utc_ms(2026, 11, 14, 22, 17)
    fake = FakeJobs(start, results=[True, False, True])
    stop_at = start + 2 * MS_PER_DAY

    def sleep(seconds: float) -> None:
        fake.now += round(seconds * 1000)
        if fake.now >= stop_at:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_scheduler(fake, sleep=sleep)  # type: ignore[arg-type]
    backups = [t for name, t in fake.log if name == "backup"]
    checks = [t for name, t in fake.log if name == "checks"]
    assert backups[0] == start  # catch-up at start
    assert backups[1] == utc_ms(2026, 11, 15, 0, 30)  # fails...
    assert backups[2] == utc_ms(2026, 11, 15, 1, 30)  # ...retried an hour later
    assert backups[3] == utc_ms(2026, 11, 16, 0, 30)
    assert checks[0] == start
    assert checks[1:3] == [utc_ms(2026, 11, 14, 23, 5), utc_ms(2026, 11, 15, 0, 5)]
    assert all(t % MS_PER_HOUR == 5 * M for t in checks[1:])


@pytest.mark.db
def test_backup_pings_success_with_a_summary_and_failure_with_the_error(
    database_url: str, tmp_path: Path, rclone_binary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    populate(database_url, utc_ms(2026, 11, 13, 22), utc_ms(2026, 11, 14, 1))
    pings: list[tuple[bool, str]] = []
    monkeypatch.setattr(jobs_module, "_ping", lambda _s, ok, msg: pings.append((ok, msg)))
    rclone, _ = local_r2(tmp_path, rclone_binary)
    monkeypatch.setattr(jobs_module, "Rclone", lambda _config: rclone)
    monkeypatch.setattr(jobs_module, "r2_config", lambda _settings: rclone.config)
    data = tmp_path / "data"
    write_raw(data, DAY)
    jobs = Jobs(
        Settings(_env_file=None, database_url=database_url),  # type: ignore[arg-type]
        JobSettings(data_dir=data, config_dir=REPO_ROOT / "configs"),
        clock=lambda: utc_ms(2026, 11, 14, 1, 31),
        boot_time=lambda: None,
    )
    assert jobs.backup()
    ok, message = pings[-1]
    assert ok
    assert "2026-11-13:" in message
    assert "dataset version" in message

    assert jobs.backup()
    assert pings[-1] == (True, "nothing new: every finished day is in R2")

    def unconfigured(_settings: Settings) -> None:
        raise R2NotConfiguredError("R2 is not configured: R2_ENDPOINT not set")

    monkeypatch.setattr(jobs_module, "r2_config", unconfigured)
    assert not jobs.backup()
    assert pings[-1][0] is False
    assert "R2_ENDPOINT not set" in pings[-1][1]


@pytest.mark.db
def test_each_checks_run_records_the_server_boot_once(database_url: str) -> None:
    populate(database_url, utc_ms(2026, 11, 13, 22), utc_ms(2026, 11, 14, 1))
    boot = utc_ms(2026, 11, 13, 22, 30) + 40_000
    jobs = Jobs(
        Settings(_env_file=None, database_url=database_url),  # type: ignore[arg-type]
        JobSettings(config_dir=REPO_ROOT / "configs"),
        clock=lambda: utc_ms(2026, 11, 14, 1, 31),
        boot_time=lambda: boot,
    )
    jobs.checks()
    jobs.checks()
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT boot_ms FROM host_boots").fetchall() == [(boot,)]
        labels = conn.execute(
            "SELECT count(*) FROM data_quality WHERE check_name = 'server_reboot'"
        ).fetchone()
    assert labels == (1,)  # the boot lost no minute: a one-minute label that skips nothing


def test_restore_test_refuses_outside_its_throwaway_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPTLAB_RESTORE_TARGET", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db:5432/optlab")
    with pytest.raises(SystemExit, match="throwaway"):
        main(["restore-test"])


@pytest.mark.db
def test_sync_pulls_only_what_is_missing_or_wrong(
    database_url: str, tmp_path: Path, rclone_binary: str
) -> None:
    populate(database_url, utc_ms(2026, 11, 13, 22), utc_ms(2026, 11, 14, 1))
    with psycopg.connect(database_url) as conn:
        run_checks(conn, load_quality_config(REPO_ROOT / "configs"), utc_ms(2026, 11, 14, 2))
    rclone, _ = local_r2(tmp_path, rclone_binary)
    write_raw(tmp_path / "server", DAY)
    back_up_day(database_url, DAY, tmp_path / "server", rclone, now_ms=utc_ms(2026, 11, 14, 2))
    laptop = tmp_path / "laptop"

    assert available_days(rclone) == [DAY]
    first = sync_day(rclone, DAY, laptop, raw=True)
    assert first.downloaded == first.files
    assert first.raw_files == 2
    assert manifest_path(laptop / EXPORT_DIR, DAY).is_file()
    assert sync_day(rclone, DAY, laptop).downloaded == 0

    damaged = next((laptop / EXPORT_DIR / "option_quotes").rglob("*.parquet"))
    damaged.write_bytes(b"not parquet")
    assert sync_day(rclone, DAY, laptop).downloaded == 1
    assert damaged.read_bytes()[:4] == b"PAR1"
