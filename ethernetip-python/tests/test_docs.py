"""The README must describe the configuration and the breaking changes the code has."""

from __future__ import annotations

import re

import pytest
from conftest import REPO_ROOT

SOURCES = REPO_ROOT / "ethernetip-python" / "src" / "ethernetip_mcp"
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

# Every tool whose response shape changed in this version (see the BREAKING
# CHANGE footers in the git history).
CHANGED_TOOLS = [
    "read_tag",
    "read_array",
    "write_tag",
    "write_array",
    "write_string",
    "write_multiple_tags",
    "read_multiple_tags",
    "read_string",
    "read_tag_by_alias",
    "write_tag_by_alias",
    "list_tags",
    "get_tag_list",
    "get_plc_info",
    "get_plc_time",
    "set_plc_time",
    "ping",
    "get_connection_status",
]


def _section(title: str) -> str:
    match = re.search(rf"^#+ {re.escape(title)}\n(.*?)(?=^#+ )", README, flags=re.S | re.M)
    assert match, f"README has no '{title}' section"
    return match.group(1)


def settings_read_by_the_server() -> set[str]:
    names: set[str] = set()
    for path in SOURCES.glob("*.py"):
        names |= set(re.findall(r'"((?:ENIP_[A-Z0-9_]+)|TAG_MAP_FILE)"', path.read_text(encoding="utf-8")))
    return names


@pytest.mark.parametrize("name", sorted(settings_read_by_the_server()))
def test_every_setting_has_a_configuration_row(name: str) -> None:
    assert re.search(rf"^\| `{name}` \|", _section("Configuration"), flags=re.M), name


@pytest.mark.parametrize("tool", CHANGED_TOOLS)
def test_behaviour_changes_list_every_changed_tool(tool: str) -> None:
    assert f"`{tool}`" in _section("Behaviour changes in this version"), tool


def test_behaviour_changes_cover_gates_and_retries() -> None:
    section = _section("Behaviour changes in this version")
    assert "ENIP_WRITES_ENABLED=false" in section
    assert "ENIP_SYSTEM_CMDS_ENABLED=true" in section
    assert '"unknown"' in section


def test_removed_settings_are_named() -> None:
    section = _section("Behaviour changes in this version")
    for name in ("ENIP_INIT_INFO", "ENIP_CACHE_TAG_LIST", "ENIP_CACHE_TIMEOUT"):
        assert name in section
        assert name not in settings_read_by_the_server()


# -- the .env rule is documented where people look (suite rule 3) -------------


@pytest.mark.parametrize(
    "path",
    ["README.md", "SECURITY.md", "ethernetip-python/README.md", "ethernetip-python/.env.example"],
)
def test_env_file_rule_is_documented(path: str) -> None:
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert "--env-file" in text
    assert "ENIP_WRITES_ENABLED" in text and "ENIP_SYSTEM_CMDS_ENABLED" in text


def test_env_example_lists_every_setting() -> None:
    example = (REPO_ROOT / "ethernetip-python" / ".env.example").read_text(encoding="utf-8")
    for name in settings_read_by_the_server():
        assert re.search(rf"^#? ?{name}=", example, flags=re.M), name


# -- documented response keys match real envelopes ---------------------------

DOCUMENTED_KEYS = {
    "read_tag": "{tag, value, data_type}",
    "write_tag": "{tag, value, data_type}",
    "write_multiple_tags": "{tag, value, data_type, error, outcome, request_sent}",
    "get_tag_list": "{tag, data_type, dimensions, tag_type, alias, external_access, description}",
    "get_plc_info": "{name, vendor, product_type, product_code, product_name, revision, firmware, serial, keyswitch}",
    "get_plc_time": "{plc_time, microseconds}",
    "set_plc_time": "{updated, plc_time, microseconds}",
    "ping": "{reachable, latency_ms, product_name, connection, writes_enabled, system_cmds_enabled, tag_aliases}",
}
CALLS = {
    "read_tag": {"tag_name": "MotorSpeed"},
    "write_tag": {"tag_name": "MotorSpeed", "value": 2.0},
    "write_multiple_tags": {"payloads": [{"tag_name": "MotorSpeed", "value": 2.0}]},
    "get_tag_list": {},
    "get_plc_info": {},
    "get_plc_time": {},
    "set_plc_time": {},
    "ping": {},
}


def _keys(text: str) -> set[str]:
    return {part.strip() for part in text.strip("{}").split(",")}


@pytest.mark.parametrize("tool", sorted(DOCUMENTED_KEYS))
async def test_documented_keys_match_the_envelope(tool: str) -> None:
    from fake_pycomm3 import FakeController
    from mcp.shared.memory import create_connected_server_and_client_session

    from ethernetip_mcp.eip_client import EIPClient, EIPClientConfig
    from ethernetip_mcp.server import EtherNetIPMCPServer
    from ethernetip_mcp.tools import ToolConfig

    assert DOCUMENTED_KEYS[tool] in README, f"README does not show {DOCUMENTED_KEYS[tool]} for {tool}"
    controller = FakeController()
    client = EIPClient(EIPClientConfig(max_retries=0), driver_factory=controller.factory)
    server = EtherNetIPMCPServer(client=client, tool_config=ToolConfig(writes_enabled=True, system_cmds_enabled=True))
    async with create_connected_server_and_client_session(server.mcp) as session:
        envelope = (await session.call_tool(tool, CALLS[tool])).structuredContent
    assert envelope["success"] is True, envelope["error"]
    data = envelope["data"]
    if tool == "write_multiple_tags":
        data = data["results"][0]
    elif tool == "get_tag_list":
        data = data["tags"][0]
    assert set(data) == _keys(DOCUMENTED_KEYS[tool])
