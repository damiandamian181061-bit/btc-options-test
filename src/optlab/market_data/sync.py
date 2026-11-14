"""Pull exported days from R2 into ``data/`` on the laptop, for local backtests.

Each day's manifest is fetched and its dataset version checked; only files that are missing
locally or whose SHA-256 differs are downloaded, and every file is checked afterwards. The
manifest is written last, so a local manifest always means the day is complete. ``raw=True``
also pulls that day's raw stream files.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from optlab.market_data.export import (
    EXPORT_DIR,
    ExportError,
    manifest_files,
    manifest_path,
    read_manifest,
    verify_files,
)
from optlab.market_data.r2 import EXPORTS_PREFIX, RAW_PREFIX, Rclone

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncResult:
    day: date
    dataset_version: str
    files: int
    downloaded: int
    bytes: int
    raw_files: int = 0


def available_days(rclone: Rclone) -> list[date]:
    names = rclone.names(f"{EXPORTS_PREFIX}/manifests")
    return [date.fromisoformat(n.removesuffix(".json")) for n in names if n.endswith(".json")]


def sync_day(rclone: Rclone, day: date, data_dir: Path, *, raw: bool = False) -> SyncResult:
    data = rclone.get(f"{EXPORTS_PREFIX}/manifests/{day.isoformat()}.json")
    if data is None:
        raise ExportError(f"{day} is not in R2 (not exported yet?)")
    manifest = read_manifest(data)
    root = data_dir / EXPORT_DIR
    files = manifest_files(manifest)
    stale = [f["path"] for f in files if verify_files(root, [f])]
    rclone.download(EXPORTS_PREFIX, root, stale)
    problems = verify_files(root, files)
    if problems:
        raise ExportError(f"{day}: downloaded files do not match the manifest: {problems[:5]}")
    path = manifest_path(root, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

    raw_count = 0
    if raw:
        raw_data = rclone.get(f"{RAW_PREFIX}/manifests/{day.isoformat()}.json")
        if raw_data is None:
            raise ExportError(f"{day}: no raw manifest in R2")
        raw_files = json.loads(raw_data)["files"]
        raw_dir = data_dir / "raw"
        missing = [f["path"] for f in raw_files if verify_files(raw_dir, [f])]
        rclone.download(RAW_PREFIX, raw_dir, missing)
        problems = verify_files(raw_dir, raw_files)
        if problems:
            raise ExportError(f"{day}: raw files do not match their manifest: {problems[:5]}")
        raw_count = len(raw_files)

    result = SyncResult(
        day=day,
        dataset_version=manifest["dataset_version"],
        files=len(files),
        downloaded=len(stale),
        bytes=sum(f["bytes"] for f in files),
        raw_files=raw_count,
    )
    logger.info(
        "synced %s: %d files (%d downloaded), %.1f MB, dataset version %s",
        day,
        result.files,
        result.downloaded,
        result.bytes / 1e6,
        result.dataset_version[:12],
    )
    return result
