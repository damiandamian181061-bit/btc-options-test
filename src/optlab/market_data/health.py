"""Health checks that ping healthchecks.io, run as their own service next to the recorder.

- Recorder: every minute, ping ``HC_PING_URL_RECORDER`` only if ``option_quotes`` received rows
  in the last 2 minutes. A stopped or stuck recorder therefore stops the pings, and
  healthchecks.io emails Damian once the check's period and grace have passed.
- Disk: every 10 minutes, report how full the disk holding ``data/`` is to ``HC_PING_URL_DISK``.
  Below 70% it is a normal ping; at 70% a failure ping marked WARNING; at 85% a failure ping
  marked CRITICAL. healthchecks.io only emails when a check changes state, so on the way from
  WARNING to CRITICAL the check sends one success ping first to re-arm the alert, which makes
  sure the CRITICAL email goes out.

Ping URLs are secrets: they are held as ``SecretStr`` and never logged.
"""

import asyncio
import logging
import shutil
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from optlab.domain.clock import MS_PER_MINUTE, Clock, SystemClock

logger = logging.getLogger(__name__)

FRESH_WITHIN_MS = 2 * MS_PER_MINUTE
DISK_WARN = 0.70
DISK_CRITICAL = 0.85


class Pinger(Protocol):
    async def success(self, message: str) -> bool: ...

    async def fail(self, message: str) -> bool: ...


class HealthchecksPinger:
    """POSTs to a healthchecks.io ping URL (``/fail`` for failures) with a short message."""

    def __init__(self, name: str, url: SecretStr | None, timeout_s: float = 10.0) -> None:
        self.name = name
        self._url = url
        self._timeout_s = timeout_s
        if url is None:
            logger.warning("%s: no ping URL configured; pings are skipped", name)
        elif not url.get_secret_value().startswith("https://"):
            raise ValueError(f"{name}: the ping URL must start with https://")

    async def success(self, message: str) -> bool:
        return await self._ping("", message)

    async def fail(self, message: str) -> bool:
        return await self._ping("/fail", message)

    async def _ping(self, suffix: str, message: str) -> bool:
        if self._url is None:
            return False
        url = self._url.get_secret_value().rstrip("/") + suffix
        try:
            await asyncio.to_thread(self._post, url, message.encode()[:10_000])
        except OSError as exc:
            # Never include the URL: it is a secret. urllib errors don't contain it.
            logger.warning("%s: ping failed (%s)", self.name, type(exc).__name__)
            return False
        logger.info("%s: pinged %s", self.name, "fail" if suffix else "ok")
        return True

    def _post(self, url: str, body: bytes) -> None:
        request = urllib.request.Request(url, data=body, method="POST")  # noqa: S310 - https only
        with urllib.request.urlopen(request, timeout=self._timeout_s) as response:  # noqa: S310
            response.read()


# ------------------------------------------------------------------------------------ recorder


async def check_recorder(
    rows_since: Callable[[int], Awaitable[tuple[int, object]]],
    pinger: Pinger,
    now_ms: int,
) -> bool:
    """Ping only if option_quotes got rows in the last 2 minutes. True if it pinged."""
    count, newest = await rows_since(now_ms - FRESH_WITHIN_MS)
    if count <= 0:
        logger.warning("recorder: no option_quotes rows in the last 2 minutes; not pinging")
        return False
    return await pinger.success(f"{count} option_quotes rows in 2 minutes, newest {newest}")


# ------------------------------------------------------------------------------------ disk


class DiskLevel(IntEnum):
    OK = 0
    WARNING = 1
    CRITICAL = 2


@dataclass(frozen=True)
class DiskUsage:
    total_bytes: int
    used_bytes: int

    @property
    def fraction(self) -> float:
        return self.used_bytes / self.total_bytes if self.total_bytes else 1.0

    def describe(self) -> str:
        gb = 1024**3
        return (
            f"disk {self.fraction:.1%} full ({self.used_bytes / gb:.1f} of "
            f"{self.total_bytes / gb:.1f} GiB); warning at {DISK_WARN:.0%}, "
            f"critical at {DISK_CRITICAL:.0%}"
        )


def disk_usage(path: Path) -> DiskUsage:
    usage = shutil.disk_usage(path)
    return DiskUsage(total_bytes=usage.total, used_bytes=usage.total - usage.free)


def disk_level(fraction: float) -> DiskLevel:
    if fraction >= DISK_CRITICAL:
        return DiskLevel.CRITICAL
    if fraction >= DISK_WARN:
        return DiskLevel.WARNING
    return DiskLevel.OK


class DiskAlerter:
    """Turns disk usage into pings, re-arming the alert when WARNING becomes CRITICAL."""

    def __init__(self, pinger: Pinger) -> None:
        self._pinger = pinger
        self.level: DiskLevel | None = None

    async def check(self, usage: DiskUsage) -> DiskLevel:
        level = disk_level(usage.fraction)
        message = usage.describe()
        if level is DiskLevel.OK:
            await self._pinger.success(message)
        else:
            logger.warning("%s %s", level.name, message)
            if self.level is not None and DiskLevel.OK < self.level < level:
                await self._pinger.success(f"re-arming the alert before {level.name}: {message}")
            await self._pinger.fail(f"{level.name}: {message}")
        self.level = level
        return level


# ------------------------------------------------------------------------------------ service


async def run_health(
    rows_since: Callable[[int], Awaitable[tuple[int, object]]],
    recorder_pinger: Pinger,
    disk_pinger: Pinger,
    disk_path: Path,
    *,
    clock: Clock | None = None,
    recorder_every_s: float = 60.0,
    disk_every_s: float = 600.0,
) -> None:
    """Run both checks forever."""
    clock = clock or SystemClock()
    alerter = DiskAlerter(disk_pinger)

    async def recorder_loop() -> None:
        while True:
            try:
                await check_recorder(rows_since, recorder_pinger, clock.now_ms())
            except Exception as exc:
                logger.error("recorder check failed (%s); not pinging", type(exc).__name__)
            await clock.sleep(recorder_every_s)

    async def disk_loop() -> None:
        while True:
            try:
                await alerter.check(disk_usage(disk_path))
            except OSError as exc:
                logger.error("disk check failed (%s)", exc)
            await clock.sleep(disk_every_s)

    await asyncio.gather(recorder_loop(), disk_loop())
