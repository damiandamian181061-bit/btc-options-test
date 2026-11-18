"""Export one UTC day of the database to zstd Parquet, with a manifest that versions it.

Layout, the same on the server (``data/exports/v1``), in R2 (``exports/v1/``) and on the laptop
after a sync::

    {table}/date=YYYY-MM-DD/part-0.parquet
    {table}/date=YYYY-MM-DD/expiry=YYYY-MM-DD/part-0.parquet   (option_quotes, option_trades)
    manifests/YYYY-MM-DD.json

Every column is exported as stored. ``timestamptz`` becomes ``timestamp[us, UTC]``; ``numeric``
(delivery prices) and ``jsonb`` become strings, so exact decimals stay exact. Rows are sorted
by instrument then time, which compresses well and makes each file reproducible.

The manifest lists every file with its row count, size and SHA-256, and every table with its
row count and a content checksum computed by PostgreSQL over the exported rows. The restore
test recomputes that checksum in a fresh database, so it proves the round trip
database -> Parquet -> database loses nothing. The dataset version is the SHA-256 of the
manifest's content (format, day, schema revision and tables), so it changes if any byte of any
file does.

Everything is read in one REPEATABLE READ, read-only transaction: files and checksums describe
the same snapshot of the database.
"""

import hashlib
import json
import logging
import shutil
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg import sql

from optlab.domain.clock import MS_PER_DAY, datetime_to_ms, ms_to_datetime
from optlab.market_data import schema as db_schema

logger = logging.getLogger(__name__)

EXPORT_FORMAT = "optlab-export-v1"
EXPORT_DIR = "exports/v1"  # under data/ locally, and the prefix in R2
MANIFESTS = "manifests"
BATCH_ROWS = 50_000
ROW_GROUP_ROWS = 250_000
ZSTD_LEVEL = 9


@dataclass(frozen=True)
class TableSpec:
    name: str
    day_filter: str  # SQL over alias t, with %(start)s/%(end)s (timestamptz), *_ms (bigint)
    order_by: str
    by_expiry: bool = False


_TS_DAY = "t.ts >= %(start)s AND t.ts < %(end)s"
_HOUR_DAY = "t.hour_ts >= %(start)s AND t.hour_ts < %(end)s"

TABLES: tuple[TableSpec, ...] = (
    TableSpec("option_quotes", _TS_DAY, 't.instrument_name COLLATE "C", t.ts', by_expiry=True),
    TableSpec(
        "option_trades",
        _TS_DAY,
        't.instrument_name COLLATE "C", t.ts, t.trade_id COLLATE "C"',
        by_expiry=True,
    ),
    TableSpec("underlying_ticks", _TS_DAY, 't.instrument_name COLLATE "C", t.ts'),
    # Instruments listed by the end of the day and not delisted before it began.
    TableSpec(
        "instruments",
        "t.first_seen_ms < %(end_ms)s AND (t.delisted_ms IS NULL OR t.delisted_ms >= %(start_ms)s)",
        't.instrument_name COLLATE "C"',
    ),
    # Delivery prices by the day they were recorded (when they became known).
    TableSpec(
        "delivery_prices",
        "t.recorded_ms >= %(start_ms)s AND t.recorded_ms < %(end_ms)s",
        't.index_name COLLATE "C", t.delivery_date',
    ),
    TableSpec("connection_events", _TS_DAY, "t.id"),
    TableSpec("data_quality", _HOUR_DAY, "t.id"),
    TableSpec("data_quality_runs", _HOUR_DAY, "t.hour_ts"),
    TableSpec("recorder_status", _TS_DAY, 't.ts, t.run_id COLLATE "C"'),
)
TABLE_SPECS = {t.name: t for t in TABLES}
# What the export reads, plus the table the backup records each export in.
EXPORT_TABLES: tuple[str, ...] = (*(t.name for t in TABLES), "dataset_exports")

