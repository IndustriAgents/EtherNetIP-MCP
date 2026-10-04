# ethernetip-python

Python MCP server for Rockwell/Allen-Bradley EtherNet/IP controllers. Built with `pycomm3`, `mcp[cli]`, and `uv`, it exposes the `ethernetip-mcp` entry point for AI agents and MCP-compatible clients.

## Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- Network access to a controller or the included [mock server](../ethernetip-mock-server/)

## Quick Start

Run these from this directory (`ethernetip-python/`).

Against a controller:

```bash
uv sync
ENIP_HOST=192.168.1.10 ENIP_SLOT=0 ENIP_WRITES_ENABLED=false uv run ethernetip-mcp
```

Against the mock PLC (start it first, see [its README](../ethernetip-mock-server/README.md)):

```bash
uv sync
ENIP_HOST=127.0.0.1 ENIP_PORT=5025 ENIP_JSON_BRIDGE=true uv run ethernetip-mcp
```

The server speaks MCP over stdio, so it waits for a client on stdin. Create a `.env` file in this directory or set environment variables to configure host, slot or route, retries, write permissions, and tag map path. The full list is in the [Configuration](../README.md#configuration) section of the top-level README. Writes are enabled by default; set `ENIP_WRITES_ENABLED=false` for a read-only server.

## Layout

```
ethernetip-python/
├── README.md
├── pyproject.toml
├── uv.lock
├── .python-version
└── src/ethernetip_mcp
    ├── __init__.py
    ├── cli.py
    ├── server.py
    ├── tools.py
    └── eip_client.py
```

## Status

The scaffolding in `server.py`, `eip_client.py`, and `tools.py` implements connection management, retries, structured responses, and the initial tool surface (`read_tag`, `write_tag`, array/string helpers, batch read/write, tag list, tag map, PLC time, and health checks). Extend these modules to cover the remaining planned features such as UDT operations, module info, and session control. See [Planned Capabilities](../README.md#planned-capabilities) in the top-level README.

Against a real controller (the `pycomm3` path), the write tools, `read_array` and `get_plc_info` do not work yet, and some settings are ignored; writes currently work only against the mock. See [Known limitations](../README.md#known-limitations) in the top-level README.
