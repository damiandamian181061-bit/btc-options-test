"""Nightly export, R2 upload and verification, local pruning, and the restore test.

R2 is stood in for by a local folder through the real rclone binary (its ``local`` backend),
so the exact rclone commands the server runs are exercised. The stand-in answers reads of
missing objects the way R2 does (see ``R2Like``), not the way a local folder does.
"""

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import SecretStr

from optlab.config import Settings
from optlab.market_data.backup import (
    BackupError,
    back_up_day,
    days_to_back_up,
    prune_local,
)
from optlab.market_data.export import (
    EXPORT_DIR,
    TABLE_SPECS,
    ExportError,
    content_checksum,
    day_bounds,
    export_day,
    manifest_files,
    read_manifest,
    session_settings,
)
from optlab.market_data.quality import load_quality_config, run_checks
from optlab.market_data.r2 import (
    R2NotConfiguredError,
    Rclone,
    RcloneError,
    RemoteConfig,
    r2_config,
)
from optlab.market_data.raw_stream import QUOTE_SCHEMA
from optlab.market_data.restore import RestoreRefusedError, restore_test
from tests.conftest import ALEMBIC_INI, REPO_ROOT
from tests.market_data.synthetic import OPTIONS, populate, utc_ms

DAY = date(2026, 11, 13)
START = utc_ms(2026, 11, 13, 22)
END = utc_ms(2026, 11, 14, 1)
NOW = utc_ms(2026, 11, 14, 1, 31)


class R2Like:
    """Runs the real rclone against the local bucket folder, but reads of a missing object or
    folder behave as on R2: ``rclone cat`` or ``lsf`` of a key that isn't there exits 0 and
    prints nothing (an S3 store has no folders, so a missing key reads as an empty one). The
    local backend says "not found" instead, which hid the failed 14 Nov 2026 nightly backup.
    """

    def __init__(self, bucket: Path) -> None:
        self.bucket = bucket
        self.empty_reads: list[str] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        verb, target = command[3], command[-1]  # [binary, "--config", "", verb, ..., target]
        if verb in ("cat", "lsf") and target.startswith("r2:"):
            path = Path(target.removeprefix("r2:"))
            if path.is_relative_to(self.bucket) and not path.exists():
                self.empty_reads.append(verb)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.run(command, **kwargs)  # noqa: S603 - the rclone the tests run


def local_r2(tmp_path: Path, binary: str) -> tuple[Rclone, Path]:
    bucket = tmp_path / "bucket"
    bucket.mkdir()
    config = RemoteConfig(
        bucket=str(bucket), env={"RCLONE_CONFIG_R2_TYPE": "local"}, secrets=("s3cr3t",)
    )
    return Rclone(config, binary=binary, runner=R2Like(bucket)), bucket


def write_raw(data_dir: Path, day: date, hours: int = 2) -> list[Path]:
    paths = []
    for h in range(hours):
        folder = data_dir / "raw" / "quotes" / day.isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"quotes_{day.isoformat()}T{22 + h:02d}_run.parquet"
        table = pa.Table.from_pylist(
            [
                {
                    "recv_ts_ms": START + h,
                    "exchange_ts_ms": START,
                    "instrument_name": "BTC-X",
                    "bid": 0.01,
                    "bid_size": 1.0,
                    "ask": 0.02,
                    "ask_size": 1.0,
                }
            ],
            schema=QUOTE_SCHEMA,
        )
        pq.write_table(table, path, compression="zstd")
        paths.append(path)
    return paths


def prepared(database_url: str) -> None:
    populate(database_url, START, END)
    with psycopg.connect(database_url) as conn:
        run_checks(conn, load_quality_config(REPO_ROOT / "configs"), NOW)


# ------------------------------------------------------------------------------------ export