_ARROW_TYPES: dict[str, pa.DataType] = {
    "timestamp with time zone": pa.timestamp("us", tz="UTC"),
    "double precision": pa.float64(),
    "bigint": pa.int64(),
    "integer": pa.int32(),
    "smallint": pa.int16(),
    "text": pa.string(),
    "boolean": pa.bool_(),
    "numeric": pa.string(),
    "jsonb": pa.string(),
    "date": pa.date32(),
}
TEXT_TYPES = {"numeric", "jsonb"}


class ExportError(RuntimeError):
    """The export could not be completed or failed a consistency check."""


def day_bounds(day: date) -> dict[str, Any]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    start_ms = datetime_to_ms(start)
    return {
        "start": start,
        "end": start + timedelta(days=1),
        "start_ms": start_ms,
        "end_ms": start_ms + MS_PER_DAY,
    }


def session_settings(conn: psycopg.Connection[Any]) -> None:
    """Settings that make row text (and so the checksums) the same in every database."""
    conn.execute("SET TimeZone = 'UTC'")
    conn.execute("SET DateStyle = 'ISO, YMD'")
    conn.execute("SET extra_float_digits = 1")


def table_columns(conn: psycopg.Connection[Any], table: str) -> list[tuple[str, str, bool]]:
    """(name, PostgreSQL type, nullable) for each column, in table order."""
    rows = conn.execute(
        "SELECT column_name, data_type, is_nullable = 'YES' FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    if not rows:
        raise ExportError(f"table {table} not found")
    unknown = [(n, t) for n, t, _ in rows if t not in _ARROW_TYPES]
    if unknown:
        raise ExportError(f"{table}: no Parquet type for {unknown}")
    return [(str(n), str(t), bool(null)) for n, t, null in rows]


def arrow_schema(columns: Sequence[tuple[str, str, bool]]) -> pa.Schema:
    return pa.schema([pa.field(n, _ARROW_TYPES[t], nullable=null) for n, t, null in columns])


def content_checksum(
    conn: psycopg.Connection[Any], spec: TableSpec, params: dict[str, Any]
) -> tuple[int, str]:
    """Row count and an order-independent checksum of the rows, computed by PostgreSQL.

    Each row's text form (``t::text``) is hashed with MD5; the checksum combines the sum of the
    first 60 bits and the XOR of the next 60 bits over all rows. Needs ``session_settings``.
    """
    query = sql.SQL(
        "SELECT count(*), "
        "coalesce(sum(('x' || substr(h, 1, 15))::bit(60)::bigint), 0)::text, "
        "coalesce(bit_xor(('x' || substr(h, 16, 15))::bit(60)::bigint), 0)::text "
        "FROM (SELECT md5(t::text) AS h FROM {table} t WHERE {where}) s"
    ).format(table=sql.Identifier(spec.name), where=sql.SQL(spec.day_filter))
    row = conn.execute(query, params).fetchone()
    if row is None:  # pragma: no cover - count(*) always returns a row
        raise ExportError(f"checksum of {spec.name} returned nothing")
    return int(row[0]), f"md5-60:{row[1]}:{row[2]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def dataset_version(manifest: dict[str, Any]) -> str:
    """SHA-256 of the manifest's content: format, day, schema revision and every table."""
    content = {k: manifest[k] for k in ("format", "day", "schema_revision", "tables")}
    return hashlib.sha256(canonical_json(content)).hexdigest()


def combined_version(versions: Sequence[str]) -> str:
    """The dataset version of several days: SHA-256 over their versions in day order."""
    return hashlib.sha256("\n".join(versions).encode()).hexdigest()


def manifest_path(root: Path, day: date) -> Path:
    return root / MANIFESTS / f"{day.isoformat()}.json"


def _select(spec: TableSpec, columns: Sequence[tuple[str, str, bool]], extra: str) -> sql.Composed:
    cols = sql.SQL(", ").join(
        sql.SQL("t.{}::text AS {}").format(sql.Identifier(n), sql.Identifier(n))
        if t in TEXT_TYPES
        else sql.SQL("t.{}").format(sql.Identifier(n))
        for n, t, _ in columns
    )
    return sql.SQL("SELECT {cols} FROM {table} t WHERE {where}{extra} ORDER BY {order}").format(
        cols=cols,
        table=sql.Identifier(spec.name),
        where=sql.SQL(spec.day_filter),
        extra=sql.SQL(extra),
        order=sql.SQL(spec.order_by),
    )


def _batches(
    conn: psycopg.Connection[Any], query: sql.Composed, params: dict[str, Any], schema: pa.Schema
) -> Iterator[pa.RecordBatch]:
    with conn.cursor(name="optlab_export") as cur:
        cur.itersize = BATCH_ROWS
        cur.execute(query, params)
        while rows := cur.fetchmany(BATCH_ROWS):
            columns = list(zip(*rows, strict=True))
            yield pa.RecordBatch.from_arrays(
                [pa.array(c, type=f.type) for c, f in zip(columns, schema, strict=True)],
                schema=schema,
            )


def _write_file(
    path: Path, schema: pa.Schema, batches: Iterator[pa.RecordBatch], *, keep_empty: bool
) -> tuple[int, int] | None:
    """Write batches to one Parquet file; (rows, bytes). With no rows, write an empty file
    (so the day still carries the table's schema) or, unless ``keep_empty``, nothing."""
    pending: list[pa.RecordBatch] = []
    pending_rows = 0
    rows = 0
    writer: pq.ParquetWriter | None = None
    tmp = path.with_name(path.name + ".tmp")
    try:
        for batch in batches:
            pending.append(batch)
            pending_rows += batch.num_rows
            if pending_rows >= ROW_GROUP_ROWS:
                writer = writer or _open_writer(tmp, schema)
                writer.write_table(pa.Table.from_batches(pending, schema=schema))
                rows += pending_rows
                pending, pending_rows = [], 0
        if pending_rows:
            writer = writer or _open_writer(tmp, schema)
            writer.write_table(pa.Table.from_batches(pending, schema=schema))
            rows += pending_rows
        if writer is None and keep_empty:
            writer = _open_writer(tmp, schema)
            writer.write_table(schema.empty_table())
    finally:
        if writer is not None:
            writer.close()
    if writer is None:
        return None
    tmp.rename(path)
    return rows, path.stat().st_size


def _open_writer(path: Path, schema: pa.Schema) -> pq.ParquetWriter:
    path.parent.mkdir(parents=True, exist_ok=True)
    return pq.ParquetWriter(
        str(path),
        schema,
        compression="zstd",
        compression_level=ZSTD_LEVEL,
        use_dictionary=True,
        write_statistics=True,
    )


def _expiry_days(conn: psycopg.Connection[Any], params: dict[str, Any]) -> list[date]:
    rows = conn.execute(
        "SELECT DISTINCT expiry_ms FROM instruments WHERE kind = 'option' "
        "AND first_seen_ms < %(end_ms)s AND expiry_ms >= %(start_ms)s",
        params,
    ).fetchall()
    return sorted({ms_to_datetime(int(r[0])).date() for r in rows})


def export_day(database_url: str, day: date, root: Path, *, now_ms: int) -> dict[str, Any]:
    """Export ``day`` under ``root`` (``data/exports/v1``) and write its manifest; return it.

    Files from an earlier, unfinished export of the same day are replaced.
    """
    params = day_bounds(day)
    tag = f"date={day.isoformat()}"
    for spec in TABLES:
        shutil.rmtree(root / spec.name / tag, ignore_errors=True)
    tables: dict[str, Any] = {}
    with psycopg.connect(database_url, application_name="optlab-export") as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        session_settings(conn)
        try:
            revision = db_schema.check(conn, EXPORT_TABLES, "the nightly export")
        except db_schema.SchemaMismatchError as e:
            raise ExportError(str(e)) from None
        for spec in TABLES:
            columns = table_columns(conn, spec.name)
            schema = arrow_schema(columns)
            files: list[dict[str, Any]] = []
            parts: list[tuple[str, str, dict[str, Any]]] = []
            if spec.by_expiry:
                for expiry in _expiry_days(conn, params):
                    bounds = day_bounds(expiry)
                    extra = (
                        " AND t.instrument_name IN (SELECT instrument_name FROM instruments "
                        "WHERE kind = 'option' AND expiry_ms >= %(exp_start)s "
                        "AND expiry_ms < %(exp_end)s)"
                    )
                    p = {**params, "exp_start": bounds["start_ms"], "exp_end": bounds["end_ms"]}
                    parts.append((f"{tag}/expiry={expiry.isoformat()}", extra, p))
            else:
                parts.append((tag, "", params))
            for folder, extra, p in parts:
                rel = f"{spec.name}/{folder}/part-0.parquet"
                batches = _batches(conn, _select(spec, columns, extra), p, schema)
                written = _write_file(root / rel, schema, batches, keep_empty=not spec.by_expiry)
                if written is None:
                    continue
                n, size = written
                files.append(
                    {"path": rel, "rows": n, "bytes": size, "sha256": sha256_file(root / rel)}
                )
            rows, checksum = content_checksum(conn, spec, params)
            if rows != sum(f["rows"] for f in files):
                raise ExportError(
                    f"{spec.name} {day}: {rows} rows in the day but "
                    f"{sum(f['rows'] for f in files)} written (an option without an instrument?)"
                )
            tables[spec.name] = {"rows": rows, "checksum": checksum, "files": files}
            logger.info("export %s %s: %d rows in %d files", day, spec.name, rows, len(files))
        conn.rollback()

    manifest: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "day": day.isoformat(),
        "schema_revision": revision,  # the database's, at export time
        "tables": tables,
    }
    manifest["dataset_version"] = dataset_version(manifest)
    manifest["created_ms"] = now_ms
    path = manifest_path(root, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(json.dumps(manifest, indent=1, sort_keys=True).encode() + b"\n")
    tmp.rename(path)
    return manifest


def read_manifest(data: bytes) -> dict[str, Any]:
    """Parse a manifest and check that its dataset version matches its content."""
    manifest: dict[str, Any] = json.loads(data)
    if manifest.get("format") != EXPORT_FORMAT:
        raise ExportError(f"unknown manifest format {manifest.get('format')!r}")
    if dataset_version(manifest) != manifest.get("dataset_version"):
        raise ExportError(f"manifest for {manifest.get('day')}: dataset version does not match")
    return manifest


def manifest_files(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for t in manifest["tables"].values() for f in t["files"]]


def verify_files(root: Path, files: Sequence[dict[str, Any]]) -> list[str]:
    """Problems with local copies of these files (missing, wrong size or SHA-256)."""
    problems: list[str] = []
    for f in files:
        path = root / f["path"]
        if not path.is_file():
            problems.append(f"{f['path']}: missing")
        elif path.stat().st_size != f["bytes"]:
            problems.append(f"{f['path']}: {path.stat().st_size} bytes, expected {f['bytes']}")
        elif sha256_file(path) != f["sha256"]:
            problems.append(f"{f['path']}: SHA-256 does not match")
    return problems


# ------------------------------------------------------------------------------------ raw files

RAW_FORMAT = "optlab-raw-v1"
RAW_STREAMS = ("quotes", "trades")


def raw_manifest(raw_dir: Path, day: date, *, now_ms: int) -> dict[str, Any]:
    """Every finished raw file for ``day`` (``.parquet`` and ``.incomplete``), with hashes.

    Refuses while any file of that day is still being written (``.inprogress``).
    """
    files: list[dict[str, Any]] = []
    for stream in RAW_STREAMS:
        folder = raw_dir / stream / day.isoformat()
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.name.endswith(".inprogress"):
                raise ExportError(f"raw file still being written: {stream}/{path.name}")
            rows = pq.ParquetFile(path).metadata.num_rows if path.suffix == ".parquet" else None
            files.append(
                {
                    "path": f"{stream}/{day.isoformat()}/{path.name}",
                    "rows": rows,
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return {
        "format": RAW_FORMAT,
        "day": day.isoformat(),
        "files": files,
        "bytes": sum(f["bytes"] for f in files),
        "created_ms": now_ms,
    }
