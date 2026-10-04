"""A cancelled write, or one pending when the client disconnects, is never sent later.

Suite rules 1 and 12 (clarified): when the MCP client cancels a call
(notifications/cancelled) or goes away (end of stdin), a write that has not
been sent yet (queued behind another call, still connecting, or waiting to
retry) must never reach the device afterwards.

The stdio tests run the real ``ethernetip-mcp`` CLI: against a fake controller
the test can black-hole (blackhole_server.py) for the CIP path, and against a
bridge port that only starts listening after the cancel for the JSON bridge.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
from conftest import TESTS_DIR, free_port, server_command, server_env
from fake_pycomm3 import FakeController

from ethernetip_mcp.eip_client import EIPClient, EIPClientConfig, EIPClientError

pytestmark = pytest.mark.integration

INIT = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}


def tool_call(request_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def cancel(request_id: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}}


class StdioServer:
    """The server as a child process, driven with raw JSON-RPC lines."""

    def __init__(self, command: list[str], env: dict[str, str], err_path: Path) -> None:
        self.err_path = err_path
        with err_path.open("wb") as err:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err,
                env={**os.environ, **env, "PYTHONUNBUFFERED": "1"},
            )
        self.responses: dict[int, dict[str, Any]] = {}
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout
        for line in self.process.stdout:
            message = json.loads(line)
            if "id" in message:
                self.responses[message["id"]] = message

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def initialize(self) -> None:
        self.send(INIT)
        self.wait(0)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def wait(self, request_id: int, seconds: float = 30.0) -> dict[str, Any]:
        end = time.monotonic() + seconds
        while request_id not in self.responses:
            assert time.monotonic() < end, f"no response to request {request_id}"
            time.sleep(0.02)
        return self.responses[request_id]

    def disconnect(self) -> None:
        assert self.process.stdin
        self.process.stdin.close()

    def finish(self, seconds: float = 30.0) -> int:
        try:
            return self.process.wait(seconds)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()

    def stderr(self) -> str:
        return self.err_path.read_text(errors="replace")


# -- CIP over stdio -----------------------------------------------------------


class BlackHole:
    def __init__(self, tmp_path: Path) -> None:
        self.gate = tmp_path / "peer_up"
        self.writes = tmp_path / "writes.log"

    def up(self) -> None:
        self.gate.touch()

    def down(self) -> None:
        self.gate.unlink(missing_ok=True)

    def written(self) -> list[str]:
        return self.writes.read_text().splitlines() if self.writes.exists() else []

    def server(self, tmp_path: Path) -> StdioServer:
        env = server_env(
            ENIP_HOST="10.0.0.5",
            ENIP_WRITES_ENABLED="true",
            ENIP_RETRY_BACKOFF_BASE="0.2",
            BH_GATE=str(self.gate),
            BH_WRITES=str(self.writes),
            BH_WAIT="1.5",
        )
        return StdioServer([sys.executable, str(TESTS_DIR / "blackhole_server.py")], env, tmp_path / "server.err")


def test_cancelled_write_queued_behind_a_read_is_never_sent(tmp_path: Path) -> None:
    hole = BlackHole(tmp_path)
    hole.up()  # reachable at startup, so the session exists
    server = hole.server(tmp_path)
    try:
        server.initialize()
        hole.down()
        server.send(tool_call(1, "read_tag", {"tag_name": "MotorSpeed"}))  # holds the session
        time.sleep(0.3)
        server.send(tool_call(2, "write_tag", {"tag_name": "Batch_Count", "value": 777}))  # queued
        time.sleep(0.4)
        server.send(cancel(2))
        time.sleep(0.4)
        hole.up()
        server.wait(1)
        time.sleep(2.0)  # time for an abandoned worker to have sent it
        assert hole.written() == []
        # The session still works for the next write.
        server.send(tool_call(3, "write_tag", {"tag_name": "Batch_Count", "value": 778}))
        envelope = server.wait(3)["result"]["structuredContent"]
        assert envelope["success"] is True
        assert len(hole.written()) == 1 and "778" in hole.written()[0]
    finally:
        server.disconnect()
        assert server.finish() == 0


def test_cancelled_write_that_is_still_connecting_is_never_sent(tmp_path: Path) -> None:
    hole = BlackHole(tmp_path)  # unreachable from the start: the write has to connect
    server = hole.server(tmp_path)
    try:
        server.initialize()
        server.send(tool_call(2, "write_tag", {"tag_name": "Batch_Count", "value": 777}))
        time.sleep(0.5)
        server.send(cancel(2))
        time.sleep(0.3)
        hole.up()
        time.sleep(3.0)  # longer than the connection retries would take
        assert hole.written() == []
    finally:
        server.disconnect()
        assert server.finish() == 0


@pytest.mark.parametrize("mode", ["queued", "connecting"])
def test_pending_write_is_never_sent_after_the_client_disconnects(tmp_path: Path, mode: str) -> None:
    hole = BlackHole(tmp_path)
    if mode == "queued":
        hole.up()
    server = hole.server(tmp_path)
    try:
        server.initialize()
        if mode == "queued":
            hole.down()
            server.send(tool_call(1, "read_tag", {"tag_name": "MotorSpeed"}))
            time.sleep(0.3)
        server.send(tool_call(2, "write_tag", {"tag_name": "Batch_Count", "value": 777}))
        time.sleep(0.5)
        server.disconnect()  # the client goes away
        time.sleep(0.3)
        hole.up()
        rc = server.finish()
        time.sleep(1.0)
    finally:
        if server.process.poll() is None:
            server.process.kill()
    assert hole.written() == []
    assert rc == 0
    assert "Traceback" not in server.stderr()


# -- JSON bridge over stdio ---------------------------------------------------


class LateBridge:
    """A JSON bridge port that starts listening only when told to, recording requests."""

    def __init__(self) -> None:
        self.port = free_port()
        self.received: list[dict[str, Any]] = []
        self.sock: socket.socket | None = None

    def start(self) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.port))
        self.sock.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        assert self.sock
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                line = conn.makefile("rb").readline()
                if line:
                    request = json.loads(line)
                    self.received.append(request)
                    data = {"tag": request.get("tag"), "value": request.get("value"), "data_type": "REAL"}
                    conn.sendall(json.dumps({"success": True, "data": data}).encode() + b"\n")

    def close(self) -> None:
        if self.sock:
            self.sock.close()

    def server(self, tmp_path: Path) -> StdioServer:
        env = server_env(
            ENIP_HOST="127.0.0.1",
            ENIP_PORT=str(self.port),
            ENIP_JSON_BRIDGE="true",
            ENIP_WRITES_ENABLED="true",
            ENIP_MAX_RETRIES="3",
            ENIP_RETRY_BACKOFF_BASE="0.5",  # retries at about 0.5, 1.5 and 3.5 s
        )
        return StdioServer(server_command(), env, tmp_path / "server.err")


@pytest.mark.parametrize("how", ["cancel", "disconnect"])
def test_bridge_write_waiting_to_retry_is_never_sent(tmp_path: Path, how: str) -> None:
    bridge = LateBridge()
    server = bridge.server(tmp_path)
    try:
        server.initialize()
        server.send(tool_call(1, "write_tag", {"tag_name": "Line_Speed", "value": 77.0}))
        time.sleep(0.3)  # the first connect was refused; the write waits to retry
        if how == "cancel":
            server.send(cancel(1))
        else:
            server.disconnect()
        time.sleep(0.1)
        bridge.start()  # the device is reachable again
        time.sleep(4.5)  # past every retry
        assert bridge.received == []
        if how == "cancel":
            server.send(tool_call(2, "read_tag", {"tag_name": "Line_Speed"}))
            assert server.wait(2)["result"]["structuredContent"]["success"] is True
            assert [r["op"] for r in bridge.received] == ["read"]
            server.disconnect()
        assert server.finish() == 0
        assert "Traceback" not in server.stderr()
    finally:
        if server.process.poll() is None:
            server.process.kill()
        bridge.close()


# -- the same guarantees in-process ------------------------------------------


def make_client(controller: FakeController, **config: Any) -> EIPClient:
    settings = {"max_retries": 0, "retry_backoff_base": 0.0, **config}
    return EIPClient(EIPClientConfig(**settings), driver_factory=controller.factory)


async def test_cancel_scope_on_a_queued_write_prevents_the_send() -> None:
    gate = threading.Event()
    controller = FakeController(read_gate=gate)
    client = make_client(controller)
    await client.ensure_connection()
    async with anyio.create_task_group() as tg:
        tg.start_soon(client.read_tag, "MotorSpeed")
        await anyio.sleep(0.2)
        with anyio.move_on_after(0.3):  # the client cancels the queued write
            await client.write_tag("Batch_Count", 5)
        gate.set()
    await anyio.sleep(0.5)
    assert controller.all_calls("write") == []
    await client.write_tag("Batch_Count", 6)  # later writes still work
    assert controller.all_calls("write") == [("write", (("Batch_Count", 6),))]


async def test_cancel_scope_while_connecting_prevents_the_send() -> None:
    gate = threading.Event()
    controller = FakeController(open_gate=gate)
    client = make_client(controller)
    with anyio.move_on_after(0.3):
        await client.write_tag("Batch_Count", 5)
    gate.set()
    await anyio.sleep(0.5)
    assert controller.all_calls("write") == []


async def test_queued_call_hitting_its_deadline_leaves_the_running_call_alone() -> None:
    gate = threading.Event()
    controller = FakeController(read_gate=gate)
    client = make_client(controller, deadline=5.0)
    await client.ensure_connection()
    results: dict[str, Any] = {}

    async def read() -> None:
        results["read"] = await client.read_tag("MotorSpeed")

    async with anyio.create_task_group() as tg:
        tg.start_soon(read)
        await anyio.sleep(0.2)
        client.config.deadline = 0.3  # the queued write gets a short deadline
        with pytest.raises(EIPClientError, match="nothing was sent") as info:
            await client.write_tag("Batch_Count", 5)
        assert info.value.meta["outcome"] == "not_sent"
        await anyio.sleep(0.1)
        gate.set()
    assert results["read"][0]["value"] == 1450.0  # its session was not closed under it
    assert controller.all_calls("write") == []


async def test_shutdown_stops_a_write_that_is_still_connecting() -> None:
    """The closing flag alone (no task cancellation) stops the send."""
    gate = threading.Event()
    controller = FakeController(open_gate=gate)
    client = make_client(controller)
    async with anyio.create_task_group() as tg:
        outcome: dict[str, Any] = {}

        async def write() -> None:
            try:
                await client.write_tag("Batch_Count", 5)
            except EIPClientError as exc:
                outcome["meta"] = exc.meta
                outcome["error"] = str(exc)

        tg.start_soon(write)
        await anyio.sleep(0.2)
        client.shutdown()
        gate.set()
    assert controller.all_calls("write") == []
    assert outcome["meta"]["outcome"] == "not_sent" and "disconnected" in outcome["error"]


async def test_no_request_starts_after_shutdown() -> None:
    controller = FakeController()
    client = make_client(controller)
    client.shutdown()
    with pytest.raises(EIPClientError, match="disconnected") as info:
        await client.write_tag("Batch_Count", 5)
    assert info.value.meta["outcome"] == "not_sent"
    assert controller.opens == 0
    await client.ensure_connection()  # does nothing once closing
    assert controller.opens == 0


async def test_bridge_shutdown_between_connect_and_send(monkeypatch: pytest.MonkeyPatch) -> None:
    from ethernetip_mcp import eip_client

    client = EIPClient(EIPClientConfig(port=5025, json_bridge=True, max_retries=3, retry_backoff_base=0.0))
    sent: list[bytes] = []

    class Stream:
        async def __aenter__(self) -> Stream:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def send(self, data: bytes) -> None:
            sent.append(data)

    async def connect(host: str, port: int) -> Stream:
        client.shutdown()  # the client disconnects while the connection is being made
        return Stream()

    monkeypatch.setattr(eip_client.anyio, "connect_tcp", connect)
    with pytest.raises(EIPClientError, match="disconnected") as info:
        await client.write_tag("Batch_Count", 5)
    assert info.value.meta["outcome"] == "not_sent"
    assert sent == []


async def test_bridge_shutdown_during_backoff_stops_retries(closed_port: int) -> None:
    client = EIPClient(EIPClientConfig(port=closed_port, json_bridge=True, max_retries=5, retry_backoff_base=0.2))
    attempts: list[float] = []

    async def write() -> None:
        with pytest.raises(EIPClientError, match="disconnected") as info:
            await client.write_tag("Batch_Count", 5)
        attempts.append(info.value.meta["attempts"])

    async with anyio.create_task_group() as tg:
        tg.start_soon(write)
        await anyio.sleep(0.1)  # first attempt refused, now waiting to retry
        client.shutdown()
    assert attempts == [2]  # the retry stopped at the closing check
