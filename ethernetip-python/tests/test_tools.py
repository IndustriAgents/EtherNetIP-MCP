"""Tool behaviour over an in-memory MCP session (CIP backend on the fake driver)."""

from __future__ import annotations

import importlib.metadata
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fake_pycomm3 import FakeController
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from ethernetip_mcp import package_version
from ethernetip_mcp.eip_client import EIPClient, EIPClientConfig
from ethernetip_mcp.server import EtherNetIPMCPServer
from ethernetip_mcp.tools import ToolConfig

EXPECTED_TOOLS = {
    "read_tag",
    "write_tag",
    "read_array",
    "write_array",
    "read_string",
    "write_string",
    "get_tag_list",
    "read_multiple_tags",
    "write_multiple_tags",
    "list_tags",
    "read_tag_by_alias",
    "write_tag_by_alias",
    "ping",
    "get_connection_status",
    "get_plc_info",
    "get_plc_time",
    "set_plc_time",
}

TAG_MAP = {
    "motor_speed": {
        "tag": "MotorSpeed",
        "data_type": "REAL",
        "description": "Motor speed",
        "scaling": {"raw_min": 0, "raw_max": 1800, "eng_min": 0, "eng_max": 100},
    },
    "batches": {
        "tag": "Batch_Count",
        "data_type": "DINT",
        "scaling": {"raw_min": 0, "raw_max": 10, "eng_min": 0, "eng_max": 1},
    },
    "message": {"tag": "Program:MainProgram.Alarm_Message"},
    "count_unscaled": {"tag": "Batch_Count", "data_type": "DINT"},
    "flat": {"tag": "MotorSpeed", "scaling": {"raw_min": 5, "raw_max": 5}},
    "no_tag": {"description": "missing tag"},
}


@asynccontextmanager
async def session(
    controller: FakeController | None = None,
    *,
    writes: bool = False,
    system: bool = False,
    tag_map: Path | None = None,
    tool_config: ToolConfig | None = None,
) -> AsyncIterator[ClientSession]:
    controller = controller or FakeController()
    client = EIPClient(EIPClientConfig(max_retries=0, retry_backoff_base=0.0), driver_factory=controller.factory)
    server = EtherNetIPMCPServer(
        client=client,
        tool_config=tool_config or ToolConfig(writes_enabled=writes, system_cmds_enabled=system, tag_map_path=tag_map),
    )
    async with create_connected_server_and_client_session(server.mcp) as client_session:
        yield client_session


async def call(client_session: ClientSession, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await client_session.call_tool(name, arguments or {})
    assert not result.isError, result.content
    envelope = result.structuredContent
    assert set(envelope) == {"success", "data", "error", "meta"}
    assert json.loads(result.content[0].text) == envelope
    return envelope


@pytest.fixture
def tag_map_file(tmp_path: Path) -> Path:
    path = tmp_path / "tags.json"
    path.write_text(json.dumps(TAG_MAP), encoding="utf-8")
    return path


async def test_every_tool_is_listed_with_a_description() -> None:
    async with session() as s:
        tools = (await s.list_tools()).tools
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description and len(tool.description) > 20, tool.name


async def test_server_reports_its_own_version() -> None:
    controller = FakeController()
    client = EIPClient(EIPClientConfig(max_retries=0), driver_factory=controller.factory)
    server = EtherNetIPMCPServer(client=client, tool_config=ToolConfig())
    options = server.mcp._mcp_server.create_initialization_options()
    assert options.server_name == "EtherNet/IP MCP Server"
    assert options.server_version == package_version() == importlib.metadata.version("ethernetip-mcp")


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("write_tag", {"tag_name": "MotorSpeed", "value": 1.0}),
        ("write_array", {"tag_name": "Tank_Levels", "values": [1.0]}),
        ("write_string", {"tag_name": "Program:MainProgram.Alarm_Message", "value": "x"}),
        ("write_multiple_tags", {"payloads": [{"tag_name": "MotorSpeed", "value": 1.0}]}),
        ("write_tag_by_alias", {"alias": "motor_speed", "value": 50}),
    ],
)
async def test_writes_are_refused_by_default(tool: str, arguments: dict[str, Any], tag_map_file: Path) -> None:
    controller = FakeController()
    # Only TAG_MAP_FILE is set: the write gate comes from ToolConfig's own default.
    default_config = ToolConfig.from_env({"TAG_MAP_FILE": str(tag_map_file)})
    async with session(controller, tool_config=default_config) as s:
        envelope = await call(s, tool, arguments)
    assert envelope["success"] is False
    assert "ENIP_WRITES_ENABLED=true" in envelope["error"]
    assert controller.all_calls("write") == []


