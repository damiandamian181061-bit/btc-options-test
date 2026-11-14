"""Restore test: rebuild a fresh database from R2 for one day and prove nothing was lost.

1. Refuse unless the target database is empty, so this can never write into the live one.
2. Create the schema with the Alembic migrations.
3. Fetch the day's manifest from R2 and check its dataset version.
4. Download every file it lists and check its size and SHA-256.
5. Load every file into its table with COPY.
6. Recompute each table's row count and content checksum in the restored database and compare
   them with the manifest (computed in the live database at export time).

On the server it runs in a throwaway TimescaleDB container with its own volume and a memory
limit (``infra/restore-test``), never against the live ``db`` service.
"""

import logging
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pyarrow.parquet as pq
from alembic import command
from alembic.config import Config
from psycopg import sql

from optlab.market_data.export import (
    TABLE_SPECS,
    content_checksum,
    day_bounds,
    read_manifest,
    session_settings,
    verify_files,
)
from optlab.market_data.r2 import EXPORTS_PREFIX, Rclone

logger = logging.getLogger(__name__)


class RestoreRefusedError(RuntimeError):
    """The target is not an empty database."""


@dataclass
class TableCheck:
    table: str
    rows_expected: int
    rows_restored: int
    checksum_expected: str
    checksum_restored: str

    @property
    def ok(self) -> bool:
        return (
            self.rows_expected == self.rows_restored
            and self.checksum_expected == self.checksum_restored
        )


@dataclass
class RestoreReport:
    day: date
    dataset_version: str
    files: int
    bytes: int
    file_problems: list[str] = field(default_factory=list)
    tables: list[TableCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.file_problems and bool(self.tables) and all(t.ok for t in self.tables)

    def format(self) -> str:
        lines = [
            f"Restore test for {self.day}: {'PASS' if self.ok else 'FAIL'}",
            f"dataset version {self.dataset_version}",
            f"{self.files} files, {self.bytes / 1e6:.1f} MB downloaded from R2; "
            f"SHA-256 {'all match' if not self.file_problems else 'PROBLEMS'}",
            *(f"  file problem: {p}" for p in self.file_problems),
            "",
            "| table | rows in manifest | rows restored | checksum |",
            "| --- | --- | --- | --- |",
        ]
        for t in self.tables:
            same = "match" if t.checksum_expected == t.checksum_restored else "DIFFERENT"
            lines.append(f"| {t.table} | {t.rows_expected:,} | {t.rows_restored:,} | {same} |")
        return "\n".join(lines)


def alembic_config(database_url: str, ini: Path) -> Config:
    config = Config(str(ini))
    config.attributes["database_url"] = database_url
    config.attributes["configure_logging"] = False
    return config


def refuse_unless_empty(database_url: str) -> None:
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', "
            "'information_schema', '_timescaledb_catalog', '_timescaledb_config', "
            "'_timescaledb_internal', '_timescaledb_cache', '_timescaledb_functions', "
            "'timescaledb_information', 'timescaledb_experimental')"
        ).fetchone()
    if row is None or row[0] != 0:
        raise RestoreRefusedError(
            "the restore target already has tables: the restore test only runs against a "
            "new, empty database (never the live one)"
        )


def latest_day(rclone: Rclone) -> date:
    names = [n for n in rclone.names(f"{EXPORTS_PREFIX}/manifests") if n.endswith(".json")]
    if not names:
        raise RuntimeError("no exported day in R2 yet")
    return date.fromisoformat(max(names).removesuffix(".json"))


def load_file(conn: psycopg.Connection[Any], table: str, path: Path) -> int:
    """COPY one Parquet file into ``table``; returns the rows loaded."""
    parquet = pq.ParquetFile(path)
    names = parquet.schema_arrow.names
    cols = sql.SQL(", ").join(map(sql.Identifier, names))
    rows = 0
    with (
        conn.cursor() as cur,
        cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(table), cols)) as copy,
    ):
        for batch in parquet.iter_batches(batch_size=50_000):
            for row in zip(*(c.to_pylist() for c in batch.columns), strict=True):
                copy.write_row(row)
            rows += batch.num_rows
    return rows


def restore_test(
    database_url: str,
    rclone: Rclone,
    *,
    alembic_ini: Path,
    day: date | None = None,
    work_dir: Path | None = None,
) -> RestoreReport:
    refuse_unless_empty(database_url)
    day = day or latest_day(rclone)
    raw = rclone.get(f"{EXPORTS_PREFIX}/manifests/{day.isoformat()}.json")
    if raw is None:
        raise RuntimeError(f"no manifest for {day} in R2")
    manifest = read_manifest(raw)
    files = [f for t in manifest["tables"].values() for f in t["files"]]
    report = RestoreReport(
        day=day,
        dataset_version=manifest["dataset_version"],
        files=len(files),
        bytes=sum(f["bytes"] for f in files),
    )
    logger.info("restore test %s: creating the schema", day)
    command.upgrade(alembic_config(database_url, alembic_ini), "head")

    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        root = Path(tmp)
        rclone.download(EXPORTS_PREFIX, root, [f["path"] for f in files])
        report.file_problems = verify_files(root, files)
        if report.file_problems:
            return report
        with psycopg.connect(database_url, application_name="optlab-restore") as conn:
            for name, table in manifest["tables"].items():
                loaded = sum(load_file(conn, name, root / f["path"]) for f in table["files"])
                logger.info("restore test %s: loaded %d rows into %s", day, loaded, name)
            conn.commit()
            session_settings(conn)
            params = day_bounds(day)
            for name, table in manifest["tables"].items():
                rows, checksum = content_checksum(conn, TABLE_SPECS[name], params)
                report.tables.append(
                    TableCheck(name, table["rows"], rows, table["checksum"], checksum)
                )
    return report
