"""Configuration parsing, defaults, and when the configuration is read."""

from __future__ import annotations

import os
import subprocess

import pytest
from conftest import server_command, server_env

from ethernetip_mcp import cli
from ethernetip_mcp.eip_client import ConfigError, EIPClientConfig
from ethernetip_mcp.tools import ToolConfig


def test_defaults() -> None:
    config = EIPClientConfig.from_env({})
    assert config.host == "127.0.0.1"
    assert config.port == 44818
    assert config.slot == 0
    assert config.timeout == 5.0
    assert config.max_retries == 3
    assert config.json_bridge is False
    assert config.micro800 is False
    assert config.cip_path() == "127.0.0.1:44818"


def test_writes_and_system_commands_are_off_by_default() -> None:
    tools = ToolConfig.from_env({})
    assert tools.writes_enabled is False
    assert tools.system_cmds_enabled is False
    assert ToolConfig().writes_enabled is False


@pytest.mark.parametrize("raw, expected", [("true", True), ("1", True), ("YES", True), ("false", False), ("0", False)])
def test_boolean_values(raw: str, expected: bool) -> None:
    assert ToolConfig.from_env({"ENIP_WRITES_ENABLED": raw}).writes_enabled is expected


@pytest.mark.parametrize(
    "env, message",
    [
        ({"ENIP_WRITES_ENABLED": "ture"}, "ENIP_WRITES_ENABLED"),
        ({"ENIP_JSON_BRIDGE": "maybe"}, "ENIP_JSON_BRIDGE"),
        ({"ENIP_PORT": "abc"}, "ENIP_PORT"),
        ({"ENIP_PORT": "70000"}, "ENIP_PORT"),
        ({"ENIP_PORT": "65535"}, "ENIP_PORT"),  # pycomm3 rejects 65535
        ({"ENIP_SLOT": "-1"}, "ENIP_SLOT"),
        ({"ENIP_TIMEOUT": "0"}, "ENIP_TIMEOUT"),
        ({"ENIP_TIMEOUT": "nan"}, "ENIP_TIMEOUT"),
        ({"ENIP_MAX_RETRIES": "-1"}, "ENIP_MAX_RETRIES"),
        ({"ENIP_HOST": "10.0.0.5:x"}, "ENIP_HOST"),
        ({"ENIP_HOST": "10.0.0.5:44819", "ENIP_PORT": "44818"}, "conflicts"),
        ({"ENIP_MICRO800": "true", "ENIP_SLOT": "2"}, "Micro800"),
        ({"ENIP_JSON_BRIDGE": "true", "ENIP_PATH": "10.0.0.5/bp/0"}, "ENIP_PATH"),
    ],
)
def test_invalid_configuration_is_rejected(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        if "ENIP_WRITES_ENABLED" in env:
            ToolConfig.from_env(env)
        else:
            EIPClientConfig.from_env(env)


@pytest.mark.parametrize(
    "host, expected",
    [
        ("::1", ("::1", 44818)),
        ("[::1]", ("::1", 44818)),
        ("[::1]:5025", ("::1", 5025)),
        ("fe80::1", ("fe80::1", 44818)),
        ("localhost:5025", ("localhost", 5025)),
    ],
)
def test_json_bridge_accepts_ipv6(host: str, expected: tuple[str, int]) -> None:
    config = EIPClientConfig.from_env({"ENIP_JSON_BRIDGE": "true", "ENIP_HOST": host})
    assert (config.host, config.port) == expected


@pytest.mark.parametrize("host", ["::1", "[::1]:44818", "[fe80::1]"])
def test_cip_rejects_ipv6_clearly(host: str) -> None:
    with pytest.raises(ConfigError, match="IPv6 address; pycomm3 connects to controllers over IPv4 only"):
        EIPClientConfig.from_env({"ENIP_HOST": host})


@pytest.mark.parametrize("host", ["[::1", "[]:5025", "[::1]5025", "[::1]:x"])
def test_malformed_bracketed_hosts(host: str) -> None:
    with pytest.raises(ConfigError, match="ENIP_HOST"):
        EIPClientConfig.from_env({"ENIP_JSON_BRIDGE": "true", "ENIP_HOST": host})


def test_json_bridge_accepts_port_65535() -> None:
    assert EIPClientConfig.from_env({"ENIP_JSON_BRIDGE": "true", "ENIP_PORT": "65535"}).port == 65535


def test_blank_values_mean_unset() -> None:
    config = EIPClientConfig.from_env({"ENIP_HOST": " ", "ENIP_PORT": "", "ENIP_PATH": ""})
    assert (config.host, config.port, config.route) == ("127.0.0.1", 44818, None)


@pytest.mark.parametrize(
    "env, path",
    [
        ({"ENIP_HOST": "10.0.0.5"}, "10.0.0.5:44818"),
        ({"ENIP_HOST": "10.0.0.5", "ENIP_SLOT": "2"}, "10.0.0.5:44818/2"),
        ({"ENIP_HOST": "10.0.0.5", "ENIP_PORT": "44819"}, "10.0.0.5:44819"),
        ({"ENIP_HOST": "10.0.0.5:44819"}, "10.0.0.5:44819"),
        ({"ENIP_HOST": "10.0.0.5:44819", "ENIP_PORT": "44819"}, "10.0.0.5:44819"),
        ({"ENIP_HOST": "10.0.0.5", "ENIP_MICRO800": "true"}, "10.0.0.5:44818"),
        ({"ENIP_PATH": "10.0.0.5/backplane/2"}, "10.0.0.5:44818/backplane/2"),
        ({"ENIP_PATH": "10.0.0.5,bp,2", "ENIP_PORT": "2222"}, "10.0.0.5:2222/bp/2"),
        ({"ENIP_PATH": "10.0.0.5:44820/bp/0/enet/192.168.1.10"}, "10.0.0.5:44820/bp/0/enet/192.168.1.10"),
        ({"ENIP_PATH": "10.0.0.5", "ENIP_SLOT": "3"}, "10.0.0.5:44818"),  # ENIP_PATH overrides the slot
    ],
)
def test_cip_path(env: dict[str, str], path: str) -> None:
    assert EIPClientConfig.from_env(env).cip_path() == path


def test_importing_reads_no_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings are read when the server is built, so a later load_dotenv() counts."""
    monkeypatch.setenv("ENIP_WRITES_ENABLED", "true")
    monkeypatch.setenv("ENIP_PORT", "5025")
    monkeypatch.setenv("ENIP_JSON_BRIDGE", "true")
    from ethernetip_mcp.server import EtherNetIPMCPServer

    server = EtherNetIPMCPServer()
    assert server.tool_config.writes_enabled is True
    assert server.client.config.port == 5025


def test_cli_reads_configuration_after_load_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ENIP_WRITES_ENABLED", "ENIP_PORT", "ENIP_JSON_BRIDGE", "ENIP_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)

    def fake_load_dotenv(*args: object, **kwargs: object) -> bool:
        # What a .env file would contribute.
        os.environ["ENIP_WRITES_ENABLED"] = "true"
        os.environ["ENIP_PORT"] = "5025"
        os.environ["ENIP_JSON_BRIDGE"] = "true"
        os.environ["ENIP_TIMEOUT"] = "2.5"
        return True

    built = {}

    def fake_run(self: object) -> None:
        built["server"] = self

    monkeypatch.setattr(cli, "load_dotenv", fake_load_dotenv)
    monkeypatch.setattr(cli.EtherNetIPMCPServer, "run", fake_run)
    try:
        cli.main()
    finally:
        for name in ("ENIP_WRITES_ENABLED", "ENIP_PORT", "ENIP_JSON_BRIDGE", "ENIP_TIMEOUT"):
            os.environ.pop(name, None)
    server = built["server"]
    assert server.tool_config.writes_enabled is True
    assert server.client.config.port == 5025
    assert server.client.config.json_bridge is True
    assert server.client.config.timeout == 2.5


def test_bad_configuration_exits_with_message_on_stderr() -> None:
    result = subprocess.run(
        server_command(),
        env={**os.environ, **server_env(ENIP_PORT="not-a-port")},
        input=b"",
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert result.stdout == b""  # stdout is reserved for MCP
    assert b"configuration error: ENIP_PORT='not-a-port' is not an integer" in result.stderr
    assert b"Traceback" not in result.stderr
