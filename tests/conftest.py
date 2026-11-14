"""Shared fixtures. Database tests need a TimescaleDB server named by OPTLAB_TEST_DATABASE_URL
(CI starts one; locally see infra/README.md). Each test gets a fresh database at the latest
migration, dropped afterwards."""

import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "infra" / "alembic.ini"


def alembic_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["database_url"] = database_url
    config.attributes["configure_logging"] = False
    return config


@contextmanager
def _new_database(migrate: bool) -> Iterator[str]:
    base = os.environ.get("OPTLAB_TEST_DATABASE_URL")
    if not base:
        if os.environ.get("OPTLAB_REQUIRE_DB_TESTS"):
            pytest.fail("OPTLAB_TEST_DATABASE_URL is not set but database tests are required")
        pytest.skip("OPTLAB_TEST_DATABASE_URL is not set")
    name = f"optlab_t_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parts = urlsplit(base)
    url = urlunsplit(parts._replace(path=f"/{name}"))
    try:
        if migrate:
            command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def database_url() -> Iterator[str]:
    """A fresh database at the latest migration."""
    with _new_database(migrate=True) as url:
        yield url


@pytest.fixture
def second_database_url() -> Iterator[str]:
    """Another fresh database at the latest migration (for A/B comparisons)."""
    with _new_database(migrate=True) as url:
        yield url


@pytest.fixture
def empty_database_url() -> Iterator[str]:
    """A fresh database with no tables at all (a restore target)."""
    with _new_database(migrate=False) as url:
        yield url


@pytest.fixture
def rclone_binary() -> str:
    """rclone on PATH or in OPTLAB_RCLONE. CI installs it and sets OPTLAB_REQUIRE_RCLONE_TESTS."""
    found = os.environ.get("OPTLAB_RCLONE") or shutil.which("rclone")
    if not found:
        if os.environ.get("OPTLAB_REQUIRE_RCLONE_TESTS"):
            pytest.fail("rclone is not installed but the rclone tests are required")
        pytest.skip("rclone is not installed")
    return found