@pytest.mark.parametrize(
    "writes, system, missing",
    [
        (False, False, "ENIP_WRITES_ENABLED, ENIP_SYSTEM_CMDS_ENABLED"),
        (True, False, "ENIP_SYSTEM_CMDS_ENABLED"),
        (False, True, "ENIP_WRITES_ENABLED"),  # system commands alone are not enough
    ],
)
async def test_set_plc_time_needs_both_gates(writes: bool, system: bool, missing: str) -> None:
    controller = FakeController()
    async with session(controller, writes=writes, system=system) as s:
        envelope = await call(s, "set_plc_time")
    assert envelope["success"] is False
    assert "needs ENIP_WRITES_ENABLED=true and ENIP_SYSTEM_CMDS_ENABLED=true" in envelope["error"]
    assert envelope["error"].endswith(f"(not set: {missing})")
    assert controller.set_time_calls == []


@pytest.mark.parametrize(
    "tool, arguments, fragment",
    [
        ("read_tag", {}, "tag_name: Field required"),
        ("read_tag", {"tag_name": ""}, "tag_name"),
        ("read_tag", {"tag_name": "MotorSpeed", "count": 0}, "count"),
        ("read_array", {"tag_name": "Tank_Levels", "elements": -1}, "elements"),
        ("read_array", {"tag_name": "Tank_Levels", "elements": "many"}, "elements"),
        ("write_array", {"tag_name": "Tank_Levels", "values": []}, "values"),
        ("write_tag", {"tag_name": "MotorSpeed"}, "value: Field required"),
        ("read_multiple_tags", {"tags": []}, "tags"),
        ("read_multiple_tags", {"tags": "MotorSpeed"}, "tags"),
        ("write_multiple_tags", {"payloads": ["MotorSpeed"]}, "payloads"),
        ("get_tag_list", {"program": ""}, "program"),
        ("read_array", {"tag_name": "Tank_Levels", "elements": True}, "elements"),
        ("read_array", {"tag_name": "Tank_Levels", "elements": "2"}, "elements"),
        ("read_array", {"tag_name": "Tank_Levels", "elements": 2.0}, "elements"),
        ("read_tag", {"tag_name": "Tank_Levels", "count": True}, "count"),
        ("read_tag_by_alias", {"alias": ""}, "alias"),
    ],
)
async def test_bad_arguments_come_back_as_envelopes(tool: str, arguments: dict[str, Any], fragment: str) -> None:
    async with session(writes=True) as s:
        envelope = await call(s, tool, arguments)
    assert envelope["success"] is False
    assert envelope["error"].startswith(f"Invalid arguments for {tool}:")
    assert fragment in envelope["error"]


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({"payloads": [{"value": 1}]}, "needs a non-empty 'tag_name'"),
        ({"payloads": [{"tag_name": "MotorSpeed"}]}, "needs a 'value'"),
        ({"payloads": [{"tag_name": "MotorSpeed", "value": 1}, {"tag": "MotorSpeed", "value": 2}]}, "more than once"),
    ],
)
async def test_write_multiple_tags_validates_payloads(arguments: dict[str, Any], fragment: str) -> None:
    controller = FakeController()
    async with session(controller, writes=True) as s:
        envelope = await call(s, "write_multiple_tags", arguments)
    assert envelope["success"] is False and fragment in envelope["error"]
    assert controller.all_calls("write") == []


