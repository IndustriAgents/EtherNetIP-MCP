"""Safety switches never come from an automatically found .env (suite rule 3)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ethernetip_mcp import cli
from ethernetip_mcp.eip_client import ConfigError

GATES_AND_CONNECTION = (
    "ENIP_WRITES_ENABLED=true\nENIP_SYSTEM_CMDS_ENABLED=true\nENIP_PORT=5025\nENIP_JSON_BRIDGE=true\n"
)
SETTINGS = ("ENIP_WRITES_ENABLED", "ENIP_SYSTEM_CMDS_ENABLED", "ENIP_PORT", "ENIP_JSON_BRIDGE")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    yield
    for name in SETTINGS:
        os.environ.pop(name, None)


@pytest.mark.parametrize("depth", [0, 2], ids=["same-directory", "parent-directory"])
def test_auto_env_cannot_turn_gates_on(tmp_path: Path, depth: int) -> None:
    (tmp_path / ".env").write_text(GATES_AND_CONNECTION)
    start = tmp_path.joinpath(*(["nested"] * depth))
    start.mkdir(parents=True, exist_ok=True)
    environ: dict[str, str] = {}
    warnings: list[str] = []
    found = cli.load_environment(None, start=start, environ=environ, warn=warnings.append)
    assert found == tmp_path / ".env"
    assert environ == {"ENIP_PORT": "5025", "ENIP_JSON_BRIDGE": "true"}  # connection settings only
    assert len(warnings) == 2
    assert "ENIP_WRITES_ENABLED" in warnings[0] and "ENIP_SYSTEM_CMDS_ENABLED" in warnings[1]
    assert all("--env-file" in w for w in warnings)


def test_explicit_env_file_may_turn_gates_on(tmp_path: Path) -> None:
    path = tmp_path / "server.env"
    path.write_text(GATES_AND_CONNECTION)
    environ: dict[str, str] = {}
    warnings: list[str] = []
    assert cli.load_environment(path, environ=environ, warn=warnings.append) == path
    assert environ["ENIP_WRITES_ENABLED"] == "true" and environ["ENIP_SYSTEM_CMDS_ENABLED"] == "true"
    assert warnings == []


def test_explicit_env_file_skips_discovery(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ENIP_HOST=10.9.9.9\n")
    explicit = tmp_path / "explicit.env"
    explicit.write_text("ENIP_PORT=1234\n")
    environ: dict[str, str] = {}
    cli.load_environment(explicit, start=tmp_path, environ=environ)
    assert environ == {"ENIP_PORT": "1234"}


def test_real_environment_wins(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(GATES_AND_CONNECTION)
    environ = {"ENIP_PORT": "44818", "ENIP_WRITES_ENABLED": "false"}
    warnings: list[str] = []
    cli.load_environment(None, start=tmp_path, environ=environ, warn=warnings.append)
    assert environ["ENIP_PORT"] == "44818" and environ["ENIP_WRITES_ENABLED"] == "false"
    explicit = tmp_path / "explicit.env"
    explicit.write_text(GATES_AND_CONNECTION)
    cli.load_environment(explicit, environ=environ)
    assert environ["ENIP_PORT"] == "44818" and environ["ENIP_WRITES_ENABLED"] == "false"


def test_missing_explicit_env_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        cli.load_environment(tmp_path / "nope.env", environ={})


def test_no_env_file_anywhere(tmp_path: Path) -> None:
    start = tmp_path / "a"
    start.mkdir()
    # tmp_path's parents may hold a .env on a developer machine; only check the search itself.
    found = cli.discover_env_file(start)
    assert found is None or found.parent not in (start, tmp_path)


@pytest.mark.parametrize(
    "name, switch",
    [
        ("ENIP_WRITES_ENABLED", True),
        ("ENIP_SYSTEM_CMDS_ENABLED", True),
        ("FOO_CONFIG_CMDS_ENABLED", True),
        ("FOO_STATE_CHANGE_ENABLED", True),
        ("ENIP_PORT", False),
        ("ENIP_DEBUG", False),
    ],
)
def test_safety_switch_names(name: str, switch: bool) -> None:
    assert cli.is_safety_switch(name) is switch


def test_main_ignores_gates_from_a_parent_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], clean_env: None
) -> None:
    (tmp_path / ".env").write_text(GATES_AND_CONNECTION)
    start = tmp_path / "src" / "ethernetip_mcp"
    start.mkdir(parents=True)
    monkeypatch.setattr(cli, "DOTENV_SEARCH_START", start)
    built = {}
    monkeypatch.setattr(cli.EtherNetIPMCPServer, "run", lambda self: built.setdefault("server", self))
    cli.main([])
    server = built["server"]
    assert server.tool_config.writes_enabled is False
    assert server.tool_config.system_cmds_enabled is False
    assert server.client.config.port == 5025  # connection settings still apply
    err = capsys.readouterr()
    assert "ignoring ENIP_WRITES_ENABLED" in err.err and "ignoring ENIP_SYSTEM_CMDS_ENABLED" in err.err
    assert err.out == ""  # stdout stays clean for MCP


def test_main_honours_gates_from_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_env: None) -> None:
    path = tmp_path / "server.env"
    path.write_text(GATES_AND_CONNECTION)
    monkeypatch.setattr(cli, "DOTENV_SEARCH_START", tmp_path)  # would find nothing else
    built = {}
    monkeypatch.setattr(cli.EtherNetIPMCPServer, "run", lambda self: built.setdefault("server", self))
    cli.main(["--env-file", str(path)])
    assert built["server"].tool_config.writes_enabled is True
    assert built["server"].tool_config.system_cmds_enabled is True
