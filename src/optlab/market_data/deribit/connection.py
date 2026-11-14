"""One WebSocket connection to Deribit's public API (JSON-RPC 2.0), kept alive for good.

- Requests get increasing ids; each id maps to a future that the response resolves.
- ``public/set_heartbeat`` runs on every connect. The server then sends ``test_request`` when it
  has heard nothing from us for a while, and we must answer with ``public/test`` or it closes
  the socket (close code 4000).
- If nothing at all arrives for ``silence_timeout_s`` the connection is treated as dead.
- After any disconnect it reconnects with exponential backoff and jitter, then resubscribes
  every channel it carries.
- Only ``public/`` methods can be called: there is no key and no private access.
"""

import asyncio
import itertools
import logging
import random
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any

import msgspec
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from optlab.domain.clock import Clock, SystemClock
from optlab.domain.market import ConnectionEvent, ConnectionEventKind
from optlab.market_data.deribit import wire
from optlab.market_data.deribit.channels import batches
from optlab.market_data.deribit.rate_limit import RateLimits

logger = logging.getLogger(__name__)

PUBLIC_WS_URL = "wss://www.deribit.com/ws/api/v2"
MAX_FRAME_BYTES = 16 * 1024 * 1024
HEARTBEAT_CLOSE_CODE = 4000


class DeribitError(Exception):
    """Base class for errors talking to Deribit."""