async def test_value_validation_errors_are_envelopes() -> None:
    async with session(writes=True) as s:
        null_value = await call(s, "write_tag", {"tag_name": "MotorSpeed", "value": None})
        bad_count = await call(s, "read_tag", {"tag_name": "Tank_Levels{3}", "count": 2})
    assert null_value["success"] is False and "no value given" in null_value["error"]
    assert bad_count["success"] is False and "asks for 3 elements" in bad_count["error"]


async def test_unexpected_exceptions_become_envelopes() -> None:
    class Boom(FakeController):
        def factory(self, path: str):  # noqa: ANN201
            raise ZeroDivisionError("kaboom")

    async with session(Boom()) as s:
        envelope = await call(s, "read_tag", {"tag_name": "MotorSpeed"})
    assert envelope["success"] is False and "kaboom" in envelope["error"]


async def test_read_and_write_round_trip() -> None:
    async with session(writes=True) as s:
        written = await call(s, "write_tag", {"tag_name": "MotorSpeed", "value": 1200.5})
        read = await call(s, "read_tag", {"tag_name": "MotorSpeed"})
        array = await call(s, "read_array", {"tag_name": "Tank_Levels", "elements": 3})
    assert written["success"] is True
    assert written["data"] == {"tag": "MotorSpeed", "value": 1200.5, "data_type": "REAL"}
    assert read["data"] == {"tag": "MotorSpeed", "value": 1200.5, "data_type": "REAL"}
    assert read["meta"]["backend"] == "cip"
    assert array["data"]["value"] == [32.4, 31.9, 33.1]


async def test_failed_read_is_not_success() -> None:
    async with session() as s:
        envelope = await call(s, "read_tag", {"tag_name": "Nope"})
    assert envelope == {
        "success": False,
        "data": None,
        "error": "read_tag(Nope) failed: Tag doesn't exist - Nope",
        "meta": {
            "backend": "cip",
            "attempts": 1,
            "duration_ms": envelope["meta"]["duration_ms"],
            "tool": "read_tag",
            "tag_name": "Nope",
        },
    }


async def test_read_string() -> None:
    async with session() as s:
        text = await call(s, "read_string", {"tag_name": "Program:MainProgram.Alarm_Message"})
        number = await call(s, "read_string", {"tag_name": "MotorSpeed"})
    assert text["success"] is True and text["data"]["value"] == "OK"
    assert number["success"] is False and "is not a string (data_type REAL)" in number["error"]


async def test_batch_partial_failures_are_failures() -> None:
    async with session(writes=True) as s:
        reads = await call(s, "read_multiple_tags", {"tags": ["MotorSpeed", "Nope"]})
        writes = await call(
            s,
            "write_multiple_tags",
            {"payloads": [{"tag_name": "MotorSpeed", "value": 2.0}, {"tag": "Nope", "value": 1}]},
        )
    assert reads["success"] is False and reads["error"].startswith("1 of 2 reads failed: Nope")
    assert reads["data"]["results"][0]["value"] == 1450.0
    assert writes["success"] is False
    # pycomm3 knows the controller's tags, so an unknown tag is refused before sending.
    assert writes["error"].startswith("1 of 2 writes were not confirmed: Nope (not_sent)")
    assert [(r["outcome"], r["request_sent"]) for r in writes["data"]["results"]] == [
        ("written", True),
        ("not_sent", False),
    ]
    assert writes["data"]["results"][0]["error"] is None


