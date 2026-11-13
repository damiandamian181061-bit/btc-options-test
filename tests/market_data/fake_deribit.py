"""A local WebSocket server that speaks enough of Deribit's JSON-RPC to test the adapter.

It answers set_heartbeat, test, subscribe, unsubscribe, get_instruments and
get_delivery_prices; after each subscribe it replays the recorded-format notifications for the
accepted channels. Tests can drop or close every connection, mute the server, push extra
messages and inspect every request it received.
"""

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "deribit"


def load_session() -> list[dict[str, Any]]:
    lines = (FIXTURES / "session.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def load_instruments() -> dict[str, list[dict[str, Any]]]:
    data: dict[str, list[dict[str, Any]]] = json.loads(
        (FIXTURES / "instruments.json").read_text(encoding="utf-8")
    )
    return data


def load_delivery_prices() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES / "delivery_prices.json").read_text(encoding="utf-8")
    )
    return data


class FakeDeribit:
    def __init__(
        self,
        *,
        replay: list[dict[str, Any]] | None = None,
        reject: set[str] | None = None,
        instruments: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.replay = load_session() if replay is None else replay
        self.reject = reject or set()
        self.instruments = load_instruments() if instruments is None else instruments
        self.delivery_prices = load_delivery_prices()
        self.requests: list[dict[str, Any]] = []
        self.accepted_connections = 0
        self.muted = False
        self._sockets: set[ServerConnection] = set()
        self._server: Server | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        port = next(iter(self._server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}"

    async def __aenter__(self) -> "FakeDeribit":
        self._server = await serve(self._handler, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    # ----------------------------------------------------------------- test controls

    @property
    def open_connections(self) -> int:
        return len(self._sockets)

    def methods(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r.get("method") == name]

    def subscribed_channels(self) -> list[str]:
        return [c for r in self.methods("public/subscribe") for c in r["params"]["channels"]]

    async def drop_all(self) -> None:
        """Cut every connection without a close frame, like a network failure."""
        for ws in list(self._sockets):
            ws.transport.abort()

    async def close_all(self, code: int, reason: str = "") -> None:
        for ws in list(self._sockets):
            await ws.close(code, reason)

    async def send_all(self, message: dict[str, Any] | str) -> None:
        text = message if isinstance(message, str) else json.dumps(message)
        for ws in list(self._sockets):
            await ws.send(text)

    async def test_request(self) -> None:
        await self.send_all(
            {"jsonrpc": "2.0", "method": "heartbeat", "params": {"type": "test_request"}}
        )

    async def wait_for(self, predicate: Any, timeout: float = 5.0) -> None:
        async def poll() -> None:
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), timeout)

    # ----------------------------------------------------------------- protocol

    async def _handler(self, ws: ServerConnection) -> None:
        self.accepted_connections += 1
        self._sockets.add(ws)
        tasks: set[asyncio.Task[None]] = set()
        try:
            async for raw in ws:
                message = json.loads(raw)
                self.requests.append(message)
                if not self.muted:
                    # Answer concurrently, so a slow request can finish after a fast one.
                    task = asyncio.create_task(self._respond_safely(ws, message))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        except ConnectionClosed:
            pass
        finally:
            self._sockets.discard(ws)
            for task in tasks:
                task.cancel()

    async def _respond_safely(self, ws: ServerConnection, msg: dict[str, Any]) -> None:
        with contextlib.suppress(ConnectionClosed):
            await self._respond(ws, msg)

    async def _respond(self, ws: ServerConnection, msg: dict[str, Any]) -> None:
        method, params, request_id = msg["method"], msg.get("params", {}), msg["id"]
        notifications: list[dict[str, Any]] = []
        error: dict[str, Any] | None = None
        result: Any = None
        if method == "public/set_heartbeat":
            result = "ok"
        elif method == "public/test":
            result = {"version": "fake"}
        elif method == "public/get_time":
            await asyncio.sleep(params.get("delay", 0))
            result = params.get("tag")
        elif method == "public/subscribe":
            result = [c for c in params["channels"] if c not in self.reject]
            notifications = [n for n in self.replay if n["params"].get("channel") in result]
        elif method == "public/unsubscribe":
            result = params["channels"]
        elif method == "public/get_instruments":
            result = self.instruments.get(f"{params['currency']}/{params['kind']}", [])
        elif method == "public/get_delivery_prices":
            result = self.delivery_prices
        elif method == "public/fail":
            error = {"code": 10028, "message": "too_many_requests"}
        else:
            error = {"code": -32601, "message": "Method not found"}
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "testnet": False}
        if error is not None:
            body["error"] = error
        else:
            body["result"] = result
        await ws.send(json.dumps(body))
        for notification in notifications:
            await ws.send(json.dumps(notification))
