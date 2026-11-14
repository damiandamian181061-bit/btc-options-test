"""The nightly backup: export each finished UTC day, put it and its raw files in R2, verify.

For each recorded day before today that is not yet verified in R2:

1. export it (``export.export_day``) and list its raw files (``export.raw_manifest``);
2. upload the raw files to ``raw/{quotes,trades}/DAY/`` and the export to ``exports/v1/``;
3. check every uploaded file against R2 (size and MD5, ``rclone check``);
4. upload the raw manifest, then the export manifest, last, and read the export manifest back.
   A manifest in R2 therefore always means every file it lists is there;
5. record the day in ``dataset_exports`` with its dataset version.

Afterwards, the server's own raw and export files for days more than 7 days old are removed,
but only files listed in a manifest and found again in R2 with the same size and MD5 just
before (decided by Damian on 14 Nov: 7 days of raw files on the server, older ones only in R2).
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from optlab.market_data.export import (
    EXPORT_DIR,
    export_day,
    manifest_files,
    manifest_path,
    raw_manifest,
    read_manifest,
)
from optlab.market_data.r2 import EXPORTS_PREFIX, RAW_PREFIX, Rclone

logger = logging.getLogger(__name__)

KEEP_LOCAL_DAYS = 7
RAW_MANIFESTS = "manifests"


class BackupError(RuntimeError):
    """A day could not be backed up or verified."""


@dataclass(frozen=True)
class DayBackup:
    day: date
    dataset_version: str
    rows: int
    export_bytes: int
    raw_files: int
    raw_bytes: int

    def describe(self) -> str:
        return (
            f"{self.day}: {self.rows:,} rows, {self.export_bytes / 1e6:.1f} MB exported, "
            f"{self.raw_files} raw files {self.raw_bytes / 1e6:.1f} MB, "
            f"dataset version {self.dataset_version[:12]}"
        )


def recorded_days(conn: psycopg.Connection[Any], raw_dir: Path, today: date) -> list[date]:
    """Every UTC day from the first one with data up to yesterday."""
    row = conn.execute("SELECT min(ts) FROM option_quotes").fetchone()
    firsts: list[date] = []
    if row is not None and row[0] is not None:
        firsts.append(row[0].astimezone(UTC).date())
    for stream in ("quotes", "trades"):
        folder = raw_dir / stream
        if folder.is_dir():
            for p in folder.iterdir():
                try:
                    firsts.append(date.fromisoformat(p.name))
                except ValueError:
                    continue
    if not firsts:
        return []
    first = min(firsts)
    return [first + timedelta(days=i) for i in range((today - first).days)]


def days_to_back_up(conn: psycopg.Connection[Any], raw_dir: Path, today: date) -> list[date]:
    done = {
        r[0]
        for r in conn.execute(
            "SELECT day FROM dataset_exports WHERE uploaded_ms IS NOT NULL"
        ).fetchall()
    }
    return [d for d in recorded_days(conn, raw_dir, today) if d not in done]


def back_up_day(
    database_url: str, day: date, data_dir: Path, rclone: Rclone, *, now_ms: int
) -> DayBackup:
    """Export, upload and verify one day; record it in dataset_exports."""
    export_root = data_dir / EXPORT_DIR
    raw_dir = data_dir / "raw"
    manifest = export_day(database_url, day, export_root, now_ms=now_ms)
    raw = raw_manifest(raw_dir, day, now_ms=now_ms)
    export_files = [f["path"] for f in manifest_files(manifest)]
    raw_files = [f["path"] for f in raw["files"]]

    rclone.upload(raw_dir, RAW_PREFIX, raw_files)
    rclone.verify(raw_dir, RAW_PREFIX, raw_files)
    rclone.upload(export_root, EXPORTS_PREFIX, export_files)
    rclone.verify(export_root, EXPORTS_PREFIX, export_files)

    raw_manifest_file = raw_dir / RAW_MANIFESTS / f"{day.isoformat()}.json"
    raw_manifest_file.parent.mkdir(parents=True, exist_ok=True)
    raw_manifest_file.write_bytes(json.dumps(raw, indent=1, sort_keys=True).encode() + b"\n")
    rclone.put(raw_manifest_file, f"{RAW_PREFIX}/{RAW_MANIFESTS}/{day.isoformat()}.json")
    local_manifest = manifest_path(export_root, day)
    remote_manifest = f"{EXPORTS_PREFIX}/manifests/{day.isoformat()}.json"
    rclone.put(local_manifest, remote_manifest)
    if rclone.get(remote_manifest) != local_manifest.read_bytes():
        raise BackupError(f"{day}: the manifest read back from R2 differs from the local one")

    result = DayBackup(
        day=day,
        dataset_version=manifest["dataset_version"],
        rows=sum(t["rows"] for t in manifest["tables"].values()),
        export_bytes=sum(f["bytes"] for f in manifest_files(manifest)),
        raw_files=len(raw_files),
        raw_bytes=raw["bytes"],
    )
    with psycopg.connect(database_url, application_name="optlab-backup") as conn:
        conn.execute(
            "INSERT INTO dataset_exports (day, dataset_version, manifest, rows, export_bytes, "
            "raw_files, raw_bytes, exported_ms, uploaded_ms) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (day) DO UPDATE SET dataset_version = EXCLUDED.dataset_version, "
            "manifest = EXCLUDED.manifest, rows = EXCLUDED.rows, "
            "export_bytes = EXCLUDED.export_bytes, raw_files = EXCLUDED.raw_files, "
            "raw_bytes = EXCLUDED.raw_bytes, exported_ms = EXCLUDED.exported_ms, "
            "uploaded_ms = EXCLUDED.uploaded_ms, local_pruned_ms = NULL",
            (
                day,
                result.dataset_version,
                Jsonb(manifest),
                result.rows,
                result.export_bytes,
                result.raw_files,
                result.raw_bytes,
                manifest["created_ms"],
                now_ms,
            ),
        )
    logger.info("backed up %s", result.describe())
    return result


def prune_local(
    database_url: str,
    data_dir: Path,
    rclone: Rclone,
    today: date,
    *,
    now_ms: int,
    keep_days: int = KEEP_LOCAL_DAYS,
) -> list[date]:
    """Remove the server's raw and export files for verified days older than ``keep_days``.

    Each file is checked against R2 again first; only files listed in the day's manifests are
    removed, and anything else in those folders is left alone.
    """
    export_root = data_dir / EXPORT_DIR
    raw_dir = data_dir / "raw"
    cutoff = today - timedelta(days=keep_days)
    with psycopg.connect(database_url, application_name="optlab-backup") as conn:
        days = [
            r[0]
            for r in conn.execute(
                "SELECT day FROM dataset_exports WHERE uploaded_ms IS NOT NULL "
                "AND local_pruned_ms IS NULL AND day < %s ORDER BY day",
                (cutoff,),
            ).fetchall()
        ]
        pruned: list[date] = []
        for day in days:
            manifest = read_manifest(manifest_path(export_root, day).read_bytes())
            raw_file = raw_dir / RAW_MANIFESTS / f"{day.isoformat()}.json"
            raw = json.loads(raw_file.read_bytes())
            export_files = [
                f["path"] for f in manifest_files(manifest) if (export_root / f["path"]).exists()
            ]
            raw_files = [f["path"] for f in raw["files"] if (raw_dir / f["path"]).exists()]
            rclone.verify(raw_dir, RAW_PREFIX, raw_files)
            rclone.verify(export_root, EXPORTS_PREFIX, export_files)
            for root, files in ((raw_dir, raw_files), (export_root, export_files)):
                for rel in files:
                    (root / rel).unlink()
                for rel in files:
                    _remove_empty_parents(root / rel, root)
            conn.execute(
                "UPDATE dataset_exports SET local_pruned_ms = %s WHERE day = %s", (now_ms, day)
            )
            conn.commit()
            logger.info(
                "removed the server's copies of %s (%d raw and %d export files, verified in R2)",
                day,
                len(raw_files),
                len(export_files),
            )
            pruned.append(day)
    return pruned


def _remove_empty_parents(path: Path, root: Path) -> None:
    parent = path.parent
    while parent != root and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent
