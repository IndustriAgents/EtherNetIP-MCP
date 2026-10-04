"""The JSON-bridge client against the real mock PLC (started as a subprocess)."""

from __future__ import annotations

import json
import socket
import time
from typing import Any

import pytest
from conftest import MockPLC

from ethernetip_mcp.eip_client import EIPClient, EIPClientConfig, EIPClientError

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
        with pytest.raises(EIPClientError, match="no reply within 0.3 s"):
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
    assert results[0] == {"tag": "Batch_Count", "value": 3, "data_type": "DINT", "error": None}
    assert results[1]["error"] == "Tag 'Line_Speed' is REAL, not DINT"
    assert meta["attempts"] == 2
    assert (await client.read_tag("Batch_Count"))[0]["value"] == 3
