# ethernetip-python

Python MCP server for Rockwell/Allen-Bradley EtherNet/IP controllers. Built with `pycomm3`, `mcp[cli]` (1.x) and `uv`, it exposes the `ethernetip-mcp` entry point for AI agents and MCP-compatible clients.

## Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- Network access to a controller or the included [mock server](../ethernetip-mock-server/)

## Quick Start

Run these from this directory (`ethernetip-python/`).

Against a controller (read-only, the default):

```bash
uv sync
ENIP_HOST=192.168.1.10 ENIP_SLOT=0 uv run ethernetip-mcp
```

Against the mock PLC (start it first, see [its README](../ethernetip-mock-server/README.md)):

```bash
uv sync
ENIP_HOST=127.0.0.1 ENIP_PORT=5025 ENIP_JSON_BRIDGE=true uv run ethernetip-mcp
```

The server speaks MCP over stdio, so it waits for a client on stdin; its logs go to stderr. Configure it with environment variables; the full list is in the [Configuration](../README.md#configuration) section of the top-level README, and [`.env.example`](.env.example) shows them all.

A `.env` file in this directory (`ethernetip-python/.env`) is read automatically, but only for this server's own connection settings (`ENIP_*`, `TAG_MAP_FILE`). No other `.env` is read implicitly: not one in the working directory, not one in a parent directory, and none at all when the package is installed rather than run from this checkout. `ENIP_WRITES_ENABLED`, `ENIP_SYSTEM_CMDS_ENABLED` and any other variable in the automatic `.env` are ignored, with a warning on stderr. Set the switches in the MCP client's `env`, or pass a file explicitly with `uv run ethernetip-mcp --env-file /path/to/eip.env`, which honours every setting in it. Write tools are refused unless `ENIP_WRITES_ENABLED=true`.

## Layout

```
ethernetip-python/
├── README.md
├── pyproject.toml
├── uv.lock
├── .python-version
├── .env.example        # every setting; copy for --env-file
├── src/ethernetip_mcp
│   ├── __init__.py
│   ├── cli.py          # entry point: load .env, build the server, run stdio
│   ├── server.py       # FastMCP wiring, argument errors as envelopes
│   ├── tools.py        # the 17 tools, write gates, tag map
│   └── eip_client.py   # configuration, pycomm3 (CIP) and JSON-bridge backends
└── tests
    ├── fake_pycomm3.py         # LogixDriver stand-in that enforces pycomm3's real signatures
    ├── test_config.py
    ├── test_docs.py            # README covers every setting, breaking change and response shape
    ├── test_env.py             # an automatic .env cannot enable writes; --env-file can
    ├── test_pycomm3_path.py    # pycomm3 call shapes, retries, writes sent once, port/timeout/Micro800
    ├── test_tools.py           # tools over an in-memory MCP session
    ├── test_json_bridge.py     # client against the mock PLC (subprocess)
    └── test_integration.py     # ethernetip-mcp over stdio against the mock PLC
```

## Tests

```bash
uv sync --extra dev
(cd ../ethernetip-mock-server && uv sync)   # the integration tests start the mock with `uv run`
uv run --extra dev pytest
uv run --extra dev ruff check src tests
uv run --extra dev ruff format --check src tests
```

The integration tests pick a free local port for the mock and stop it afterwards. Without `uv` on `PATH` they are skipped, unless `ENIP_REQUIRE_INTEGRATION=1` is set (as in CI), which makes that a failure.

## Status

`server.py`, `eip_client.py` and `tools.py` implement connection management with bounded retries, input validation, the shared response envelope and the initial tool surface (`read_tag`, `write_tag`, array/string helpers, batch read/write, tag list, tag map, PLC identity and time, and health checks). The `pycomm3` path is tested against `pycomm3`'s real signatures but not yet against a physical controller; see [Known limitations](../README.md#known-limitations). Remaining planned features, such as UDT operations, module info and session control, are listed under [Planned Capabilities](../README.md#planned-capabilities).
