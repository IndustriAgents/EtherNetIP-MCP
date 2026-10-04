"""Where settings may come from (suite rules 3, 3a, 3b and 3c).

- Only ``ethernetip-python/.env`` of a source checkout is loaded implicitly:
  never the working directory, a parent directory, or anything for an
  installed package.
- That file may set this server's own non-switch settings only (ENIP_*,
  TAG_MAP_FILE), compared case-insensitively. Safety switches and foreign
  variables (PATH, PYTHONPATH, LD_PRELOAD, ...) are ignored with a warning.
- ``--env-file`` is honoured in full; the real environment always wins.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, MutableMapping
from pathlib import Path

import pytest

from ethernetip_mcp import cli
from ethernetip_mcp.eip_client import ConfigError, EIPClientConfig
from ethernetip_mcp.tools import ToolConfig

GATES_AND_CONNECTION = (
    "ENIP_WRITES_ENABLED=true\nENIP_SYSTEM_CMDS_ENABLED=true\nENIP_PORT=5025\nENIP_JSON_BRIDGE=true\n"
)
SETTINGS = ("ENIP_WRITES_ENABLED", "ENIP_SYSTEM_CMDS_ENABLED", "ENIP_PORT", "ENIP_JSON_BRIDGE", "ENIP_HOST")


class WindowsEnviron(MutableMapping[str, str]):
    """Like CPython's os.environ on Windows: keys are upper-cased."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def __getitem__(self, key: str) -> str:
        return self._data[key.upper()]

    def __setitem__(self, key: str, value: str) -> None:
        self._data[key.upper()] = value

    def __delitem__(self, key: str) -> None:
        del self._data[key.upper()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.upper() in self._data


def make_checkout(root: Path, name: str = "ethernetip-mcp") -> Path:
    """A source-checkout layout: <root>/ethernetip-python/src/ethernetip_mcp. Returns the package dir."""
    project = root / "ethernetip-python"
    package = project / "src" / "ethernetip_mcp"
    package.mkdir(parents=True)
    (project / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0"\n')
    return package


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    yield
    for name in SETTINGS:
        os.environ.pop(name, None)


def load(package: Path, environ: MutableMapping[str, str] | None = None) -> tuple[MutableMapping[str, str], list[str]]:
    env: MutableMapping[str, str] = {} if environ is None else environ
    warnings: list[str] = []
    cli.load_environment(None, package_dir=package, environ=env, warn=warnings.append)
    return env, warnings


def test_project_env_sets_connection_settings_but_not_gates(tmp_path: Path) -> None:
    package = make_checkout(tmp_path)
    (package.parents[1] / ".env").write_text(GATES_AND_CONNECTION)
    env, warnings = load(package)
    assert dict(env) == {"ENIP_PORT": "5025", "ENIP_JSON_BRIDGE": "true"}
    assert len(warnings) == 2
    assert "ignoring ENIP_WRITES_ENABLED" in warnings[0] and "ignoring ENIP_SYSTEM_CMDS_ENABLED" in warnings[1]
    assert all("--env-file" in w for w in warnings)


@pytest.mark.parametrize(
    "body",
    [
        "enip_writes_enabled=true\nenip_system_cmds_enabled=true\n",
        "Enip_Writes_Enabled=true\nENIP_System_Cmds_Enabled=1\n",
        "export ENIP_WRITES_ENABLED=true\nexport ENIP_SYSTEM_CMDS_ENABLED=true\n",
        "ENIP_WRITES_ENABLED=\"true\"\nENIP_SYSTEM_CMDS_ENABLED='yes'\n",
        "ENIP_WRITES_ENABLED = true\n  ENIP_SYSTEM_CMDS_ENABLED=on\n",
    ],
    ids=["lower-case", "mixed-case", "export", "quoted", "spaces"],
)
@pytest.mark.parametrize("environ_kind", ["posix", "windows"])
def test_gates_are_ignored_in_every_spelling(tmp_path: Path, body: str, environ_kind: str) -> None:
    package = make_checkout(tmp_path)
    (package.parents[1] / ".env").write_text(body)
    env, warnings = load(package, WindowsEnviron() if environ_kind == "windows" else {})
    tools = ToolConfig.from_env(env)
    assert tools.writes_enabled is False and tools.system_cmds_enabled is False
    assert len(warnings) == 2 and all("ignoring ENIP_" in w for w in warnings)
    assert not any("WRITES" in key or "SYSTEM" in key for key in env)


def test_own_settings_are_normalised(tmp_path: Path) -> None:
    package = make_checkout(tmp_path)
    (package.parents[1] / ".env").write_text(
        "enip_port=5025\nexport Enip_Json_Bridge=true\ntag_map_file=/tmp/map.json\n"
    )
    env, warnings = load(package)
    assert dict(env) == {"ENIP_PORT": "5025", "ENIP_JSON_BRIDGE": "true", "TAG_MAP_FILE": "/tmp/map.json"}
    assert warnings == []
    config = EIPClientConfig.from_env(env)
    assert config.port == 5025 and config.json_bridge is True


@pytest.mark.parametrize(
    "key",
    ["PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "PYTHONSTARTUP", "HTTPS_PROXY", "path", "Foo_Port"],
)
def test_foreign_variables_are_ignored(tmp_path: Path, key: str) -> None:
    package = make_checkout(tmp_path)
    (package.parents[1] / ".env").write_text(f"{key}=/evil\nENIP_HOST=10.0.0.5\n")
    env, warnings = load(package)
    assert dict(env) == {"ENIP_HOST": "10.0.0.5"}
    assert len(warnings) == 1 and f"ignoring {key.upper()}" in warnings[0]


def test_env_in_working_directory_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = make_checkout(tmp_path / "checkout")  # no .env in the project
    workdir = tmp_path / "client-project"
    workdir.mkdir()
    (workdir / ".env").write_text("ENIP_HOST=10.6.6.6\nENIP_PORT=1\n")
    monkeypatch.chdir(workdir)
    env, warnings = load(package)
    assert dict(env) == {} and warnings == []


def test_env_in_a_parent_directory_is_not_read(tmp_path: Path) -> None:
    package = make_checkout(tmp_path)
    (tmp_path / ".env").write_text("ENIP_HOST=10.6.6.6\n")  # the repository root, a parent of the project
    (tmp_path / "ethernetip-python" / "src" / ".env").write_text("ENIP_HOST=10.7.7.7\n")
    env, _ = load(package)
    assert dict(env) == {}


def test_installed_package_has_no_implicit_env(tmp_path: Path) -> None:
    package = tmp_path / "lib" / "python3.11" / "site-packages" / "ethernetip_mcp"
    package.mkdir(parents=True)
    for directory in (package.parent, package.parents[1], package.parents[2]):
        (directory / ".env").write_text("ENIP_HOST=10.6.6.6\n")
    assert cli.project_env_file(package) is None
    assert dict(load(package)[0]) == {}


def test_other_projects_env_is_not_read(tmp_path: Path) -> None:
    package = make_checkout(tmp_path, name="someone-else")
    (package.parents[1] / ".env").write_text("ENIP_HOST=10.6.6.6\n")
    assert cli.project_env_file(package) is None


def test_real_source_checkout_is_recognised() -> None:
    package = Path(cli.__file__).resolve().parent
    assert package == cli.PACKAGE_DIR
    found = cli.project_env_file()
    assert found is None or found == package.parents[1] / ".env"


def test_explicit_env_file_may_turn_gates_on(tmp_path: Path) -> None:
    path = tmp_path / "server.env"
    path.write_text(GATES_AND_CONNECTION)
    environ: dict[str, str] = {}
    warnings: list[str] = []
    assert cli.load_environment(path, environ=environ, warn=warnings.append) == path
    assert environ["ENIP_WRITES_ENABLED"] == "true" and environ["ENIP_SYSTEM_CMDS_ENABLED"] == "true"
    assert warnings == []


def test_explicit_env_file_skips_the_project_env(tmp_path: Path) -> None:
    package = make_checkout(tmp_path)
    (package.parents[1] / ".env").write_text("ENIP_HOST=10.9.9.9\n")
    explicit = tmp_path / "explicit.env"
    explicit.write_text("ENIP_PORT=1234\n")
    environ: dict[str, str] = {}
    cli.load_environment(explicit, package_dir=package, environ=environ)
    assert environ == {"ENIP_PORT": "1234"}


def test_real_environment_wins(tmp_path: Path) -> None:
    package = make_checkout(tmp_path)
    (package.parents[1] / ".env").write_text(GATES_AND_CONNECTION)
    environ = {"ENIP_PORT": "44818", "ENIP_WRITES_ENABLED": "false"}
    load(package, environ)
    assert environ["ENIP_PORT"] == "44818" and environ["ENIP_WRITES_ENABLED"] == "false"
    explicit = tmp_path / "explicit.env"
    explicit.write_text(GATES_AND_CONNECTION)
    cli.load_environment(explicit, environ=environ)
    assert environ["ENIP_PORT"] == "44818" and environ["ENIP_WRITES_ENABLED"] == "false"


def test_missing_explicit_env_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        cli.load_environment(tmp_path / "nope.env", environ={})


@pytest.mark.parametrize(
    "name, switch",
    [
        ("ENIP_WRITES_ENABLED", True),
        ("enip_writes_enabled", True),
        (" export Enip_System_Cmds_Enabled ", True),
        ("FOO_CONFIG_CMDS_ENABLED", True),
        ("foo_state_change_enabled", True),
        ("ENIP_PORT", False),
        ("ENIP_DEBUG", False),
    ],
)
def test_safety_switch_names(name: str, switch: bool) -> None:
    assert cli.is_safety_switch(name) is switch


def test_main_ignores_gates_from_the_project_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], clean_env: None
) -> None:
    package = make_checkout(tmp_path)
    (package.parents[1] / ".env").write_text("enip_writes_enabled=true\n" + GATES_AND_CONNECTION)
    monkeypatch.setattr(cli, "PACKAGE_DIR", package)
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


def test_main_never_reads_a_working_directory_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_env: None
) -> None:
    package = make_checkout(tmp_path / "checkout")
    workdir = tmp_path / "client-project"
    workdir.mkdir()
    (workdir / ".env").write_text("ENIP_HOST=10.6.6.6\n")
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(cli, "PACKAGE_DIR", package)
    built = {}
    monkeypatch.setattr(cli.EtherNetIPMCPServer, "run", lambda self: built.setdefault("server", self))
    cli.main([])
    assert built["server"].client.config.host == "127.0.0.1"


def test_main_honours_gates_from_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_env: None) -> None:
    path = tmp_path / "server.env"
    path.write_text(GATES_AND_CONNECTION)
    monkeypatch.setattr(cli, "PACKAGE_DIR", make_checkout(tmp_path / "checkout"))
    built = {}
    monkeypatch.setattr(cli.EtherNetIPMCPServer, "run", lambda self: built.setdefault("server", self))
    cli.main(["--env-file", str(path)])
    assert built["server"].tool_config.writes_enabled is True
    assert built["server"].tool_config.system_cmds_enabled is True
