"""CLI entry point for the EtherNet/IP MCP server."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from collections.abc import Callable, MutableMapping
from pathlib import Path

from dotenv import dotenv_values

from .eip_client import ConfigError
from .server import EtherNetIPMCPServer

PROJECT_NAME = "ethernetip-mcp"
PACKAGE_DIR = Path(__file__).resolve().parent

# Variables that let the server change the device. Only the process
# environment or an explicit --env-file may turn them on.
_SAFETY_SWITCH = re.compile(r"_(WRITES_ENABLED|SYSTEM_CMDS_ENABLED|CONFIG_CMDS_ENABLED|STATE_CHANGE_ENABLED)$")
# The only variables the implicit .env may set: this server's own settings.
_OWN_PREFIX = "ENIP_"
_OWN_EXTRAS = frozenset({"TAG_MAP_FILE"})


def normalize_key(key: str) -> str:
    """Normalise a .env key the way Windows does (upper case), dropping an ``export`` prefix."""
    key = key.strip()
    if key.lower().startswith("export "):
        key = key[len("export ") :].strip()
    return key.upper()


def is_safety_switch(name: str) -> bool:
    return bool(_SAFETY_SWITCH.search(normalize_key(name)))


def is_own_setting(name: str) -> bool:
    key = normalize_key(name)
    return (key.startswith(_OWN_PREFIX) or key in _OWN_EXTRAS) and not is_safety_switch(key)


def project_env_file(package_dir: Path | None = None) -> Path | None:
    """The implicit .env: ``ethernetip-python/.env`` of a source checkout, nothing else.

    It is looked for only in this project's own directory, found relative to
    the package source (``<project>/src/ethernetip_mcp``), and only if that
    directory's pyproject.toml is this project. Never the working directory
    or a parent directory, and never for an installed package.
    """
    package_dir = PACKAGE_DIR if package_dir is None else package_dir
    if package_dir.parent.name != "src":
        return None
    project = package_dir.parent.parent
    try:
        meta = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if meta.get("project", {}).get("name") != PROJECT_NAME:
        return None
    candidate = project / ".env"
    return candidate if candidate.is_file() else None


def _warn(message: str) -> None:
    print(f"ethernetip-mcp: warning: {message}", file=sys.stderr)


def load_environment(
    env_file: Path | None,
    *,
    package_dir: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
    warn: Callable[[str], None] = _warn,
) -> Path | None:
    """Apply a .env file to the environment; the real environment always wins.

    With ``env_file`` (``--env-file``), every variable in it is honoured,
    including the safety switches. Otherwise only ``ethernetip-python/.env``
    of a source checkout is read, and only for this server's own non-switch
    settings (``ENIP_*`` and ``TAG_MAP_FILE``). Keys are compared case-
    insensitively and applied upper-cased, as Windows would; a safety switch
    or any other variable in it (PATH, PYTHONPATH, LD_PRELOAD, proxies, ...)
    is ignored with a warning on stderr. Returns the file used, if any.
    """
    env = os.environ if environ is None else environ
    if env_file is not None:
        if not env_file.is_file():
            raise ConfigError(f"--env-file {env_file} does not exist")
        for key, value in dotenv_values(env_file).items():
            if value is not None and key not in env:
                env[key] = value
        return env_file

    found = project_env_file(package_dir)
    if found is None:
        return None
    for raw_key, value in dotenv_values(found).items():
        if value is None:
            continue
        key = normalize_key(raw_key)
        if is_safety_switch(key):
            warn(
                f"ignoring {key} from {found}: the automatically loaded .env cannot enable writes or commands. "
                "Set it in the process environment (the MCP client's env) or pass --env-file."
            )
            continue
        if not is_own_setting(key):
            warn(f"ignoring {key} from {found}: the automatically loaded .env may only set ENIP_* and TAG_MAP_FILE.")
            continue
        if key not in env:
            env[key] = value
    return found


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ethernetip-mcp",
        description="EtherNet/IP MCP server for Logix controllers. Speaks MCP over stdio; configure it with ENIP_* "
        "environment variables.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        help="read settings from this file, including ENIP_WRITES_ENABLED and ENIP_SYSTEM_CMDS_ENABLED "
        "(the automatically loaded ethernetip-python/.env may only set connection settings)",
    )
    args = parser.parse_args(argv)
    try:
        # Load the environment first: all configuration is read when the server is built.
        load_environment(args.env_file)
        server = EtherNetIPMCPServer()
    except ConfigError as exc:
        # stdout belongs to MCP, so the message goes to stderr.
        print(f"ethernetip-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