async def test_plc_info_time_and_set_time() -> None:
    controller = FakeController()
    async with session(controller, writes=True, system=True) as s:
        info = await call(s, "get_plc_info")
        plc_time = await call(s, "get_plc_time")
        set_time = await call(s, "set_plc_time")
    assert info["success"] is True and info["data"]["product_name"] == "1756-L83E/B"
    assert None not in (info["data"]["name"], info["data"]["serial"], info["data"]["firmware"])
    assert set(plc_time["data"]) == {"plc_time", "microseconds"}  # no double nesting
    assert set_time["success"] is True and set_time["data"]["updated"] is True
    assert set_time["data"]["microseconds"] == controller.set_time_calls[0]


async def test_ping_and_status_reflect_the_device() -> None:
    from pycomm3 import CommError

    # One failure for the startup attempt, one for the first ping.
    controller = FakeController(open_errors=[CommError("refused")] * 2)
    async with session(controller) as s:
        down = await call(s, "ping")
        status_down = await call(s, "get_connection_status")
        up = await call(s, "ping")
        status_up = await call(s, "get_connection_status")
    assert down["success"] is False and "refused" in down["error"]
    assert status_down["data"]["connected"] is False
    assert up["success"] is True and up["data"]["reachable"] is True
    assert up["data"]["writes_enabled"] is False
    assert status_up["data"]["connected"] is True and status_up["data"]["last_error"] is None


async def test_tag_map_aliases(tag_map_file: Path) -> None:
    controller = FakeController()
    async with session(controller, writes=True, tag_map=tag_map_file) as s:
        listing = await call(s, "list_tags")
        speed = await call(s, "read_tag_by_alias", {"alias": "motor_speed"})
        written = await call(s, "write_tag_by_alias", {"alias": "motor_speed", "value": 50})
        batches = await call(s, "write_tag_by_alias", {"alias": "batches", "value": 0.46})
        unscaled = await call(s, "write_tag_by_alias", {"alias": "count_unscaled", "value": 5.7})
        unknown = await call(s, "read_tag_by_alias", {"alias": "nope"})
        no_tag = await call(s, "read_tag_by_alias", {"alias": "no_tag"})
        flat = await call(s, "read_tag_by_alias", {"alias": "flat"})
        text = await call(s, "read_tag_by_alias", {"alias": "message"})
    assert listing["success"] is True and listing["data"]["count"] == len(TAG_MAP)
    assert speed["data"]["raw_value"] == 1450.0
    assert speed["data"]["value"] == pytest.approx(80.5556, rel=1e-4)
    assert written["data"]["raw_value"] == pytest.approx(900.0)
    assert controller.tags["MotorSpeed"][0] == pytest.approx(900.0)
    assert batches["success"] is True and batches["data"]["raw_value"] == 5  # rounded for DINT
    assert ("write", (("Batch_Count", 5),)) in controller.all_calls("write")
    # Without scaling the value is passed through unchanged, and the controller refuses 5.7 for a DINT.
    assert unscaled["success"] is False and "Unable to create a writable value" in unscaled["error"]
    assert unknown["success"] is False and "Unknown alias 'nope'" in unknown["error"]
    assert no_tag["success"] is False and "has no 'tag'" in no_tag["error"]
    assert flat["success"] is False and "zero span" in flat["error"]
    assert text["success"] is True and text["data"]["value"] == "OK"  # no scaling defined


async def test_scaling_refuses_non_numeric_values(tmp_path: Path) -> None:
    path = tmp_path / "tags.json"
    path.write_text(json.dumps({"msg": {"tag": "Program:MainProgram.Alarm_Message", "scaling": {"raw_max": 10}}}))
    async with session(tag_map=path) as s:
        envelope = await call(s, "read_tag_by_alias", {"alias": "msg"})
    assert envelope["success"] is False and "non-numeric" in envelope["error"]


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"a": 1}'])
async def test_broken_tag_map_is_reported(tmp_path: Path, content: str) -> None:
    path = tmp_path / "tags.json"
    path.write_text(content)
    async with session(tag_map=path) as s:
        listing = await call(s, "list_tags")
        read = await call(s, "read_tag_by_alias", {"alias": "a"})
    assert listing["success"] is False and "TAG_MAP_FILE" in listing["error"]
    assert read["success"] is False and "TAG_MAP_FILE" in read["error"]


