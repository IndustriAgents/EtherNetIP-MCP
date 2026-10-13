"""The cancel race: a cancel in the same loop step a wait completes must still stop the send.

The MCP SDK's RequestResponder.cancel() sets the request scope's cancel_called
synchronously and replies "Request cancelled", but anyio does not interrupt a
task (or a worker thread) whose wait has just completed: it runs one more
step, and that step could send. So the pre-send checks read cancel_called
themselves. These tests cancel the request in exactly that window, many
times, through the real MCP request machinery, and assert nothing is sent.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import anyio
import pytest
from fake_pycomm3 import FakeController
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from ethernetip_mcp.eip_client import CALL_CANCELLED, EIPClient, EIPClientConfig
from ethernetip_mcp.server import EtherNetIPMCPServer, request_cancel_scope
from ethernetip_mcp.tools import ToolConfig

ROUNDS = 40


def current_responder(server: EtherNetIPMCPServer) -> Any:
    """The SDK's RequestResponder of the request being handled (mcp 1.x internals)."""
    context = server.mcp._mcp_server.request_context
    return context.session._in_flight[context.request_id]


def cancel_now(responder: Any, loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Run the SDK's own RequestResponder.cancel(), its synchronous part right now.

    cancel() sets the scope's cancel_called and marks the request completed,
    then suspends at anyio's checkpoint (a bare sleep(0) yield) before sending
    "Request cancelled". Stepping the real coroutine once lands the cancel in
    the current loop step; a task resumes it to send the reply.
    """
    coro = responder.cancel()
    try:
        suspended_at = coro.send(None)
    except StopIteration:
        return
    assert suspended_at is None, "RequestResponder.cancel() changed: it no longer suspends at a checkpoint"
    asyncio.get_running_loop().create_task(coro)


async def call_expecting_cancel(session: Any, name: str, arguments: dict[str, Any]) -> None:
    with anyio.fail_after(10):
        try:
            await session.call_tool(name, arguments)
        except McpError as exc:
            assert "cancelled" in str(exc).lower()


def test_mcp_internals_still_expose_the_request_cancel_scope() -> None:
    """Fails if mcp moves the private pieces the cancel check relies on."""
    controller = FakeController()
    client = EIPClient(EIPClientConfig(max_retries=0), driver_factory=controller.factory)
    server = EtherNetIPMCPServer(client=client, tool_config=ToolConfig())
    seen: dict[str, Any] = {}
    original = client.read_tag

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen["scope"] = request_cancel_scope(server.mcp._mcp_server)
        seen["responder"] = current_responder(server)
        seen["flag_during_call"] = CALL_CANCELLED.get()()
        seen["flag"] = CALL_CANCELLED.get()
        return await original(*args, **kwargs)

    client.read_tag = spy  # type: ignore[method-assign]

    async def run() -> None:
        async with create_connected_server_and_client_session(server.mcp) as session:
            result = await session.call_tool("read_tag", {"tag_name": "MotorSpeed"})
            assert result.structuredContent["success"] is True

    anyio.run(run)
    assert isinstance(seen["scope"], anyio.CancelScope)
    assert seen["responder"]._cancel_scope is seen["scope"]
    assert hasattr(type(seen["responder"]), "cancel")
    assert seen["flag_during_call"] is False
    # The server wired the call's flag to this request (not the no-request default).
    from ethernetip_mcp.eip_client import _never

    assert seen["flag"] is not _never


async def test_cip_cancel_as_the_connect_completes_never_writes() -> None:
    sent_after_cancel = 0
    from pycomm3 import CommError

    for _ in range(ROUNDS):
        # Unreachable at startup, so the write itself has to open the connection.
        controller = FakeController(open_errors=[CommError("down at startup")])
        client = EIPClient(EIPClientConfig(max_retries=0), driver_factory=controller.factory)
        server = EtherNetIPMCPServer(client=client, tool_config=ToolConfig(writes_enabled=True))
        loop = asyncio.get_running_loop()
        holder: dict[str, Any] = {}
        original = client.write_tag

        async def write(
            *args: Any, _original: Any = original, _holder: dict = holder, _server: Any = server, **kwargs: Any
        ) -> Any:
            _holder["responder"] = current_responder(_server)
            return await _original(*args, **kwargs)

        client.write_tag = write  # type: ignore[method-assign]
        # In the worker thread, the moment the connection is up: cancel the request.
        controller.after_open = (
            lambda _h=holder, _loop=loop: anyio.from_thread.run_sync(cancel_now, _h["responder"], _loop)
            if "responder" in _h
            else None
        )
        async with create_connected_server_and_client_session(server.mcp) as session:
            await call_expecting_cancel(session, "write_tag", {"tag_name": "Batch_Count", "value": 9})
            await anyio.sleep(0.05)  # let an abandoned worker finish what it was doing
        assert controller.opens == 2  # startup failed; the write's own connect raced the cancel
        sent_after_cancel += len(controller.all_calls("write"))
    assert sent_after_cancel == 0


async def test_cip_cancel_as_the_session_is_released_never_writes() -> None:
    sent_after_cancel = 0
    for _ in range(ROUNDS):
        controller = FakeController()
        client = EIPClient(EIPClientConfig(max_retries=0), driver_factory=controller.factory)
        server = EtherNetIPMCPServer(client=client, tool_config=ToolConfig(writes_enabled=True))
        loop = asyncio.get_running_loop()
        holder: dict[str, Any] = {}
        write_queued = threading.Event()
        original = client.write_tag

        async def write(
            *args: Any,
            _original: Any = original,
            _holder: dict = holder,
            _queued: Any = write_queued,
            _server: Any = server,
            **kwargs: Any,
        ) -> Any:
            _holder["responder"] = current_responder(_server)
            _queued.set()
            return await _original(*args, **kwargs)

        client.write_tag = write  # type: ignore[method-assign]

        def after_read(_h: dict = holder, _queued: Any = write_queued, _loop: Any = loop) -> None:
            # The read holds the session until the write is queued behind it, then
            # cancels the write in the same step that it releases the session.
            if _queued.wait(5):
                anyio.from_thread.run_sync(cancel_now, _h["responder"], _loop)

        controller.after_read = after_read
        async with create_connected_server_and_client_session(server.mcp) as session:
            async with anyio.create_task_group() as tg:
                tg.start_soon(session.call_tool, "read_tag", {"tag_name": "MotorSpeed"})
                await anyio.sleep(0.01)
                await call_expecting_cancel(session, "write_tag", {"tag_name": "Batch_Count", "value": 9})
            await anyio.sleep(0.05)
        sent_after_cancel += len(controller.all_calls("write"))
    assert sent_after_cancel == 0


async def test_bridge_cancel_as_the_connect_completes_never_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    from ethernetip_mcp import eip_client

    sent: list[bytes] = []
    client = EIPClient(EIPClientConfig(port=5025, json_bridge=True, max_retries=0))
    server = EtherNetIPMCPServer(client=client, tool_config=ToolConfig(writes_enabled=True))

    class Stream:
        async def __aenter__(self) -> Stream:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def send(self, data: bytes) -> None:
            sent.append(data)

        async def receive(self, max_bytes: int = 65536) -> bytes:
            await anyio.sleep(10)
            return b""

    async def connect(host: str, port: int) -> Stream:
        # The connection completes and, in the same step, the client cancels.
        cancel_now(current_responder(server), asyncio.get_running_loop())
        return Stream()

    monkeypatch.setattr(eip_client.anyio, "connect_tcp", connect)
    async with create_connected_server_and_client_session(server.mcp) as session:
        for _ in range(ROUNDS):
            await call_expecting_cancel(session, "write_tag", {"tag_name": "Batch_Count", "value": 9})
    assert sent == []


async def test_server_still_answers_ping_after_a_same_step_cancel() -> None:
    """The SDK has already answered "Request cancelled"; the server must not answer again."""
    controller = FakeController()
    client = EIPClient(EIPClientConfig(max_retries=0), driver_factory=controller.factory)
    server = EtherNetIPMCPServer(client=client, tool_config=ToolConfig(writes_enabled=True))
    loop = asyncio.get_running_loop()

    async def write_finishing_as_cancelled(*args: Any, **kwargs: Any) -> Any:
        cancel_now(current_responder(server), loop)  # cancelled in the step the result is ready
        return {"tag": "Batch_Count", "value": 1, "data_type": "DINT"}, {"outcome": "written", "request_sent": True}

    client.write_tag = write_finishing_as_cancelled  # type: ignore[method-assign]
    async with create_connected_server_and_client_session(server.mcp, raise_exceptions=True) as session:
        for _ in range(5):
            await call_expecting_cancel(session, "write_tag", {"tag_name": "Batch_Count", "value": 1})
        ping = await session.call_tool("ping", {})  # the server is still up and answering
        assert ping.structuredContent["success"] is True