@pytest.mark.db
def test_export_writes_partitioned_files_and_a_versioned_manifest(
    database_url: str, tmp_path: Path
) -> None:
    prepared(database_url)
    root = tmp_path / EXPORT_DIR
    manifest = export_day(database_url, DAY, root, now_ms=NOW)

    tables = manifest["tables"]
    assert set(tables) == set(TABLE_SPECS)
    quotes = tables["option_quotes"]
    assert quotes["rows"] == 120 * len(OPTIONS)  # 22:00 to 23:59, every option
    assert [f["path"] for f in quotes["files"]] == [
        "option_quotes/date=2026-11-13/expiry=2026-11-20/part-0.parquet",
        "option_quotes/date=2026-11-13/expiry=2026-11-27/part-0.parquet",
    ]
    assert tables["underlying_ticks"]["rows"] == 120 * 3
    assert tables["instruments"]["rows"] == len(OPTIONS) + 1
    assert tables["delivery_prices"]["rows"] == 2  # only those recorded on the 13th
    assert tables["connection_events"]["rows"] == 2
    assert tables["data_quality_runs"]["rows"] == 2
    status = tables["recorder_status"]  # no rows: an empty file keeps the schema
    assert (status["rows"], status["checksum"]) == (0, "md5-60:0:0")
    assert [f["rows"] for f in status["files"]] == [0]
    assert pq.read_schema(root / status["files"][0]["path"]).names[:2] == ["ts", "run_id"]
    assert read_manifest((root / "manifests" / "2026-11-13.json").read_bytes()) == manifest

    # Values survive exactly: sorted by instrument then time, decimals kept as text.
    first = pq.read_table(root / quotes["files"][0]["path"])
    names = first.column("instrument_name").to_pylist()
    assert names == sorted(names)
    assert first.schema.field("ts").type == pa.timestamp("us", tz="UTC")
    prices = pq.read_table(root / tables["delivery_prices"]["files"][0]["path"])
    assert sorted(prices.column("delivery_price").to_pylist()) == ["90120.10", "90123.4567"]

    # The same data exports to the same bytes, so the same dataset version.
    again = export_day(database_url, DAY, tmp_path / "again", now_ms=NOW + 1)
    assert again["dataset_version"] == manifest["dataset_version"]


@pytest.mark.db
def test_the_checksum_changes_with_any_value(database_url: str) -> None:
    prepared(database_url)
    with psycopg.connect(database_url) as conn:
        session_settings(conn)
        params = day_bounds(DAY)
        before = content_checksum(conn, TABLE_SPECS["option_quotes"], params)
        conn.execute(
            "UPDATE option_quotes SET bid_size = bid_size + 1e-9 "
            "WHERE ts = (SELECT min(ts) FROM option_quotes) AND instrument_name = %s",
            (OPTIONS[0].name,),
        )
        after = content_checksum(conn, TABLE_SPECS["option_quotes"], params)
    assert before[0] == after[0]
    assert before[1] != after[1]


def test_a_tampered_manifest_is_rejected(tmp_path: Path) -> None:
    from optlab.market_data.export import dataset_version

    manifest: dict[str, Any] = {
        "format": "optlab-export-v1",
        "day": "2026-11-13",
        "schema_revision": "0002",
        "tables": {"option_quotes": {"rows": 1, "checksum": "x", "files": []}},
    }

    manifest["dataset_version"] = dataset_version(manifest)
    assert read_manifest(json.dumps(manifest).encode())["day"] == "2026-11-13"
    manifest["tables"]["option_quotes"]["rows"] = 2
    with pytest.raises(ExportError, match="dataset version does not match"):
        read_manifest(json.dumps(manifest).encode())


def test_raw_files_still_being_written_are_refused(tmp_path: Path) -> None:
    from optlab.market_data.export import raw_manifest

    write_raw(tmp_path, DAY)
    (tmp_path / "raw" / "quotes" / DAY.isoformat() / "q.parquet.inprogress").write_bytes(b"x")
    with pytest.raises(ExportError, match="still being written"):
        raw_manifest(tmp_path / "raw", DAY, now_ms=NOW)


# ------------------------------------------------------------------------------------ R2


