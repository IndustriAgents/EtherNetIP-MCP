"""The JSON-bridge client against the real mock PLC (started as a subprocess)."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from conftest import MockPLC

from ethernetip_mcp.eip_client import EIPClient, EIPClientConfig, EIPClientError, OutcomeUnknownError
from ethernetip_mcp.server import EtherNetIPMCPServer
from ethernetip_mcp.tools import ToolConfig

pytestmark = pytest.mark.integration


def bridge(port: int, **config: Any) -> EIPClient:
    settings = {"port": port, "json_bridge": True, "max_retries": 0, "retry_backoff_base": 0.0, **config}
    return EIPClient(EIPClientConfig(**settings))


def raw_request(plc: MockPLC, payload: Any) -> dict[str, Any]:
    with socket.create_connection((plc.host, plc.port), timeout=5) as sock:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode() + b"\n"
        sock.sendall(data)
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            reply += chunk
    return json.loads(reply)


async def test_unknown_tag_error_is_not_double_quoted(mock_plc: MockPLC) -> None:
    with pytest.raises(EIPClientError) as info:
        await bridge(mock_plc.port).read_tag("Nope")
    assert str(info.value) == "read_tag(Nope) failed: Unknown tag 'Nope'"
    assert raw_request(mock_plc, {"op": "read", "tag": "Nope"}) == {"success": False, "error": "Unknown tag 'Nope'"}


async def test_reads_follow_pycomm3_element_rules(mock_plc: MockPLC) -> None:
    client = bridge(mock_plc.port)
    tag = "Program:MainProgram.Tank_Levels"
    first, _ = await client.read_tag(tag)
    two, _ = await client.read_tag(tag, 2)
    braces, _ = await client.read_tag(tag + "{3}")
    assert first == {"tag": tag, "value": 32.4, "data_type": "REAL"}  # one element, like a controller
    assert two == {"tag": tag, "value": [32.4, 31.9], "data_type": "REAL[2]", "elements": 2}
    assert braces["value"] == [32.4, 31.9, 33.1]
    with pytest.raises(EIPClientError, match="count 4 is out of range"):
        await client.read_tag(tag, 4)
    with pytest.raises(EIPClientError, match="is not an array"):
        await client.read_tag("Line_Speed", 2)


async def test_writes_are_type_checked(mock_plc: MockPLC) -> None:
    client = bridge(mock_plc.port)
    result, _ = await client.write_tag("Batch_Count", 7)
    assert result == {"tag": "Batch_Count", "value": 7, "data_type": "DINT"}
    assert (await client.read_tag("Batch_Count"))[0]["value"] == 7
    with pytest.raises(EIPClientError, match="Batch_Count' is DINT; expected an integer"):
        await client.write_tag("Batch_Count", 1.5)
    with pytest.raises(EIPClientError, match="is REAL; expected a number"):
        await client.write_tag("Line_Speed", "fast")
    with pytest.raises(EIPClientError, match="is REAL, not DINT"):
        await client.write_tag("Line_Speed", 3, "DINT")
    flag = "Program:MainProgram.Conveyor_Status.Running"
    assert (await client.write_tag(flag, 0))[0]["value"] is False  # 1/0 accepted, as pycomm3 does
    assert (await client.write_tag(flag, 1))[0]["value"] is True
    with pytest.raises(EIPClientError, match="expected true/false or 1/0"):
        await client.write_tag(flag, 2)
    with pytest.raises(EIPClientError, match="at most 82 characters"):
        await client.write_tag("Program:MainProgram.Alarm_Message", "x" * 83)
    assert (await client.read_tag("Line_Speed"))[0]["value"] == 12.5  # nothing changed


async def test_list_writes_update_leading_elements(mock_plc: MockPLC) -> None:
    client = bridge(mock_plc.port)
    tag = "Program:MainProgram.Tank_Levels"
    result, _ = await client.write_tag(tag, [1.0, 2.0])
    assert result["value"] == [1.0, 2.0] and result["data_type"] == "REAL[2]"
    assert (await client.read_tag(tag, 3))[0]["value"] == [1.0, 2.0, 33.1]
    with pytest.raises(EIPClientError, match="has 3 elements; got 4 values"):
        await client.write_tag(tag, [1.0, 2.0, 3.0, 4.0])


async def test_tag_list_scopes(mock_plc: MockPLC) -> None:
    client = bridge(mock_plc.port)
    controller_tags, meta = await client.get_tag_list()
    levels = [t for t in (await client.get_tag_list("MainProgram"))[0] if t["tag"].endswith("Tank_Levels")][0]
    # Same keys and conventions as a real controller's list (see _tag_definition).
    assert levels == {
        "tag": "Program:MainProgram.Tank_Levels",
        "data_type": "REAL",
        "dimensions": [3],
        "tag_type": "atomic",
        "alias": False,
        "external_access": "Read/Write",
        "description": None,
    }
    every, _ = await client.get_tag_list("*")
    program, _ = await client.get_tag_list("MainProgram")
    assert [t["tag"] for t in controller_tags] == ["Line_Speed", "Batch_Count"] and meta["count"] == 2
    assert len(every) == 7
    assert all(t["tag"].startswith("Program:MainProgram.") for t in program) and len(program) == 5
    with pytest.raises(EIPClientError, match="Unknown program 'Nope'"):
        await client.get_tag_list("Nope")


async def test_identity_and_clock_come_from_the_mock(mock_plc: MockPLC) -> None:
    client = bridge(mock_plc.port)
    info, _ = await client.get_controller_info()
    assert info["product_name"] == "ethernetip-mock-server" and info["firmware"] == "0.1"

    y2k = 946_684_800_000_000
    raw_request(mock_plc, {"op": "set_time", "microseconds": y2k})
    clock, _ = await client.get_plc_time()
    assert clock["plc_time"].startswith("2000-01-01T00:00")

    before = int(time.time() * 1_000_000)
    await client.set_plc_time()
    clock, _ = await client.get_plc_time()
    assert before <= clock["microseconds"] <= int(time.time() * 1_000_000) + 1_000_000


async def test_status_reflects_last_exchange(mock_plc: MockPLC, closed_port: int) -> None:
    client = bridge(mock_plc.port)
    assert client.connection_status()["connected"] is False
    await client.ping()
    status = client.connection_status()
    assert status["connected"] is True and status["last_contact"] and status["backend"] == "json_bridge"

    down = bridge(closed_port)
    with pytest.raises(EIPClientError, match="cannot reach the JSON bridge"):
        await down.ping()
    assert down.connection_status()["connected"] is False
    assert "cannot reach" in down.connection_status()["last_error"]


async def test_bridge_retries_are_bounded(closed_port: int) -> None:
    client = bridge(closed_port, max_retries=2, retry_backoff_base=0.05)
    start = time.perf_counter()
    with pytest.raises(EIPClientError, match="failed after 3 attempt") as info:
        await client.read_tag("Line_Speed")
    assert info.value.meta["attempts"] == 3
    assert time.perf_counter() - start < 2.0


async def test_bridge_honours_enip_timeout() -> None:
    hole = socket.socket()
    hole.bind(("127.0.0.1", 0))
    hole.listen(4)
    try:
        client = bridge(hole.getsockname()[1], timeout=0.3)
        start = time.perf_counter()
        with pytest.raises(EIPClientError, match="no complete reply within 0.3 s"):
            await client.read_tag("Line_Speed")
        assert time.perf_counter() - start < 2.0
    finally:
        hole.close()


def test_mock_rejects_malformed_requests(mock_plc: MockPLC) -> None:
    assert raw_request(mock_plc, b"not json\n") == {"success": False, "error": "Invalid JSON"}
    assert raw_request(mock_plc, [1, 2]) == {"success": False, "error": "Request must be a JSON object"}
    assert raw_request(mock_plc, {"op": "explode"}) == {"success": False, "error": "Unknown op 'explode'"}
    assert raw_request(mock_plc, {"op": "read"}) == {"success": False, "error": "'tag' must be a non-empty string"}
    reply = raw_request(mock_plc, {"op": "read", "tag": "Line_Speed", "count": True})
    assert reply["success"] is False and "'count'" in reply["error"]
    reply = raw_request(mock_plc, {"op": "set_time", "microseconds": "soon"})
    assert reply["success"] is False and "'microseconds'" in reply["error"]


async def test_write_multiple_tags_reports_each_entry(mock_plc: MockPLC) -> None:
    client = bridge(mock_plc.port)
    results, meta = await client.write_multiple_tags([("Batch_Count", 3, "DINT"), ("Line_Speed", 2, "DINT")])
    assert results[0] == {
        "tag": "Batch_Count",
        "value": 3,
        "data_type": "DINT",
        "error": None,
        "outcome": "written",
        "request_sent": True,
    }
    assert results[1]["error"] == "Tag 'Line_Speed' is REAL, not DINT"
    assert (results[1]["outcome"], results[1]["request_sent"]) == ("rejected", True)
    assert meta["attempts"] == 2
    assert (await client.read_tag("Batch_Count"))[0]["value"] == 3


# -- writes are never repeated once they may have reached the device ---------

Handler = Callable[[dict[str, Any], asyncio.StreamWriter], Awaitable[None]]


@asynccontextmanager
async def fake_bridge(handler: Handler, host: str = "127.0.0.1") -> AsyncIterator[tuple[int, list[dict[str, Any]]]]:
    """A one-request-per-connection JSON bridge whose replies the test controls."""
    received: list[dict[str, Any]] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        if line:
            request = json.loads(line)
            received.append(request)
            try:
                await handler(request, writer)
            except (ConnectionError, OSError):
                pass
        writer.close()

    server = await asyncio.start_server(serve, host, 0)
    try:
        yield server.sockets[0].getsockname()[1], received
    finally:
        server.close()
        await server.wait_closed()


def reply(writer: asyncio.StreamWriter, request: dict[str, Any]) -> None:
    data = {"tag": request.get("tag"), "value": request.get("value"), "data_type": "DINT"}
    writer.write(json.dumps({"success": True, "data": data}).encode() + b"\n")


async def test_bridge_write_with_late_reply_is_sent_exactly_once() -> None:
    async def late(request: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(0.5)  # applied, but answered after the client's timeout
        reply(writer, request)

    async with fake_bridge(late) as (port, received):
        client = bridge(port, timeout=0.2, max_retries=3)
        with pytest.raises(OutcomeUnknownError, match="may have been applied") as info:
            await client.write_tag("StartCmd", 1)
        await asyncio.sleep(0.6)
    assert len(received) == 1
    assert info.value.meta["outcome"] == "unknown" and info.value.meta["attempts"] == 1


async def test_bridge_set_plc_time_dropped_reply_is_sent_exactly_once() -> None:
    async def drop(request: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        return None  # connection closes without a reply

    async with fake_bridge(drop) as (port, received):
        with pytest.raises(OutcomeUnknownError):
            await bridge(port, max_retries=3).set_plc_time()
    assert [r["op"] for r in received] == ["set_time"]


async def test_bridge_write_to_unreachable_device_is_retried_and_not_sent(closed_port: int) -> None:
    with pytest.raises(EIPClientError) as info:
        await bridge(closed_port, max_retries=2).write_tag("Batch_Count", 1)
    assert not isinstance(info.value, OutcomeUnknownError)
    assert info.value.meta == {**info.value.meta, "outcome": "not_sent", "attempts": 3}


async def test_bridge_reads_are_still_retried_after_a_dropped_reply() -> None:
    async def flaky(request: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        if flaky.calls == 0:
            flaky.calls += 1
            return None
        writer.write(
            json.dumps({"success": True, "data": {"tag": "X", "value": 3, "data_type": "DINT"}}).encode() + b"\n"
        )

    flaky.calls = 0
    async with fake_bridge(flaky) as (port, received):
        result, meta = await bridge(port, max_retries=1).read_tag("X")
    assert result["value"] == 3 and meta["attempts"] == 2 and len(received) == 2


async def test_batch_reports_applied_entries_when_the_connection_drops() -> None:
    async def first_only(request: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        if request["tag"] == "A":
            reply(writer, request)  # B: applied or not, the connection drops without a reply

    async with fake_bridge(first_only) as (port, received):
        client = EIPClient(EIPClientConfig(port=port, json_bridge=True, timeout=1, max_retries=2))
        server = EtherNetIPMCPServer(client=client, tool_config=ToolConfig(writes_enabled=True))
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(server.mcp) as session:
            result = await session.call_tool(
                "write_multiple_tags",
                {
                    "payloads": [
                        {"tag_name": "A", "value": 1},
                        {"tag_name": "B", "value": 2},
                        {"tag_name": "C", "value": 3},
                    ]
                },
            )
    envelope = result.structuredContent
    assert [r["tag"] for r in received] == ["A", "B"]  # B sent once, C never sent
    assert envelope["success"] is False
    assert "2 of 3 writes were not confirmed" in envelope["error"]
    outcomes = [(r["tag"], r["outcome"]) for r in envelope["data"]["results"]]
    assert outcomes == [("A", "written"), ("B", "unknown"), ("C", "not_sent")]
    assert "may have been applied" in envelope["data"]["results"][1]["error"]
    assert envelope["data"]["results"][2]["error"].startswith("not sent")
    assert [r["request_sent"] for r in envelope["data"]["results"]] == [True, True, False]


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as sock:
            sock.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _ipv6_loopback(), reason="no IPv6 loopback")
async def test_bridge_over_ipv6() -> None:
    async def answer(request: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        writer.write(
            json.dumps({"success": True, "data": {"tag": "X", "value": 1, "data_type": "DINT"}}).encode() + b"\n"
        )

    async with fake_bridge(answer, host="::1") as (port, _):
        config = EIPClientConfig.from_env({"ENIP_JSON_BRIDGE": "true", "ENIP_HOST": f"[::1]:{port}"})
        result, _ = await EIPClient(config).read_tag("X")
    assert result["value"] == 1


# -- outcome/request_sent and the "sent" boundary on the bridge ---------------


async def test_bridge_write_outcomes(mock_plc: MockPLC, closed_port: int) -> None:
    client = bridge(mock_plc.port)
    _, meta = await client.write_tag("Batch_Count", 5)
    assert (meta["outcome"], meta["request_sent"]) == ("written", True)
    with pytest.raises(EIPClientError) as info:
        await client.write_tag("Batch_Count", "five")
    assert (info.value.meta["outcome"], info.value.meta["request_sent"]) == ("rejected", True)
    _, meta = await client.set_plc_time()
    assert (meta["outcome"], meta["request_sent"]) == ("written", True)
    with pytest.raises(EIPClientError) as info:
        await bridge(closed_port).set_plc_time()
    assert (info.value.meta["outcome"], info.value.meta["request_sent"]) == ("not_sent", False)


class _BrokenSendStream:
    """A connected stream whose send() fails or never finishes."""

    def __init__(self, mode: str) -> None:
        self.mode = mode

    async def __aenter__(self) -> _BrokenSendStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def send(self, data: bytes) -> None:
        sends.append(data)
        if self.mode == "hang":
            await asyncio.sleep(30)
        raise OSError("connection reset while sending")

    async def receive(self, max_bytes: int = 65536) -> bytes:
        raise AssertionError("receive must not be reached")


sends: list[bytes] = []


@pytest.mark.parametrize("mode", ["raise", "hang"])
async def test_failure_during_send_is_unknown_and_never_retried(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Once send() has started, part of the request may be on the wire."""
    from ethernetip_mcp import eip_client

    async def connect(host: str, port: int) -> _BrokenSendStream:
        return _BrokenSendStream(mode)

    sends.clear()
    monkeypatch.setattr(eip_client.anyio, "connect_tcp", connect)
    client = bridge(5025, max_retries=3, timeout=0.3)
    with pytest.raises(OutcomeUnknownError, match="may have been applied") as info:
        await client.write_tag("Batch_Count", 1)
    assert info.value.meta["attempts"] == 1 and info.value.meta["request_sent"] is True
    assert len(sends) == 1