async def test_missing_tag_map_is_reported(tmp_path: Path) -> None:
    async with session(tag_map=tmp_path / "missing.json") as s:
        listing = await call(s, "list_tags")
    assert listing["success"] is False and "cannot be read" in listing["error"]


async def test_tag_map_reloads_when_changed(tmp_path: Path) -> None:
    path = tmp_path / "tags.json"
    path.write_text(json.dumps({"a": {"tag": "MotorSpeed"}}))
    async with session(tag_map=path) as s:
        first = await call(s, "list_tags")
        path.write_text(json.dumps({"a": {"tag": "MotorSpeed"}, "b": {"tag": "Batch_Count"}}))
        second = await call(s, "list_tags")
    assert first["data"]["count"] == 1 and second["data"]["count"] == 2


async def test_no_tag_map_configured() -> None:
    async with session() as s:
        listing = await call(s, "list_tags")
        read = await call(s, "read_tag_by_alias", {"alias": "x"})
    assert listing["success"] is True and listing["data"] == {"aliases": [], "count": 0}
    assert read["success"] is False and "none defined" in read["error"]


async def test_lost_write_reply_is_reported_as_unknown_outcome() -> None:
    from pycomm3 import CommError

    controller = FakeController(reply_errors=[CommError("failed to receive reply")])
    async with session(controller, writes=True) as s:
        envelope = await call(s, "write_tag", {"tag_name": "Batch_Count", "value": 3})
    assert envelope["success"] is False and "may have been applied" in envelope["error"]
    assert envelope["meta"]["outcome"] == "unknown"
    assert len(controller.all_calls("write")) == 1


async def test_write_refusals_say_nothing_was_sent(tag_map_file: Path) -> None:
    cases = [
        (session(), "write_tag", {"tag_name": "MotorSpeed", "value": 1.0}),  # writes disabled
        (session(writes=True), "set_plc_time", {}),  # system commands disabled
        (session(writes=True), "write_tag", {"tag_name": "MotorSpeed"}),  # invalid arguments
        (session(writes=True), "write_tag", {"tag_name": "MotorSpeed", "value": None}),  # no value
        (session(writes=True), "write_multiple_tags", {"payloads": [{"value": 1}]}),  # bad payload
        (session(writes=True, tag_map=tag_map_file), "write_tag_by_alias", {"alias": "nope", "value": 1}),
        (session(writes=True, tag_map=tag_map_file), "write_tag_by_alias", {"alias": "flat", "value": 1}),
    ]
    for context, tool, arguments in cases:
        async with context as s:
            envelope = await call(s, tool, arguments)
        assert envelope["success"] is False, (tool, arguments)
        assert envelope["meta"]["outcome"] == "not_sent" and envelope["meta"]["request_sent"] is False, (
            tool,
            arguments,
        )


async def test_successful_writes_report_outcome() -> None:
    async with session(writes=True, system=True) as s:
        written = await call(s, "write_tag", {"tag_name": "MotorSpeed", "value": 2.0})
        clock = await call(s, "set_plc_time")
    for envelope in (written, clock):
        assert envelope["success"] is True
        assert envelope["meta"]["outcome"] == "written" and envelope["meta"]["request_sent"] is True


async def test_bool_is_not_written_into_a_dint() -> None:
    controller = FakeController()
    async with session(controller, writes=True) as s:
        envelope = await call(s, "write_tag", {"tag_name": "Batch_Count", "value": True})
    assert envelope["success"] is False and "a boolean is not accepted" in envelope["error"]
    assert envelope["meta"]["outcome"] == "not_sent"
    assert controller.all_calls("write") == []


