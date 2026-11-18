"""Which database schemas a service can run on: the tables it uses must be exactly as expected.

Each service names the tables it reads or writes. At start (and the nightly export before each
run) it checks those tables' columns, types, NULL rules and constraints against the fingerprints
this code was built for (``EXPECTED``), whatever the database's Alembic revision. So:

- a later migration that only adds tables (like 0004's surface tables) leaves the recorder, the
  backup job and the paper service running, and a reboot brings them back on their old image
  without refusing the newer schema;
- a migration that changes one of their tables makes them refuse to start, as before, so they
  are restarted on the new image (infra/server-setup.md section 16).

Decided by Damian on 18 Nov 2026 (Phase 3, add-on j). ``EXPECTED`` describes the head
migration; tests/market_data/test_schema.py checks it against a freshly migrated database, so
changing a table without updating its fingerprint fails CI.
"""

import hashlib
import json
from collections.abc import Iterable
from typing import Any

import psycopg

# sha256 of each table's columns and constraints (see ``describe``), first 16 hex digits, at
# the head migration (0004). Unchanged since a table's own migration: 0001 recorder tables,
# 0002 data platform, 0003 rough paper and host_boots, 0004 surface.
EXPECTED: dict[str, str] = {
    "connection_events": "f2e37fed2e4e883a",
    "data_quality": "6565481b20b21404",
    "data_quality_runs": "e18c0e87bb420429",
    "dataset_exports": "4d736264e2669746",
    "delivery_prices": "3b21179a421fb260",
    "features": "29b96e77c09fa166",
    "host_boots": "4ad3b8f9ffc8302a",
    "instruments": "bc3a74ab65b76377",
    "option_quotes": "dade0c1edbf153be",
    "option_trades": "e73f76098ad54202",
    "recorder_status": "c7fd62709a09de1f",
    "rough_decisions": "3d0fd6efe7e7b4d4",
    "rough_equity": "ca4c52336dc64d8e",
    "rough_fills": "e320031a207d96a7",
    "rough_runs": "979ac80d883df744",
    "rough_spreads": "cc7840d2e6f89634",
    "underlying_ticks": "e1706d0cc561f40e",
    "vol_expiries": "1141b37703200a01",
    "vol_surface": "b5c58b49bbb2fc59",
}

RECORDER_TABLES: tuple[str, ...] = (
    "instruments",
    "option_quotes",
    "underlying_ticks",
    "option_trades",
    "delivery_prices",
    "connection_events",
    "recorder_status",
)
# The market-data tables every reader of MarketDataView may touch.
VIEW_TABLES: tuple[str, ...] = (
    "instruments",
    "option_quotes",
    "underlying_ticks",
    "option_trades",
    "delivery_prices",
    "data_quality",
)
ROUGH_TABLES: tuple[str, ...] = (
    "rough_runs",
    "rough_decisions",
    "rough_spreads",
    "rough_fills",
    "rough_equity",
)
SURFACE_TABLES: tuple[str, ...] = ("vol_expiries", "vol_surface", "features")


class SchemaMismatchError(RuntimeError):
    """A table this code uses is missing or differs from what the code expects."""


def describe(conn: psycopg.Connection[Any], table: str) -> dict[str, Any] | None:
    """Columns (name, type, NOT NULL) in order and constraint definitions, or None if absent."""
    oid = conn.execute("SELECT to_regclass(%s)::oid", (f"public.{table}",)).fetchone()
    if oid is None or oid[0] is None:
        return None
    columns = conn.execute(
        "SELECT attname, format_type(atttypid, atttypmod), attnotnull FROM pg_attribute "
        "WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
        (oid[0],),
    ).fetchall()
    constraints = conn.execute(
        "SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = %s ORDER BY 1, 2",
        (oid[0],),
    ).fetchall()
    return {
        "columns": [[str(n), str(t), bool(nn)] for n, t, nn in columns],
        "constraints": [[str(c), str(d)] for c, d in constraints],
    }


def fingerprint(description: dict[str, Any]) -> str:
    text = json.dumps(description, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def fingerprints(conn: psycopg.Connection[Any], tables: Iterable[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for t in tables:
        d = describe(conn, t)
        out[t] = None if d is None else fingerprint(d)
    return out


def revision(conn: psycopg.Connection[Any]) -> str | None:
    exists = conn.execute("SELECT to_regclass('public.alembic_version') IS NOT NULL").fetchone()
    if not exists or not exists[0]:
        return None
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return None if row is None else str(row[0])


def mismatches(conn: psycopg.Connection[Any], tables: Iterable[str]) -> list[str]:
    """What differs, one line per table; empty when every table is as expected."""
    out: list[str] = []
    for table, found in fingerprints(conn, tables).items():
        want = EXPECTED.get(table)
        if want is None:
            out.append(f"{table}: no expected fingerprint in this code")
        elif found is None:
            out.append(f"{table}: missing")
        elif found != want:
            out.append(f"{table}: changed ({found}, this code expects {want})")
    return out


def check(conn: psycopg.Connection[Any], tables: Iterable[str], who: str) -> str | None:
    """The database's revision; ``SchemaMismatchError`` unless every table is as expected."""
    problems = mismatches(conn, tables)
    rev = revision(conn)
    if problems:
        raise SchemaMismatchError(
            f"{who} can't run on this database (revision {rev!r}): "
            + "; ".join(problems)
            + ". Back up, run the migrations, and restart it on the matching image "
            "(infra/server-setup.md)"
        )
    return rev


def check_url(database_url: str, tables: Iterable[str], who: str) -> str | None:
    """``check`` on a new connection; exits the program with the reason if it fails."""
    with psycopg.connect(database_url, autocommit=True, application_name=who) as conn:
        try:
            return check(conn, tables, who)
        except SchemaMismatchError as e:
            raise SystemExit(str(e)) from None