def test_the_r2_remote_comes_from_settings_and_is_never_shown() -> None:
    settings = Settings(
        _env_file=None,
        r2_access_key_id=SecretStr("AKIDEXAMPLE"),
        r2_secret_access_key=SecretStr("very-secret"),
        r2_endpoint=SecretStr("https://fake-account-id.r2.cloudflarestorage.com"),
        r2_bucket="btc-options-backup",
    )
    config = r2_config(settings)
    assert config.env["RCLONE_CONFIG_R2_TYPE"] == "s3"
    assert config.env["RCLONE_CONFIG_R2_PROVIDER"] == "Cloudflare"
    assert config.env["RCLONE_CONFIG_R2_NO_CHECK_BUCKET"] == "true"
    assert config.target("raw/quotes") == "r2:btc-options-backup/raw/quotes"
    assert "very-secret" not in repr(config)
    assert "fake-account-id" not in repr(config)

    def failing(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert "very-secret" not in " ".join(command)  # never on the command line
        assert "--config" in command
        message = (
            "Failed: dial tcp: lookup fake-account-id.r2.cloudflarestorage.com"
            " key AKIDEXAMPLE secret very-secret"
        )
        return subprocess.CompletedProcess(command, 1, "", message)

    with pytest.raises(RcloneError) as info:
        Rclone(config, runner=failing).run("lsf", config.target())
    text = str(info.value)
    for secret in ("very-secret", "AKIDEXAMPLE", "fake-account-id", "cloudflarestorage"):
        assert secret not in text
    assert "[redacted]" in text


def test_r2_settings_must_all_be_present() -> None:
    with pytest.raises(R2NotConfiguredError, match="R2_SECRET_ACCESS_KEY, R2_ENDPOINT"):
        r2_config(Settings(_env_file=None, r2_access_key_id=SecretStr("k"), r2_bucket="b"))


def test_a_manifest_exists_only_if_listed_never_because_cat_printed_nothing(
    tmp_path: Path, rclone_binary: str
) -> None:
    rclone, bucket = local_r2(tmp_path, rclone_binary)
    key = "exports/v1/manifests/2026-11-14.json"
    # As on R2: reading a missing key succeeds and prints nothing.
    assert rclone.run("cat", rclone.config.target(key)).stdout == ""
    assert rclone.get(key) is None

    (bucket / "exports/v1/manifests").mkdir(parents=True)
    (bucket / key).write_bytes(b'{"dataset_version": "abc"}\n')
    assert rclone.get(key) == b'{"dataset_version": "abc"}\n'

    # Listed but empty: an error, never taken as "no manifest".
    (bucket / "exports/v1/manifests/2026-11-15.json").write_bytes(b"")
    with pytest.raises(RcloneError, match="printed nothing"):
        rclone.get("exports/v1/manifests/2026-11-15.json")


@pytest.mark.db
def test_back_up_uploads_verifies_and_records_the_day(
    database_url: str, tmp_path: Path, rclone_binary: str
) -> None:
    prepared(database_url)
    data = tmp_path / "data"
    write_raw(data, DAY)
    rclone, bucket = local_r2(tmp_path, rclone_binary)
    with psycopg.connect(database_url) as conn:
        assert days_to_back_up(conn, data / "raw", date(2026, 11, 14)) == [DAY]

    result = back_up_day(database_url, DAY, data, rclone, now_ms=NOW)

    assert (bucket / "raw/quotes/2026-11-13/quotes_2026-11-13T22_run.parquet").is_file()
    assert (bucket / "raw/manifests/2026-11-13.json").is_file()
    manifest = read_manifest((bucket / "exports/v1/manifests/2026-11-13.json").read_bytes())
    for f in manifest_files(manifest):
        assert (bucket / "exports/v1" / f["path"]).stat().st_size == f["bytes"]
    assert result.dataset_version == manifest["dataset_version"]
    assert result.raw_files == 2
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT dataset_version, uploaded_ms, rows FROM dataset_exports WHERE day = %s", (DAY,)
        ).fetchone()
        assert row == (manifest["dataset_version"], NOW, result.rows)
        assert days_to_back_up(conn, data / "raw", date(2026, 11, 14)) == []


