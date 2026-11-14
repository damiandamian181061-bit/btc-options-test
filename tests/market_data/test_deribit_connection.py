"""One connection against the fake server: JSON-RPC ids, heartbeat, reconnect, resubscribe."""

import asyncio

import msgspec
import pytest

from optlab.domain.market import ConnectionEvent
from optlab.market_data.deribit.connection import (
    Backoff,
    DeribitConnection,
    DeribitRpcError,
)
from optlab.market_data.deribit.rate_limit import RateLimits
from tests.market_data.fake_deribit import FakeDeribit
from tests.market_data.helpers import wait_until

TICKERS = ["ticker.BTC-20NOV26-70000-C.100ms", "ticker.BTC-PERPETUAL.100ms"]


class Probe:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, bytes, int]] = []
        self.events: list[ConnectionEvent] = []
        self.lost: list[frozenset[str]] = []
        self.rejected: list[frozenset[str]] = []

    def connection(self, url: str, **kwargs: object) -> DeribitConnection:
        options: dict[str, object] = {
            "backoff": Backoff(initial_s=0.01, max_s=0.05, jitter=0.0),
            "call_timeout_s": 2.0,
        }
        options.update(kwargs)
        return DeribitConnection(
            "test-ws",
            url=url,
            on_notification=lambda ch, data, ms: self.notifications.append((ch, bytes(data), ms)),
            on_event=self.events.append,
            on_lost=lambda _name, chs: self.lost.append(chs),
            on_rejected=lambda _name, chs: self.rejected.append(chs),
            limits=RateLimits(),
            **options,  # type: ignore[arg-type]
        )

    def kinds(self) -> list[str]:
        return [e.kind.value for e in self.events]


async def test_connect_sets_heartbeat_and_replays_subscriptions() -> None:
    probe = Probe()
    async with FakeDeribit() as server:
        conn = probe.connection(server.url)
        await conn.subscribe(TICKERS)  # before connecting: subscribed on connect
        conn.start()
        try:
            await conn.wait_connected(5)
            await wait_until(lambda: len(probe.notifications) >= 2)
            heartbeat = server.methods("public/set_heartbeat")
            assert heartbeat[0]["params"] == {"interval": 10}
            assert sorted(server.subscribed_channels()) == sorted(TICKERS)
            assert {n[0] for n in probe.notifications} == set(TICKERS)
            assert all(
                isinstance(n[2], int) and n[2] > 1_700_000_000_000 for n in probe.notifications
            )
            assert probe.kinds()[:2] == ["connected", "subscribed"]
        finally:
            await conn.stop()


async def test_request_ids_map_to_their_own_responses() -> None:
    probe = Probe()
    async with FakeDeribit() as server:
        conn = probe.connection(server.url)
        conn.start()
        try:
            await conn.wait_connected(5)
            slow = asyncio.create_task(conn.call("public/get_time", {"delay": 0.2, "tag": "slow"}))
            fast = asyncio.create_task(conn.call("public/get_time", {"delay": 0.0, "tag": "fast"}))
            done_first, _ = await asyncio.wait({slow, fast}, return_when="FIRST_COMPLETED")
            assert done_first == {fast}
            assert msgspec.json.decode(await fast) == "fast"
            assert msgspec.json.decode(await slow) == "slow"
            ids = [r["id"] for r in server.requests]
            assert len(ids) == len(set(ids))  # every request has its own id
        finally:
            await conn.stop()


async def test_rpc_errors_raise_with_deribits_code() -> None:
    probe = Probe()
    async with FakeDeribit() as server:
        conn = probe.connection(server.url)
        conn.start()
        try:
            await conn.wait_connected(5)
            with pytest.raises(DeribitRpcError) as info:
                await conn.call("public/fail", {})
            assert info.value.code == 10028
        finally:
            await conn.stop()


async def test_only_public_methods_can_be_called() -> None:
    probe = Probe()
    async with FakeDeribit() as server:
        conn = probe.connection(server.url)
        conn.start()
        try:
            await conn.wait_connected(5)
            for method in ("private/get_positions", "private/buy", "/api/v2/private/buy"):
                with pytest.raises(ValueError, match="only public methods"):
                    await conn.call(method, {})
            assert not [r for r in server.requests if not r["method"].startswith("public/")]
        finally:
            await conn.stop()


async def test_test_request_is_answered_with_public_test() -> None:
    probe = Probe()
    async with FakeDeribit() as server:
        conn = probe.connection(server.url)
        conn.start()
        try:
            await conn.wait_connected(5)
            await server.test_request()
            await wait_until(lambda: len(server.methods("public/test")) == 1)
            assert server.methods("public/test")[0]["params"] == {}
        finally:
            await conn.stop()


async def test_dropped_connection_reconnects_and_resubscribes() -> None:
    probe = Probe()
    async with FakeDeribit() as server:
        conn = probe.connection(server.url)
        await conn.subscribe(TICKERS)
        conn.start()
        try:
            await conn.wait_connected(5)
            await server.drop_all()
            await wait_until(lambda: server.accepted_connections == 2)
            await wait_until(lambda: len(server.methods("public/subscribe")) == 2)
            await wait_until(lambda: conn.is_connected)
            first, second = server.methods("public/subscribe")
            assert sorted(first["params"]["channels"]) == sorted(second["params"]["channels"])
            assert len(server.methods("public/set_heartbeat")) == 2
            assert probe.lost == [frozenset(TICKERS)]
            kinds = probe.kinds()
            i = kinds.index("disconnected")
            assert kinds[i : i + 4] == [
                "disconnected",
                "reconnect_scheduled",
                "connected",
                "subscribed",
            ]
        finally:
            await conn.stop()


