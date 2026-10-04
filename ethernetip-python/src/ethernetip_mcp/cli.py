"""CLI entry point for the EtherNet/IP MCP server."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, MutableMapping
from pathlib import Path

from dotenv import dotenv_values

from .eip_client import ConfigError
from .server import EtherNetIPMCPServer

# Where the automatic .env search starts, walking up to the filesystem root
# (what python-dotenv's load_dotenv() does by default). For a source checkout
# this finds ethernetip-python/.env, then the repository root's .env.
DOTENV_SEARCH_START = Path(__file__).resolve().parent

# Variables that let the server change the device. An automatically found
# .env must not turn them on: with a direct venv launch the search can reach
# files the operator did not write for this server.
_SAFETY_SWITCH = re.compile(r"_(WRITES_ENABLED|SYSTEM_CMDS_ENABLED|CONFIG_CMDS_ENABLED|STATE_CHANGE_ENABLED)$")


def is_safety_switch(name: str) -> bool:
    return bool(_SAFETY_SWITCH.search(name))


def discover_env_file(start: Path) -> Path | None:
    """Return the first ``.env`` in ``start`` or one of its parents."""
    for directory in (start, *start.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def _warn(message: str) -> None:
    print(f"ethernetip-mcp: warning: {message}", file=sys.stderr)


def load_environment(
    env_file: Path | None,
    *,
    start: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
    warn: Callable[[str], None] = _warn,
) -> Path | None:
    """Apply a .env file to the environment; the real environment always wins.

    With ``env_file`` (``--env-file``), every variable in it is honoured,
    including the safety switches. Otherwise the first ``.env`` found from
    ``start`` upwards may set connection settings only: a safety switch in it
    (``ENIP_WRITES_ENABLED``, ``ENIP_SYSTEM_CMDS_ENABLED``, ...) is ignored
    with a warning on stderr. Returns the file used, if any.
    """
    env = os.environ if environ is None else environ
    if env_file is not None:
        if not env_file.is_file():
            raise ConfigError(f"--env-file {env_file} does not exist")
        for key, value in dotenv_values(env_file).items():
            if value is not None and key not in env:
                env[key] = value
        return env_file

    found = discover_env_file(start or DOTENV_SEARCH_START)
    if found is None:
        return None
    for key, value in dotenv_values(found).items():
        if value is None or key in env:
            continue
        if is_safety_switch(key):
            warn(
                f"ignoring {key} from {found}: an automatically found .env cannot enable writes or commands. "
                "Set it in the process environment (the MCP client's env) or pass --env-file."
            )
            continue
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
        "(an automatically found .env may only set connection settings)",
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