@pytest.mark.db
def test_local_copies_are_removed_after_7_days_only_once_found_in_r2(
    database_url: str, tmp_path: Path, rclone_binary: str
) -> None:
    prepared(database_url)
    data = tmp_path / "data"
    raw_files = write_raw(data, DAY)
    rclone, bucket = local_r2(tmp_path, rclone_binary)
    back_up_day(database_url, DAY, data, rclone, now_ms=NOW)
    export_file = next((data / EXPORT_DIR / "option_quotes").rglob("*.parquet"))

    # Day 7: still kept.
    assert prune_local(database_url, data, rclone, date(2026, 11, 20), now_ms=NOW) == []
    # A copy in R2 is damaged: nothing is removed.
    remote = bucket / "raw/quotes/2026-11-13" / raw_files[0].name
    original = remote.read_bytes()
    remote.write_bytes(original + b"x")
    with pytest.raises(RcloneError):
        prune_local(database_url, data, rclone, date(2026, 11, 21), now_ms=NOW)
    assert all(p.exists() for p in raw_files)
    assert export_file.exists()

    remote.write_bytes(original)  # repaired in R2 (an upload can't replace it: immutable)
    pruned = prune_local(database_url, data, rclone, date(2026, 11, 21), now_ms=NOW)
    assert pruned == [DAY]
    assert not any(p.exists() for p in raw_files)
    assert not export_file.exists()
    assert not (data / "raw" / "quotes" / DAY.isoformat()).exists()
    assert (data / "raw" / "manifests" / "2026-11-13.json").exists()  # manifests stay
    assert (bucket / "raw/quotes/2026-11-13" / raw_files[1].name).exists()  # R2 untouched


# ------------------------------------------------------------------------------------ restore


@pytest.mark.db
def test_restore_rebuilds_a_fresh_database_with_identical_rows(
    database_url: str, empty_database_url: str, tmp_path: Path, rclone_binary: str
) -> None:
    prepared(database_url)
    data = tmp_path / "data"
    rclone, _ = local_r2(tmp_path, rclone_binary)
    back_up_day(database_url, DAY, data, rclone, now_ms=NOW)

    report = restore_test(empty_database_url, rclone, alembic_ini=ALEMBIC_INI)

    assert report.day == DAY
    assert report.ok, report.format()
    assert {t.table for t in report.tables} == set(TABLE_SPECS)
    quotes = next(t for t in report.tables if t.table == "option_quotes")
    assert quotes.rows_restored == 120 * len(OPTIONS)
    assert "PASS" in report.format()


@pytest.mark.db
def test_restore_fails_on_a_damaged_file_and_refuses_a_live_database(
    database_url: str, empty_database_url: str, tmp_path: Path, rclone_binary: str
) -> None:
    prepared(database_url)
    rclone, bucket = local_r2(tmp_path, rclone_binary)
    back_up_day(database_url, DAY, tmp_path / "data", rclone, now_ms=NOW)

    with pytest.raises(RestoreRefusedError):
        restore_test(database_url, rclone, alembic_ini=ALEMBIC_INI, day=DAY)

    damaged = next((bucket / "exports/v1/underlying_ticks").rglob("*.parquet"))
    damaged.write_bytes(damaged.read_bytes()[:-10] + b"0123456789")
    report = restore_test(empty_database_url, rclone, alembic_ini=ALEMBIC_INI, day=DAY)
    assert not report.ok
    assert any("SHA-256" in p for p in report.file_problems)


# ------------------------------------------------------------------------------------ R2 only grows


