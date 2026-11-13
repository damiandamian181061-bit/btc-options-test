"""Alembic environment: migrations run online against DATABASE_URL.

The URL comes from ``optlab.config.Settings`` (so ``LIVE_TRADING=true`` stops migrations too),
or from ``config.attributes["database_url"]`` when tests call Alembic directly. It is never
printed or logged.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from optlab.config import Settings
from optlab.market_data.store import sqlalchemy_url

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    url = config.attributes.get("database_url")
    if url:
        return str(url)
    secret = Settings().database_url
    if secret is None:
        raise SystemExit("DATABASE_URL is not set")
    return secret.get_secret_value()


def run_migrations_online() -> None:
    engine = create_engine(sqlalchemy_url(_database_url()), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported")
run_migrations_online()