class DeribitRpcError(DeribitError):
    """Deribit answered a request with a JSON-RPC error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"Deribit error {code}: {message}")
        self.code = code
        self.message = message


class NotConnectedError(DeribitError):
    """The connection is down; the request was not sent or its answer was lost."""


class SilenceTimeoutError(DeribitError):
    """Nothing arrived for too long: the connection is presumed dead."""


@dataclass(frozen=True)
class Backoff:
    """Reconnect delays: ``initial_s * factor**attempt``, capped at ``max_s``, with +/- jitter."""

    initial_s: float = 1.0
    factor: float = 2.0
    max_s: float = 60.0
    jitter: float = 0.2
    stable_s: float = 60.0  # a connection that stayed up this long resets the attempt count

    def delay(self, attempt: int, rng: random.Random) -> float:
        base = min(self.max_s, self.initial_s * self.factor ** min(attempt, 30))
        return max(0.0, base * (1.0 + self.jitter * (2.0 * rng.random() - 1.0)))


NotificationHandler = Callable[[str, msgspec.Raw, int], None]
EventHandler = Callable[[ConnectionEvent], None]
ChannelsHandler = Callable[[str, frozenset[str]], None]


def _close_frame(code: int, reason: str) -> str:
    label = " (heartbeat not answered)" if code == HEARTBEAT_CLOSE_CODE else ""
    return f"code {code}{label} {reason}".strip()


def _describe(exc: BaseException) -> str:
    """Why a connection ended, including which side closed it.

    A WebSocket close is a handshake: one side sends a close frame and the other answers. The
    library records the frame received (``rcvd``), the frame sent (``sent``) and which came
    first. With neither, the TCP connection simply ended: the server, a router or the network
    dropped it. With only ``sent``, we closed it (for example the library's own keepalive
    timeout, which sends 1011) and got no answer.
    """
    if isinstance(exc, ConnectionClosed):
        rcvd, sent = exc.rcvd, exc.sent
        if sent is not None and (rcvd is None or not exc.rcvd_then_sent):
            text = f"closed by us: sent {_close_frame(sent.code, sent.reason)}"
            if rcvd is None:
                return text + "; no answer from the server"
            return text + f"; server answered {rcvd.code}"
        if rcvd is not None:
            text = f"closed by server: {_close_frame(rcvd.code, rcvd.reason)}"
            return text + ("" if sent is None else f"; we answered {sent.code}")
        return (
            "connection closed without a close frame (none sent or received: the TCP "
            "connection ended)"
        )
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class DeribitConnection:
    """A self-healing WebSocket connection carrying a set of channels."""

    def __init__(
        self,
        name: str,
        *,
        url: str,
        on_notification: NotificationHandler,
        on_event: EventHandler,
        on_lost: ChannelsHandler,
        on_rejected: ChannelsHandler,
        limits: RateLimits,
        heartbeat_s: int = 10,
        silence_timeout_s: float | None = None,
        call_timeout_s: float = 10.0,
        backoff: Backoff | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if not 10 <= heartbeat_s <= 300:
            raise ValueError("Deribit accepts heartbeat intervals of 10 to 300 seconds")
        self.name = name
        self._url = url
        self._on_notification = on_notification
        self._on_event = on_event
        self._on_lost = on_lost
        self._on_rejected = on_rejected
        self._limits = limits
        self._heartbeat_s = heartbeat_s
        self._silence_timeout_s = silence_timeout_s or 3.0 * heartbeat_s
        self._call_timeout_s = call_timeout_s
        self._backoff = backoff or Backoff()
        self._clock = clock or SystemClock()
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not cryptography
        self._channels: set[str] = set()
        self._pending: dict[int, asyncio.Future[msgspec.Raw]] = {}
        self._ids = itertools.count(1)
        self._ws: ClientConnection | None = None
        self._connected = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self.bad_messages = 0

    # ----------------------------------------------------------------- public interface

    @property
    def channels(self) -> frozenset[str]:
        return frozenset(self._channels)

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name=f"deribit-{self.name}")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._background) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None

    async def wait_connected(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout)

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> msgspec.Raw:
        """Send a request and return the raw ``result`` (adapter-internal)."""
        ws = self._ws
        if ws is None:
            raise NotConnectedError(f"{self.name} is not connected")
        return await self._call_on(ws, method, params, timeout)

    async def subscribe(self, channels: Iterable[str]) -> frozenset[str]:
        """Carry these channels from now on, through reconnects. Returns those Deribit refused."""
        new = sorted(c for c in set(channels) if c not in self._channels)
        self._channels.update(new)
        ws = self._ws
        if ws is None or not new:
            return frozenset()  # subscribed when the connection (re)opens
        rejected = await self._subscribe_on(ws, new)
        self._emit(ConnectionEventKind.SUBSCRIBED, f"{len(new) - len(rejected)} more channels")
        return rejected

    async def unsubscribe(self, channels: Iterable[str]) -> None:
        gone = sorted(c for c in set(channels) if c in self._channels)
        self._channels.difference_update(gone)
        ws = self._ws
        if ws is None or not gone:
            return
        for batch in batches(gone):
            try:
                await self._call_on(ws, "public/unsubscribe", {"channels": batch})
            except (DeribitError, ConnectionClosed, TimeoutError) as exc:
                logger.warning("%s: unsubscribe failed (%s)", self.name, _describe(exc))

    # ----------------------------------------------------------------- connection lifecycle

    async def run(self) -> None:
        """Connect, serve, and reconnect after every failure until cancelled."""
        attempt = 0
        while True:
            up_since: float | None = None
            reason = "connection ended"
            try:
                async with connect(
                    self._url,
                    max_size=MAX_FRAME_BYTES,
                    open_timeout=15,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    self._ws = ws
                    up_since = self._clock.monotonic()
                    self._emit(ConnectionEventKind.CONNECTED, "")
                    await self._serve(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = _describe(exc) if up_since is not None else f"connect failed: {exc!r}"
            finally:
                self._ws = None
                self._connected.clear()
                self._fail_pending()
                if up_since is not None:
                    self._on_lost(self.name, frozenset(self._channels))
            self._emit(ConnectionEventKind.DISCONNECTED, reason)
            stable = self._clock.monotonic() - (up_since or 0.0) >= self._backoff.stable_s
            if up_since is not None and stable:
                attempt = 0
            delay = self._backoff.delay(attempt, self._rng)
            attempt += 1
            self._emit(ConnectionEventKind.RECONNECT_SCHEDULED, f"in {delay:.1f}s")
            await self._clock.sleep(delay)

    async def _serve(self, ws: ClientConnection) -> None:
        reader = asyncio.create_task(self._read_loop(ws), name=f"deribit-{self.name}-reader")
        try:
            setup = asyncio.create_task(self._setup(ws))
            done, _ = await asyncio.wait({reader, setup}, return_when=asyncio.FIRST_COMPLETED)
            if setup in done:
                setup.result()  # raise if setup failed
                self._connected.set()
            else:
                setup.cancel()
                await asyncio.gather(setup, return_exceptions=True)
            await reader  # returns only by raising: the connection is over
        finally:
            if not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    async def _setup(self, ws: ClientConnection) -> None:
        await self._call_on(ws, "public/set_heartbeat", {"interval": self._heartbeat_s})
        channels = sorted(self._channels)
        if channels:
            rejected = await self._subscribe_on(ws, channels)
            self._emit(ConnectionEventKind.SUBSCRIBED, f"{len(channels) - len(rejected)} channels")

    async def _read_loop(self, ws: ClientConnection) -> None:
        while True:
            try:
                frame = await asyncio.wait_for(ws.recv(decode=False), self._silence_timeout_s)
            except TimeoutError:
                raise SilenceTimeoutError(
                    f"nothing received for {self._silence_timeout_s:.0f}s"
                ) from None
            self._handle(frame, self._clock.now_ms())

    # ----------------------------------------------------------------- messages

    def _handle(self, frame: bytes | str, recv_ms: int) -> None:
        try:
            env = wire.ENVELOPE.decode(frame)
            if env.method == "subscription" and env.params:
                params = wire.SUBSCRIPTION_PARAMS.decode(env.params)
                try:
                    self._on_notification(params.channel, params.data, recv_ms)
                except Exception:
                    logger.exception("%s: handler failed for %s", self.name, params.channel)
            elif env.method == "heartbeat" and env.params:
                if wire.HEARTBEAT_PARAMS.decode(env.params).type == "test_request":
                    self._spawn(self._answer_test_request())
            elif env.id is not None:
                self._resolve(env.id, env)
            else:
                self._bad(f"unexpected message: {frame[:200]!r}")
        except msgspec.DecodeError as exc:
            self._bad(f"undecodable message ({exc}): {frame[:200]!r}")

    def _resolve(self, request_id: int, env: wire.Envelope) -> None:
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        if env.error is not None:
            future.set_exception(DeribitRpcError(env.error.code, env.error.message))
        else:
            future.set_result(env.result if env.result else msgspec.Raw(b"null"))

    def _bad(self, what: str) -> None:
        self.bad_messages += 1
        if self.bad_messages <= 10 or self.bad_messages % 1000 == 0:
            logger.warning("%s: %s (total %d)", self.name, what, self.bad_messages)

    async def _answer_test_request(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await self._call_on(ws, "public/test", {})
        except (DeribitError, ConnectionClosed, TimeoutError) as exc:
            logger.warning("%s: heartbeat reply failed (%s)", self.name, _describe(exc))

    # ----------------------------------------------------------------- requests

    async def _call_on(
        self,
        ws: ClientConnection,
        method: str,
        params: dict[str, Any],
        timeout: float | None = None,
    ) -> msgspec.Raw:
        if not method.startswith("public/"):
            raise ValueError(f"only public methods are allowed, not {method!r}")
        await self._limits.acquire(method)
        request_id = next(self._ids)
        future: asyncio.Future[msgspec.Raw] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            try:
                await ws.send(msgspec.json.encode(request).decode())
            except ConnectionClosed as exc:
                raise NotConnectedError(f"{self.name}: {_describe(exc)}") from exc
            return await asyncio.wait_for(future, timeout or self._call_timeout_s)
        finally:
            self._pending.pop(request_id, None)

    async def _subscribe_on(self, ws: ClientConnection, channels: list[str]) -> frozenset[str]:
        rejected: set[str] = set()
        for batch in batches(channels):
            raw = await self._call_on(ws, "public/subscribe", {"channels": batch})
            accepted = set(wire.CHANNEL_LIST.decode(raw))
            rejected.update(c for c in batch if c not in accepted)
        if rejected:
            self._channels.difference_update(rejected)
            sample = ", ".join(sorted(rejected)[:5])
            self._emit(
                ConnectionEventKind.SUBSCRIBE_REJECTED, f"{len(rejected)} refused, e.g. {sample}"
            )
            self._on_rejected(self.name, frozenset(rejected))
        return frozenset(rejected)

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(NotConnectedError(f"{self.name}: connection lost"))
        self._pending.clear()

    # ----------------------------------------------------------------- helpers

    def _emit(self, kind: ConnectionEventKind, detail: str) -> None:
        event = ConnectionEvent(
            ts_ms=self._clock.now_ms(), source=self.name, kind=kind, detail=detail
        )
        log = logger.warning if kind is ConnectionEventKind.DISCONNECTED else logger.info
        log("%s %s %s", self.name, kind.value, detail)
        try:
            self._on_event(event)
        except Exception:
            logger.exception("%s: event handler failed", self.name)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