@pytest.mark.parametrize(
    "command",
    ["sync", "delete", "deletefile", "purge", "move", "moveto", "rmdirs", "cleanup", "copyto"],
)
def test_rclone_commands_that_remove_anything_are_refused(command: str) -> None:
    def never(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("rclone must not be started")

    rclone = Rclone(RemoteConfig(bucket="b", env={}), runner=never)
    with pytest.raises(ValueError, match="not allowed"):
        rclone.run(command, "local-folder", rclone.config.target("raw"))
    with pytest.raises(ValueError, match="--immutable"):
        rclone.run("copy", "local-folder", rclone.config.target("raw"))
    with pytest.raises(ValueError, match="not allowed"):
        rclone.run("copyto", "--immutable", "--checksum", "f", rclone.config.target("raw/f"))


def test_every_operation_uses_only_reading_or_immutable_upload_commands(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def record(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        listing = "x.json\n" if command[3] == "lsf" and command[-1] == "r2:b/raw/manifests" else ""
        content = "{}" if command[3] == "cat" else ""
        return subprocess.CompletedProcess(command, 0, listing or content, "")

    rclone = Rclone(RemoteConfig(bucket="b", env={}), runner=record)
    (tmp_path / "m.json").write_text("{}")
    rclone.upload(tmp_path, "raw", ["a"])
    rclone.verify(tmp_path, "raw", ["a"])
    rclone.download("raw", tmp_path / "d", ["a"])
    rclone.put(tmp_path / "m.json", "raw/manifests/m.json")
    assert rclone.get("raw/manifests/x.json") == b"{}"  # listed: lsf, then cat
    assert rclone.get("exports/v1/manifests/y.json") is None  # not listed: lsf only
    rclone.names("exports/v1/manifests")
    commands = [c[3] for c in seen]  # after "rclone --config ''"
    assert commands == ["copy", "check", "copy", "copy", "lsf", "cat", "lsf", "lsf"]
    uploads = [c for c in seen if c[3] == "copy" and c[-1].startswith("r2:")]
    assert len(uploads) == 2
    for c in uploads:
        assert "--immutable" in c
        assert "--checksum" in c


def test_an_upload_never_changes_an_object_already_in_r2(
    tmp_path: Path, rclone_binary: str
) -> None:
    rclone, bucket = local_r2(tmp_path, rclone_binary)
    local = tmp_path / "local"
    (local / "quotes").mkdir(parents=True)
    (local / "quotes" / "a.parquet").write_bytes(b"first version")
    rclone.upload(local, "raw", ["quotes/a.parquet"])
    rclone.upload(local, "raw", ["quotes/a.parquet"])  # the same again: fine
    (local / "quotes" / "a.parquet").write_bytes(b"something else")
    with pytest.raises(RcloneError):
        rclone.upload(local, "raw", ["quotes/a.parquet"])
    with pytest.raises(RcloneError):
        rclone.put(local / "quotes" / "a.parquet", "raw/quotes/a.parquet")
    assert (bucket / "raw" / "quotes" / "a.parquet").read_bytes() == b"first version"
    # Same bytes with a newer modification time: skipped, not an error.
    (local / "quotes" / "a.parquet").write_bytes(b"first version")
    rclone.upload(local, "raw", ["quotes/a.parquet"])


@pytest.mark.db
def test_backing_up_a_day_again_is_harmless_but_never_replaces_r2(
    database_url: str, tmp_path: Path, rclone_binary: str
) -> None:
    prepared(database_url)
    data = tmp_path / "data"
    write_raw(data, DAY)
    rclone, bucket = local_r2(tmp_path, rclone_binary)
    first = back_up_day(database_url, DAY, data, rclone, now_ms=NOW)
    r2_files = {p: p.read_bytes() for p in bucket.rglob("*") if p.is_file()}

    # The same day again (nothing changed): same version, R2 untouched.
    again = back_up_day(database_url, DAY, data, rclone, now_ms=NOW + 60_000)
    assert again.dataset_version == first.dataset_version
    assert {p: p.read_bytes() for p in bucket.rglob("*") if p.is_file()} == r2_files

    # After the server's copies are gone, a forced re-run must not shrink R2's raw list.
    prune_local(database_url, data, rclone, date(2026, 11, 21), now_ms=NOW)
    with pytest.raises(BackupError, match="never replaces data in R2"):
        back_up_day(database_url, DAY, data, rclone, now_ms=NOW + 120_000)
    assert {p: p.read_bytes() for p in bucket.rglob("*") if p.is_file()} == r2_files

    # Data changed in the database: a different export is refused, not uploaded over the old.
    with psycopg.connect(database_url) as conn:
        conn.execute("UPDATE option_quotes SET bid_size = bid_size + 1 WHERE ts < '2026-11-14'")
    write_raw(data, DAY)
    with pytest.raises(BackupError, match="different export"):
        back_up_day(database_url, DAY, data, rclone, now_ms=NOW + 180_000)
    assert {p: p.read_bytes() for p in bucket.rglob("*") if p.is_file()} == r2_files
