"""Shared fixtures. Database tests need a TimescaleDB server named by OPTLAB_TEST_DATABASE_URL
(CI starts one; locally see infra/README.md). Each test gets a fresh database at the latest
migration, dropped afterwards."""

import os
import uuid
from collections.abc import Iterator
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


@pytest.fixture
def database_url() -> Iterator[str]:
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
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
