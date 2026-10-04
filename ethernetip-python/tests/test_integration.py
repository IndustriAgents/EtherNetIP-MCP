"""End to end: the real ``ethernetip-mcp`` process over stdio, driven by the MCP client SDK."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from conftest import MockPLC, server_command, server_env
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ethernetip_mcp import package_version

pytestmark = pytest.mark.integration


@asynccontextmanager
async def mcp_session(env: dict[str, str], errlog: Path) -> AsyncIterator[tuple[ClientSession, Any]]:
    command, *args = server_command()
    params = StdioServerParameters(command=command, args=args, env=server_env(**env))
    with errlog.open("w") as err:
        async with stdio_client(params, errlog=err) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                yield session, init


async def call(session: ClientSession, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await session.call_tool(name, arguments or {})
    assert not result.isError, result.content
    envelope = json.loads(result.content[0].text)
    assert set(envelope) == {"success", "data", "error", "meta"}
    return envelope


def bridge_env(plc: MockPLC, **extra: str) -> dict[str, str]:
    return {
        "ENIP_HOST": plc.host,
        "ENIP_PORT": str(plc.port),
        "ENIP_JSON_BRIDGE": "true",
        "ENIP_MAX_RETRIES": "0",
        **extra,
    }


async def test_full_session_against_the_mock(mock_plc: MockPLC, tmp_path: Path) -> None:
    tag_map = tmp_path / "tags.json"
    tag_map.write_text(
        json.dumps(
            {
                "motor_speed": {
                    "tag": "Program:MainProgram.MotorSpeed",
                    "data_type": "REAL",
                    "scaling": {"raw_min": 0, "raw_max": 1800, "eng_min": 0, "eng_max": 100},
                }
            }
        )
    )
    env = bridge_env(mock_plc, ENIP_WRITES_ENABLED="true", ENIP_SYSTEM_CMDS_ENABLED="true", TAG_MAP_FILE=str(tag_map))
    async with mcp_session(env, tmp_path / "server.err") as (s, init):
        assert init.serverInfo.name == "EtherNet/IP MCP Server"
        assert init.serverInfo.version == package_version()

        tools = (await s.list_tools()).tools
        assert len(tools) == 17 and all(t.description for t in tools)

        status = await call(s, "get_connection_status")
        assert status["data"]["connected"] is False  # nothing exchanged yet

        speed = await call(s, "read_tag", {"tag_name": "Program:MainProgram.MotorSpeed"})
        assert speed["success"] is True
        assert speed["data"] == {"tag": "Program:MainProgram.MotorSpeed", "value": 1450.0, "data_type": "REAL"}

        unknown = await call(s, "read_tag", {"tag_name": "Nope"})
        assert unknown["success"] is False and unknown["error"] == "read_tag(Nope) failed: Unknown tag 'Nope'"

        array = await call(s, "read_array", {"tag_name": "Program:MainProgram.Tank_Levels", "elements": 2})
        assert array["data"]["value"] == [32.4, 31.9] and array["data"]["data_type"] == "REAL[2]"

        invalid = await call(s, "read_array", {"tag_name": "Program:MainProgram.Tank_Levels", "elements": 0})
        assert invalid["success"] is False and invalid["error"].startswith("Invalid arguments for read_array")

        written = await call(s, "write_tag", {"tag_name": "Line_Speed", "value": 20.5})
        assert written["success"] is True
        assert (await call(s, "read_tag", {"tag_name": "Line_Speed"}))["data"]["value"] == 20.5

        wrong_type = await call(s, "write_tag", {"tag_name": "Line_Speed", "value": "fast"})
        assert wrong_type["success"] is False and "expected a number" in wrong_type["error"]

        batch = await call(
            s,
            "write_multiple_tags",
            {"payloads": [{"tag_name": "Batch_Count", "value": 9}, {"tag_name": "Line_Speed", "value": 1.5}]},
        )
        assert batch["success"] is True
        reads = await call(s, "read_multiple_tags", {"tags": ["Batch_Count", "Line_Speed"]})
        assert [r["value"] for r in reads["data"]["results"]] == [9, 1.5]

        array_write = await call(s, "write_array", {"tag_name": "Program:MainProgram.Tank_Levels", "values": [1, 2, 3]})
        assert array_write["success"] is True
        text = await call(s, "write_string", {"tag_name": "Program:MainProgram.Alarm_Message", "value": "HIGH LEVEL"})
        assert text["success"] is True
        read_text = await call(s, "read_string", {"tag_name": "Program:MainProgram.Alarm_Message"})
        assert read_text["data"]["value"] == "HIGH LEVEL"

        tag_list = await call(s, "get_tag_list", {"program": "*"})
        assert tag_list["meta"]["count"] == 7

        info = await call(s, "get_plc_info")
        assert info["success"] is True and info["data"]["product_name"] == "ethernetip-mock-server"

        plc_time = await call(s, "get_plc_time")
        assert set(plc_time["data"]) == {"plc_time", "microseconds"}
        set_time = await call(s, "set_plc_time")
        assert set_time["success"] is True and set_time["data"]["updated"] is True

        aliases = await call(s, "list_tags")
        assert aliases["data"]["count"] == 1
        scaled = await call(s, "write_tag_by_alias", {"alias": "motor_speed", "value": 50})
        assert scaled["data"]["raw_value"] == pytest.approx(900.0)
        by_alias = await call(s, "read_tag_by_alias", {"alias": "motor_speed"})
        assert by_alias["data"]["value"] == pytest.approx(50.0)

        ping = await call(s, "ping")
        assert ping["success"] is True and ping["data"]["reachable"] is True
        status = await call(s, "get_connection_status")
        assert status["data"]["connected"] is True


async def test_default_configuration_is_read_only(mock_plc: MockPLC, tmp_path: Path) -> None:
    async with mcp_session(bridge_env(mock_plc), tmp_path / "server.err") as (s, _):
        refused = await call(s, "write_tag", {"tag_name": "Line_Speed", "value": 99.0})
        clock = await call(s, "set_plc_time")
        value = await call(s, "read_tag", {"tag_name": "Line_Speed"})
    assert refused["success"] is False and "ENIP_WRITES_ENABLED" in refused["error"]
    assert clock["success"] is False and "ENIP_SYSTEM_CMDS_ENABLED" in clock["error"]
    assert value["data"]["value"] == 12.5


async def test_server_starts_when_the_controller_is_unreachable(closed_port: int, tmp_path: Path) -> None:
    env = {"ENIP_HOST": "127.0.0.1", "ENIP_PORT": str(closed_port), "ENIP_MAX_RETRIES": "0", "ENIP_TIMEOUT": "1"}
    start = time.perf_counter()
    async with mcp_session(env, tmp_path / "server.err") as (s, init):
        assert init.serverInfo.name == "EtherNet/IP MCP Server"
        read = await call(s, "read_tag", {"tag_name": "MotorSpeed"})
        ping = await call(s, "ping")
        status = await call(s, "get_connection_status")
    assert time.perf_counter() - start < 30
    assert read["success"] is False and "failed after 1 attempt" in read["error"]
    assert ping["success"] is False
    assert status["data"]["connected"] is False and status["data"]["connection_path"] == f"127.0.0.1:{closed_port}"
    assert "Could not connect" in (tmp_path / "server.err").read_text()


def test_stdout_carries_only_json_rpc(mock_plc: MockPLC) -> None:
    """With debug logging on, stdout still holds nothing but JSON-RPC messages."""
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "read_tag", "arguments": {"tag_name": "Line_Speed"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "read_tag", "arguments": {"tag_name": "Nope"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "read_array", "arguments": {"tag_name": "x", "elements": 0}},
        },
    ]
    process = subprocess.Popen(
        server_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **server_env(**bridge_env(mock_plc), ENIP_DEBUG="true")},
    )
    responses: dict[int, dict[str, Any]] = {}
    try:
        assert process.stdin and process.stdout
        for message in messages:
            process.stdin.write(json.dumps(message).encode() + b"\n")
            process.stdin.flush()
            if "id" not in message:
                continue
            while message["id"] not in responses:
                line = process.stdout.readline()
                assert line, "server closed stdout early"
                parsed = json.loads(line)  # any non-JSON output would fail here
                assert parsed["jsonrpc"] == "2.0"
                if "id" in parsed:
                    responses[parsed["id"]] = parsed
        process.stdin.close()
        rest = process.stdout.read()
        process.wait(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    for line in rest.splitlines():
        assert json.loads(line)["jsonrpc"] == "2.0"
    stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
    assert responses[3]["result"]["structuredContent"]["success"] is True
    assert responses[4]["result"]["structuredContent"]["success"] is False
    assert responses[5]["result"]["structuredContent"]["success"] is False
    assert "DEBUG" in stderr or "Processing request" in stderr  # the logs went to stderr