# -- garbage from the peer is bounded and classified (rule 12) ----------------


async def test_garbage_reply_fails_a_read_cleanly() -> None:
    async def garbage(request: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        writer.write(b"\x00\xffnot json at all\n")

    async with fake_bridge(garbage) as (port, received):
        with pytest.raises(EIPClientError, match="undecodable reply") as info:
            await bridge(port, max_retries=3).read_tag("X")
    assert not isinstance(info.value, OutcomeUnknownError)
    assert len(received) == 1  # a garbled reply is not a connection failure: no retry


async def test_garbage_reply_to_a_write_is_unknown() -> None:
    async def garbage(request: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        writer.write(b"\x00\xffnot json at all\n")

    async with fake_bridge(garbage) as (port, received):
        with pytest.raises(OutcomeUnknownError, match="may have been applied") as info:
            await bridge(port, max_retries=3).write_tag("Batch_Count", 1)
    assert info.value.meta["request_sent"] is True and len(received) == 1


async def test_trickling_garbage_is_bounded_by_the_deadline() -> None:
    async def trickle(request: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        for _ in range(1000):  # bytes, never a newline
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(0.05)

    async with fake_bridge(trickle) as (port, _):
        client = bridge(port, max_retries=10, retry_backoff_base=0.0, timeout=0.4, deadline=1.0)
        start = time.perf_counter()
        with pytest.raises(EIPClientError, match="no complete reply"):
            await client.read_tag("X")
        assert time.perf_counter() - start < 2.0  # not 11 x 0.4 s