async def test_heartbeat_close_code_is_recorded() -> None:
    probe = Probe()
    async with FakeDeribit() as server:
        conn = probe.connection(server.url)
        conn.start()
        try:
            await conn.wait_connected(5)
            await server.close_all(4000, "heartbeat")
            await wait_until(lambda: "disconnected" in probe.kinds())
            detail = next(e.detail for e in probe.events if e.kind.value == "disconnected")
            assert "4000" in detail
            assert "heartbeat" in detail
            await wait_until(lambda: server.accepted_connections == 2)
        finally:
            await conn.stop()


@pytest.mark.parametrize(
    ("rcvd", "sent", "rcvd_then_sent", "expected"),
    [
        (None, None, None, "connection closed without a close frame (none sent or received"),
        ((4000, "heartbeat"), (4000, ""), True, "closed by server: code 4000 (heartbeat not"),
        ((1001, "going away"), None, None, "closed by server: code 1001 going away"),
        (None, (1011, "keepalive ping timeout"), None, "closed by us: sent code 1011 keepalive"),
        ((1000, ""), (1000, "bye"), False, "closed by us: sent code 1000 bye; server answered"),
    ],
)
def test_disconnects_say_which_side_closed(
    rcvd: tuple[int, str] | None,
    sent: tuple[int, str] | None,
    rcvd_then_sent: bool | None,
    expected: str,
) -> None:
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    from optlab.market_data.deribit.connection import _describe

    exc = ConnectionClosedError(
        None if rcvd is None else Close(*rcvd),
        None if sent is None else Close(*sent),
        rcvd_then_sent,
    )
    assert _describe(exc).startswith(expected)


async def test_a_dropped_tcp_connection_is_described_as_such() -> None:
    probe = Probe()
    async with FakeDeribit() as server:
        conn = probe.connection(server.url)
        conn.start()
        try:
            await conn.wait_connected(5)
            await server.drop_all()
            await wait_until(lambda: "disconnected" in probe.kinds())
            detail = next(e.detail for e in probe.events if e.kind.value == "disconnected")
            assert "none sent or received" in detail
        finally:
            await conn.stop()


async def test_silence_means_a_dead_connection() -> None:
    probe = Probe()
    async with FakeDeribit() as server:
        conn = probe.connection(server.url, silence_timeout_s=0.3)
        conn.start()
        try:
            await conn.wait_connected(5)
            server.muted = True  # the server stops sending anything
            await wait_until(lambda: "disconnected" in probe.kinds(), timeout=3)
            detail = next(e.detail for e in probe.events if e.kind.value == "disconnected")
            assert "nothing received" in detail
            server.muted = False
            await wait_until(lambda: conn.is_connected, timeout=3)
        finally:
            await conn.stop()


async def test_refused_channels_are_reported_and_dropped() -> None:
    probe = Probe()
    refused = "ticker.BTC-20NOV26-99999-C.100ms"
    async with FakeDeribit(reject={refused}) as server:
        conn = probe.connection(server.url)
        conn.start()
        try:
            await conn.wait_connected(5)
            rejected = await conn.subscribe([*TICKERS, refused])
            assert rejected == frozenset({refused})
            assert probe.rejected == [frozenset({refused})]
            assert refused not in conn.channels
            assert "subscribe_rejected" in probe.kinds()
        finally:
            await conn.stop()


async def test_garbage_messages_do_not_break_the_connection() -> None:
    probe = Probe()
    async with FakeDeribit() as server:
        conn = probe.connection(server.url)
        conn.start()
        try:
            await conn.wait_connected(5)
            await server.send_all("this is not json")
            await server.send_all({"jsonrpc": "2.0", "surprise": True})
            await wait_until(lambda: conn.bad_messages == 2)
            assert conn.is_connected
            assert msgspec.json.decode(await conn.call("public/test", {})) == {"version": "fake"}
        finally:
            await conn.stop()


async def test_unsubscribe_removes_channels_for_good() -> None:
    probe = Probe()
    async with FakeDeribit() as server:
        conn = probe.connection(server.url)
        conn.start()
        try:
            await conn.wait_connected(5)
            await conn.subscribe(TICKERS)
            await conn.unsubscribe([TICKERS[0]])
            assert server.methods("public/unsubscribe")[0]["params"]["channels"] == [TICKERS[0]]
            await server.drop_all()
            await wait_until(lambda: len(server.methods("public/subscribe")) == 2)
            assert server.methods("public/subscribe")[1]["params"]["channels"] == [TICKERS[1]]
        finally:
            await conn.stop()


def test_heartbeat_interval_must_be_valid() -> None:
    with pytest.raises(ValueError, match="10 to 300"):
        Probe().connection("ws://127.0.0.1:9", heartbeat_s=5)
