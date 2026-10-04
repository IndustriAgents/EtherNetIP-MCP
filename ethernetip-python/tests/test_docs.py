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
