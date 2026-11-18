"""Entry points for the long-running services: ``optlab-recorder`` and ``optlab-health``.

Both load ``optlab.config.Settings`` first, so they refuse to start when ``LIVE_TRADING`` is
true. Non-secret tuning comes from ``OPTLAB_*`` environment variables (``ServiceSettings``).
Both stop cleanly on SIGTERM (``docker compose stop``) and SIGINT.
"""

import asyncio
import logging
import signal
import sys
import time
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from optlab.config import Settings
from optlab.domain.clock import MS_PER_SECOND
from optlab.market_data import schema
from optlab.market_data.deribit import PUBLIC_WS_URL, DeribitClient
from optlab.market_data.health import HealthchecksPinger, run_health
from optlab.market_data.recorder import Recorder, RecorderConfig
from optlab.market_data.sink import MarketDataSink
from optlab.market_data.store import RecorderStore

logger = logging.getLogger("optlab")


class ServiceSettings(BaseSettings):
    """Non-secret settings for the recorder and health services (``OPTLAB_`` prefix)."""

    model_config = SettingsConfigDict(env_prefix="OPTLAB_", extra="ignore", frozen=True)

    data_dir: Path = Path("data")
    deribit_ws_url: str = PUBLIC_WS_URL
    ticker_interval: Literal["100ms", "agg2"] = "100ms"
    max_channels_per_connection: int = 400
    heartbeat_s: int = 10
    snapshot_max_age_s: int = 300
    raw_daily_warn_bytes: int = 1_000_000_000
    log_level: str = "INFO"


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.Formatter.converter = time.gmtime  # log times in UTC
    for noisy in ("websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _run(main: Coroutine[Any, Any, None]) -> None:
    """Run ``main`` until it ends or a SIGTERM/SIGINT cancels it."""

    async def runner() -> None:
        task = asyncio.ensure_future(main)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            logger.info("stopped by signal")

    asyncio.run(runner())


def _database_url(settings: Settings) -> str:
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is not set: the recorder needs the database")
    return settings.database_url.get_secret_value()


# ------------------------------------------------------------------------------------ recorder


async def run_recorder(settings: Settings, service: ServiceSettings) -> None:
    url = _database_url(settings)
    # Starts on any revision that leaves the recorder's own tables as this code expects.
    revision = schema.check_url(url, schema.RECORDER_TABLES, "the recorder")
    logger.info("database at revision %s; the recorder's tables are as expected", revision)
    store = RecorderStore(url)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    config = RecorderConfig(
        data_dir=service.data_dir,
        ticker_interval=service.ticker_interval,
        max_age_ms=service.snapshot_max_age_s * MS_PER_SECOND,
        raw_daily_warn_bytes=service.raw_daily_warn_bytes,
    )

    def make_client(sink: MarketDataSink) -> DeribitClient:
        return DeribitClient(
            sink,
            url=service.deribit_ws_url,
            max_channels_per_connection=service.max_channels_per_connection,
            heartbeat_s=service.heartbeat_s,
        )

    recorder = Recorder(store, make_client, config, run_id=run_id)
    logger.info("recorder run %s starting; raw files under %s", run_id, config.raw_dir)
    try:
        await recorder.run()
    finally:
        await store.close()


def recorder_main() -> None:
    settings = Settings()  # refuses LIVE_TRADING=true
    service = ServiceSettings()
    _configure_logging(service.log_level)
    _run(run_recorder(settings, service))


# ------------------------------------------------------------------------------------ health


async def run_health_service(settings: Settings, service: ServiceSettings) -> None:
    store = RecorderStore(_database_url(settings), application_name="optlab-health")

    async def rows_since(since_ms: int) -> tuple[int, object]:
        try:
            count, newest = await store.option_rows_since(since_ms)
        except Exception:
            await store.reset()
            raise
        return count, newest

    try:
        await run_health(
            rows_since,
            HealthchecksPinger("recorder check", settings.hc_ping_url_recorder),
            HealthchecksPinger("disk check", settings.hc_ping_url_disk),
            service.data_dir,
        )
    finally:
        await store.close()


def health_main() -> None:
    settings = Settings()  # refuses LIVE_TRADING=true
    service = ServiceSettings()
    _configure_logging(service.log_level)
    logger.info("health checks starting")
    _run(run_health_service(settings, service))
