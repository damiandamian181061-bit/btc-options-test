"""Health pings: recorder only when fresh rows exist; disk warnings at 70% and 85%."""

import logging
from dataclasses import dataclass, field

import pytest
from pydantic import SecretStr

from optlab.market_data.health import (
    DiskAlerter,
    DiskLevel,
    DiskUsage,
    HealthchecksPinger,
    check_recorder,
    disk_level,
)

NOW = 1_794_500_000_000


@dataclass
class FakePinger:
    pings: list[tuple[str, str]] = field(default_factory=list)

    async def success(self, message: str) -> bool:
        self.pings.append(("ok", message))
        return True

    async def fail(self, message: str) -> bool:
        self.pings.append(("fail", message))
        return True


async def test_recorder_ping_only_when_rows_were_written_in_the_last_2_minutes() -> None:
    asked: list[int] = []

    async def fresh(since_ms: int) -> tuple[int, object]:
        asked.append(since_ms)
        return 1532, "2026-11-12 22:01:00+00"

    async def stale(since_ms: int) -> tuple[int, object]:
        return 0, None

    pinger = FakePinger()
    assert await check_recorder(fresh, pinger, NOW)
    assert asked == [NOW - 120_000]
    assert pinger.pings[0][0] == "ok"
    assert "1532" in pinger.pings[0][1]
    assert not await check_recorder(stale, pinger, NOW)
    assert len(pinger.pings) == 1  # no ping at all, not even a failure


@pytest.mark.parametrize(
    ("fraction", "level"),
    [
        (0.0, DiskLevel.OK),
        (0.6999, DiskLevel.OK),
        (0.70, DiskLevel.WARNING),
        (0.8499, DiskLevel.WARNING),
        (0.85, DiskLevel.CRITICAL),
        (1.0, DiskLevel.CRITICAL),
    ],
)
def test_disk_levels(fraction: float, level: DiskLevel) -> None:
    assert disk_level(fraction) is level


def usage(fraction: float) -> DiskUsage:
    total = 80 * 1024**3
    return DiskUsage(total_bytes=total, used_bytes=round(total * fraction))


async def test_disk_alerts_warn_then_rearm_for_critical_then_recover() -> None:
    pinger = FakePinger()
    alerter = DiskAlerter(pinger)
    await alerter.check(usage(0.30))
    await alerter.check(usage(0.72))
    await alerter.check(usage(0.73))
    await alerter.check(usage(0.86))
    await alerter.check(usage(0.40))
    kinds = [k for k, _ in pinger.pings]
    assert kinds == ["ok", "fail", "fail", "ok", "fail", "ok"]
    assert pinger.pings[1][1].startswith("WARNING")
    assert "re-arming" in pinger.pings[3][1]
    assert pinger.pings[4][1].startswith("CRITICAL")
    assert "86.0% full" in pinger.pings[4][1]


async def test_straight_to_critical_needs_no_rearm() -> None:
    pinger = FakePinger()
    alerter = DiskAlerter(pinger)
    await alerter.check(usage(0.10))
    await alerter.check(usage(0.90))
    assert [k for k, _ in pinger.pings] == ["ok", "fail"]


async def test_ping_urls_are_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    private_path = "never-log-this-path"
    # Port 9 on localhost refuses connections, so the ping fails and logs a warning.
    pinger = HealthchecksPinger("disk check", SecretStr(f"https://127.0.0.1:9/{private_path}"))
    with caplog.at_level(logging.DEBUG):
        assert not await pinger.fail("CRITICAL: test")
    assert "ping failed" in caplog.text
    assert private_path not in caplog.text
    assert private_path not in repr(pinger.__dict__)


async def test_missing_ping_url_skips_pings(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        pinger = HealthchecksPinger("recorder check", None)
    assert "no ping URL" in caplog.text
    assert not await pinger.success("x")


def test_plain_http_ping_urls_are_refused() -> None:
    with pytest.raises(ValueError, match="https"):
        HealthchecksPinger("disk check", SecretStr("http://hc-ping.com/abc"))
