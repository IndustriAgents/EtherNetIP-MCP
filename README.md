# EtherNet/IP MCP Server

[![CI](https://github.com/IndustriAgents/EtherNetIP-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/IndustriAgents/EtherNetIP-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)

MCP-based tooling for Rockwell/Allen-Bradley Logix controllers (ControlLogix, CompactLogix) over EtherNet/IP, built on `pycomm3`'s `LogixDriver`. It follows the same layout as the other [IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) protocol servers: a Python MCP server plus a mock PLC, so AI agents can be exercised against simulated controller tags before they go anywhere near real equipment.

The MCP server exposes consistent tool names and the canonical `{ success, data, error, meta }` response envelope, while the mock PLC provides an in-memory tag database for offline testing.

> **Status: early.** The repository contains complete scaffolding (project metadata, entry points, client wrappers and a first set of tools) so remaining work can focus on feature completion. CI byte-compiles, imports and checks tool registration; the mock-backed path is exercised by hand (see [CONTRIBUTING.md](CONTRIBUTING.md#testing-a-change)). The `pycomm3` path to real controllers has known gaps: the write tools, `read_array` and `get_plc_info` do not work on it yet, so writes currently work only against the mock. See [Known limitations](#known-limitations), and validate on a bench controller before relying on it.

## Repository Layout

```
EtherNetIP-MCP/               # included in IndustriConnect as the EtherNetIP-Project/ submodule
├── README.md
├── LICENSE
├── CONTRIBUTING.md · SECURITY.md · CODE_OF_CONDUCT.md
├── .github/                  # CI, Dependabot, issue and PR templates
├── ethernetip-python/        # Python MCP server (uv)
└── ethernetip-mock-server/   # EtherNet/IP mock PLC (uv, JSON-over-TCP)
```

## Components

- **[ethernetip-python](ethernetip-python/)**: Python 3.11+ server built with `pycomm3`, `mcp[cli]`, and `uv`. Ships the `ethernetip-mcp` entry point.
- **[ethernetip-mock-server](ethernetip-mock-server/)**: Python mock PLC with a small in-memory tag database (controller-style `Program:` tags: scalars, an array and a string). It speaks a simple JSON-over-TCP protocol rather than CIP, which the server reaches through its JSON bridge (`ENIP_JSON_BRIDGE=true`).

## Install

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/IndustriAgents/EtherNetIP-MCP.git
cd EtherNetIP-MCP
```

## Quick Start (against the mock)

Start the mock PLC (listens on `127.0.0.1:5025`):

```bash
cd ethernetip-mock-server
uv sync
uv run ethernetip-mock-server
```

Then register the server with your MCP client. It speaks MCP over stdio; this config points it at the mock through the JSON bridge, read-only:

```json
{
  "mcpServers": {
    "ethernetip": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/EtherNetIP-MCP/ethernetip-python", "run", "ethernetip-mcp"],
      "env": {
        "ENIP_HOST": "127.0.0.1",
        "ENIP_PORT": "5025",
        "ENIP_JSON_BRIDGE": "true",
        "ENIP_WRITES_ENABLED": "false"
      }
    }
  }
}
```

To try it without a client, use the [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector); see [CONTRIBUTING.md](CONTRIBUTING.md#getting-set-up).

For a real controller, drop `ENIP_JSON_BRIDGE` and `ENIP_PORT`, and set `ENIP_HOST` to the controller (or the Ethernet module in its chassis) and `ENIP_SLOT` to the processor slot. Read [SECURITY.md](SECURITY.md) first, and [Known limitations](#known-limitations): writes do not work on that path yet.

## Configuration

Set these in the client config's `env`, in the environment, or in a `.env` file in `ethernetip-python/` (git-ignored).

| Variable | Default | Meaning |
|---|---|---|
| `ENIP_HOST` | `127.0.0.1` | Controller IP address, or the mock's host. |
| `ENIP_SLOT` | `0` | Processor slot in the chassis. |
| `ENIP_PATH` | unset | Full CIP route (e.g. `10.0.0.5/backplane/2`); overrides host and slot. |
| `ENIP_PORT` | `44818` | Port for the JSON bridge: set it to the mock's port (`5025`). The CIP path ignores it and uses 44818. |
| `ENIP_TIMEOUT` | `10` | Meant as the connection timeout in seconds. Currently ignored (see [Known limitations](#known-limitations)). |
| `ENIP_JSON_BRIDGE` | `false` | Talk to the mock's JSON-over-TCP protocol instead of CIP. |
| `ENIP_MAX_RETRIES` | `3` | Retries per operation, with exponential backoff. |
| `ENIP_RETRY_BACKOFF_BASE` | `0.5` | Base delay of that backoff, in seconds: retry *n* waits this × 2<sup>n−1</sup>. |
| `ENIP_WRITES_ENABLED` | `true` | **Writes are on by default.** Set `false` for a read-only server. |
| `ENIP_SYSTEM_CMDS_ENABLED` | `false` | Allows `set_plc_time`. |
| `TAG_MAP_FILE` | unset | JSON file of tag aliases with optional scaling, used by `list_tags` and the `*_by_alias` tools. |

A tag map maps an alias to a controller tag, optionally with linear scaling between raw and engineering units:

```json
{
  "motor_speed": {
    "tag": "Program:MainProgram.MotorSpeed",
    "data_type": "REAL",
    "description": "Motor speed",
    "scaling": { "raw_min": 0, "raw_max": 1800, "eng_min": 0, "eng_max": 100 }
  }
}
```

## Tools

All tools return `{ success, data, error, meta }`.

- **Tags:** `read_tag`, `write_tag`, `read_array`, `write_array`, `read_string`, `write_string`, `read_multiple_tags`, `write_multiple_tags`
- **Discovery:** `get_tag_list`
- **Tag map aliases:** `list_tags`, `read_tag_by_alias`, `write_tag_by_alias`
- **PLC info and time:** `get_plc_info`, `get_plc_time`, `set_plc_time`
- **Health:** `ping`, `get_connection_status`

Against a real controller, the write tools, `read_array` and `get_plc_info` do not work yet; see [Known limitations](#known-limitations).

## Known limitations

These are open bugs in the `pycomm3` path, the one that talks CIP to a real controller. The JSON bridge to the mock is not affected unless stated.

- **Writes fail.** `write_tag`, `write_array`, `write_string`, `write_tag_by_alias` and `write_multiple_tags` call `LogixDriver.write()` with arguments it does not accept, so each one fails with a `TypeError`, after retrying and reconnecting `ENIP_MAX_RETRIES` times. **Writes currently work only against the mock.**
- **Array reads fail.** `read_array`, and `read_tag` when given `count`, pass a `count=` argument that `LogixDriver.read()` does not accept, and fail with a `TypeError`.
- **`get_plc_info` returns empty fields.** It reads `LogixDriver.info`, a dict, as if it were an object, so `name`, `revision`, `serial`, `product_code` and `firmware` all come back `null`. Against the mock it fails outright, as it has no JSON-bridge path.
- **Some settings are ignored.** `ENIP_PORT`, `ENIP_TIMEOUT` and `ENIP_MICRO800` never reach `pycomm3`, which always connects to port 44818 with its own 10 s timeout and detects a Micro800 from the controller's identity. `ENIP_MICRO800` only stops `ENIP_SLOT` from being added to the route, and `ENIP_TIMEOUT` does not apply to the JSON bridge either. `ENIP_INIT_INFO`, `ENIP_CACHE_TAG_LIST`, `ENIP_CACHE_TIMEOUT` and `ENIP_DEBUG` are read but change nothing.

The other tools that reach the controller (`read_tag` without `count`, `read_string`, `read_multiple_tags`, `get_tag_list`, `read_tag_by_alias`, `get_plc_time` and `set_plc_time`) call `pycomm3` with arguments its API accepts, but none of them has been run against a controller from this repository yet.

## Planned Capabilities

- Tag operations: scalar, arrays, strings, structures, batch read/write
- Tag discovery: controller/program tag lists, UDT definitions, module info
- PLC info: controller identity, firmware, time, module inventory
- Tag map aliases with scaling metadata
- Health + session control (`ping`, `get_connection_status`, `forward_open`, `forward_close`)
- Mock PLC with deterministic tag database for offline testing

## Part of IndustriConnect

This server is one of the protocol servers in [IndustriConnect](https://github.com/IndustriAgents/IndustriConnect), a suite of MCP servers for industrial protocols (Modbus, MQTT/Sparkplug B, OPC UA, BACnet, DNP3, EtherCAT, EtherNet/IP, PROFIBUS, PROFINET and S7comm) that share one response envelope and consistent tool naming. The suite includes this repository as the `EtherNetIP-Project` git submodule, tracking `main`. Development happens here: open issues and pull requests against this repository, not IndustriConnect.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). To report a vulnerability, follow [SECURITY.md](SECURITY.md), not a public issue. Everyone taking part agrees to the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT. See [LICENSE](LICENSE).