async def test_bool_scaling_values_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "tags.json"
    path.write_text(json.dumps({"x": {"tag": "MotorSpeed", "scaling": {"raw_min": 0, "raw_max": True}}}))
    async with session(tag_map=path) as s:
        envelope = await call(s, "read_tag_by_alias", {"alias": "x"})
    assert envelope["success"] is False and "not booleans" in envelope["error"]


# -- round 3 -------------------------------------------------------------------


async def test_internal_error_after_a_write_reports_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = FakeController()
    client = EIPClient(EIPClientConfig(max_retries=0), driver_factory=controller.factory)
    original = client.write_tag

    async def write_then_crash(*args: Any, **kwargs: Any) -> Any:
        await original(*args, **kwargs)  # the controller applied it...
        raise RuntimeError("bug after the write")  # ...then the server failed

    monkeypatch.setattr(client, "write_tag", write_then_crash)
    server = EtherNetIPMCPServer(client=client, tool_config=ToolConfig(writes_enabled=True))
    async with create_connected_server_and_client_session(server.mcp) as s:
        envelope = await call(s, "write_tag", {"tag_name": "Batch_Count", "value": 9})
        read = await call(s, "read_tag", {"tag_name": "MotorSpeed"})
    assert envelope["success"] is False and "Internal error in write_tag" in envelope["error"]
    assert "may have been applied" in envelope["error"]
    assert envelope["meta"]["outcome"] == "unknown" and envelope["meta"]["request_sent"] is True
    assert controller.tags["Batch_Count"] == (9, "DINT")
    assert read["success"] is True


async def test_internal_error_in_a_read_has_no_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = FakeController()
    client = EIPClient(EIPClientConfig(max_retries=0), driver_factory=controller.factory)

    async def crash(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(client, "read_tag", crash)
    server = EtherNetIPMCPServer(client=client, tool_config=ToolConfig())
    async with create_connected_server_and_client_session(server.mcp) as s:
        envelope = await call(s, "read_tag", {"tag_name": "MotorSpeed"})
    assert envelope["success"] is False and "outcome" not in envelope["meta"]


@pytest.mark.parametrize("data_type", [5, "", ["REAL"], True])
async def test_invalid_tag_map_data_type_is_refused_before_writing(tmp_path: Path, data_type: Any) -> None:
    path = tmp_path / "tags.json"
    path.write_text(json.dumps({"start": {"tag": "Batch_Count", "data_type": data_type}}))
    controller = FakeController()
    async with session(controller, writes=True, tag_map=path) as s:
        written = await call(s, "write_tag_by_alias", {"alias": "start", "value": 1})
        read = await call(s, "read_tag_by_alias", {"alias": "start"})
    assert written["success"] is False and "invalid 'data_type'" in written["error"]
    assert written["meta"]["outcome"] == "not_sent" and written["meta"]["request_sent"] is False
    assert read["success"] is False and "invalid 'data_type'" in read["error"]
    assert controller.all_calls("write") == []


async def test_tool_annotations_never_invite_retries() -> None:
    from ethernetip_mcp.tools import WRITE_TOOLS

    async with session() as s:
        tools = {tool.name: tool for tool in (await s.list_tools()).tools}
    assert WRITE_TOOLS == {
        "write_tag",
        "write_array",
        "write_string",
        "write_multiple_tags",
        "write_tag_by_alias",
        "set_plc_time",
    }
    for name, tool in tools.items():
        hints = tool.annotations
        assert hints is not None, name
        if name in WRITE_TOOLS:
            assert hints.readOnlyHint is False, name
            assert hints.destructiveHint is True, name
            assert hints.idempotentHint is False, name
        else:
            assert hints.readOnlyHint is True, name
            assert hints.idempotentHint is not False or hints.readOnlyHint, name
    assert tools["list_tags"].annotations.openWorldHint is False
    assert tools["read_tag"].annotations.openWorldHint is True
