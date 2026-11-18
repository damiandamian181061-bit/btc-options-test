"""Compatible schemas (Damian's add-on j, 18 Nov 2026): services check their own tables, not the
revision, so a migration that only adds tables leaves them running."""

import psycopg
import pytest
from alembic import command

from optlab.market_data import schema
from optlab.market_data.export import EXPORT_TABLES
from tests.conftest import alembic_config


def test_fingerprint_is_stable_and_sensitive() -> None:
    d = {"columns": [["a", "integer", True]], "constraints": [["p", "PRIMARY KEY (a)"]]}
    assert schema.fingerprint(d) == schema.fingerprint(
        {"constraints": [["p", "PRIMARY KEY (a)"]], "columns": [["a", "integer", True]]}
    )
    assert schema.fingerprint(d) != schema.fingerprint(
        {"columns": [["a", "integer", False]], "constraints": [["p", "PRIMARY KEY (a)"]]}
    )
    assert len(schema.fingerprint(d)) == 16


def test_every_service_table_has_an_expected_fingerprint() -> None:
    lists = (
        schema.RECORDER_TABLES,
        schema.VIEW_TABLES,
        schema.ROUGH_TABLES,
        schema.SURFACE_TABLES,
        EXPORT_TABLES,
    )
    for tables in lists:
        assert set(tables) <= set(schema.EXPECTED), set(tables) - set(schema.EXPECTED)


@pytest.mark.db
def test_expected_fingerprints_match_the_head_migration(database_url: str) -> None:
    """If a migration changes a table, this fails until EXPECTED is updated, and the services
    using that table then refuse to start on the old image (they must be restarted)."""
    with psycopg.connect(database_url) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version' ORDER BY 1"
            )
        ]
        assert set(tables) == set(schema.EXPECTED)
        assert schema.fingerprints(conn, tables) == schema.EXPECTED
        assert schema.revision(conn) == "0004"
        for tables_ in (schema.RECORDER_TABLES, EXPORT_TABLES, schema.SURFACE_TABLES):
            assert schema.check(conn, tables_, "test") == "0004"


@pytest.mark.db
def test_older_revision_runs_what_it_has(empty_database_url: str) -> None:
    """At 0003 (before the surface tables) the recorder, export and paper service are fine; the
    surface worker is not."""
    command.upgrade(alembic_config(empty_database_url), "0003")
    with psycopg.connect(empty_database_url) as conn:
        assert schema.revision(conn) == "0003"
        schema.check(conn, schema.RECORDER_TABLES, "the recorder")
        schema.check(conn, EXPORT_TABLES, "the export")
        schema.check(conn, (*schema.VIEW_TABLES, *schema.ROUGH_TABLES), "the paper service")
        with pytest.raises(schema.SchemaMismatchError, match="vol_surface: missing"):
            schema.check(conn, schema.SURFACE_TABLES, "the surface worker")


@pytest.mark.db
def test_a_changed_table_is_refused(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE option_quotes ADD COLUMN extra integer")
        with pytest.raises(schema.SchemaMismatchError, match=r"option_quotes: changed") as e:
            schema.check(conn, schema.RECORDER_TABLES, "the recorder")
        assert "revision '0004'" in str(e.value)
        # Services that don't use the table are not affected.
        schema.check(conn, schema.SURFACE_TABLES, "the surface worker")
        conn.execute("ALTER TABLE delivery_prices DROP CONSTRAINT delivery_prices_pkey")
        assert any("delivery_prices" in p for p in schema.mismatches(conn, schema.RECORDER_TABLES))


@pytest.mark.db
def test_check_url_exits_with_the_reason(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DROP TABLE features")
    with pytest.raises(SystemExit, match="features: missing"):
        schema.check_url(database_url, schema.SURFACE_TABLES, "the surface worker")
    assert schema.check_url(database_url, schema.RECORDER_TABLES, "the recorder") == "0004"
