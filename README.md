# EtherNet/IP MCP Server

[![CI](https://github.com/IndustriAgents/EtherNetIP-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/IndustriAgents/EtherNetIP-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)

MCP-based tooling for Rockwell/Allen-Bradley Logix controllers (ControlLogix, CompactLogix, Micro800) over EtherNet/IP, built on `pycomm3`'s `LogixDriver`. It follows the same layout as the other [IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) protocol servers: a Python MCP server plus a mock PLC, so AI agents can be exercised against simulated controller tags before they go anywhere near real equipment.

The MCP server exposes consistent tool names and the canonical `{ success, data, error, meta }` response envelope, while the mock PLC provides an in-memory tag database for offline testing. The server is **read-only by default**: write tools are refused until you set `ENIP_WRITES_ENABLED=true`.

> **Status: early.** CI runs `ruff`, unit tests and end-to-end tests: the tests start the mock PLC and drive the real `ethernetip-mcp` process over stdio with the MCP client SDK. The `pycomm3` path to real controllers is unit-tested against a fake driver that enforces `pycomm3`'s real method signatures, and its port, timeout and retry handling are tested with the real `LogixDriver` against a local socket. It has **not** been run against a physical controller from this repository yet. Validate on a bench controller before relying on it; see [Known limitations](#known-limitations).

## Repository Layout

```
EtherNetIP-MCP/               # included in IndustriConnect as the EtherNetIP-Project/ submodule
├── README.md
├── LICENSE
├── CONTRIBUTING.md · SECURITY.md · CODE_OF_CONDUCT.md
├── .github/                  # CI, Dependabot, issue and PR templates
├── ethernetip-python/        # Python MCP server (uv), tests in ethernetip-python/tests
└── ethernetip-mock-server/   # EtherNet/IP mock PLC (uv, JSON-over-TCP)
```

## Components

- **[ethernetip-python](ethernetip-python/)**: Python 3.11+ server built with `pycomm3`, `mcp[cli]` (1.x) and `uv`. Ships the `ethernetip-mcp` entry point.
- **[ethernetip-mock-server](ethernetip-mock-server/)**: Python mock PLC with a small in-memory tag database (two controller-scoped tags and five `Program:MainProgram` tags: scalars, an array and a string). It speaks a simple JSON-over-TCP protocol rather than CIP, which the server reaches through its JSON bridge (`ENIP_JSON_BRIDGE=true`).

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

Install the server once, so the MCP client does not have to create its virtual environment (and possibly download Python) inside its startup timeout:

```bash
cd ethernetip-python
uv sync
```

Then register the server with your MCP client. It speaks MCP over stdio; this config points it at the mock through the JSON bridge:

```json
{
  "mcpServers": {
    "ethernetip": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/EtherNetIP-MCP/ethernetip-python", "run", "ethernetip-mcp"],
      "env": {
        "ENIP_HOST": "127.0.0.1",
        "ENIP_PORT": "5025",
        "ENIP_JSON_BRIDGE": "true"
      }
    }
  }
}
```

GUI clients such as Claude Desktop often start without `~/.local/bin` on their `PATH`. If the client cannot find `uv`, put the absolute path printed by `which uv` in `command`.

This config is read-only: the write tools answer `success: false` until you add `"ENIP_WRITES_ENABLED": "true"`. To try it without a client, use the [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector); see [CONTRIBUTING.md](CONTRIBUTING.md#getting-set-up).

For a real controller, drop `ENIP_JSON_BRIDGE` and `ENIP_PORT`, and set `ENIP_HOST` to the controller (or the Ethernet module in its chassis) and `ENIP_SLOT` to the processor slot; for a Micro800 set `ENIP_MICRO800=true` instead of a slot. Read [SECURITY.md](SECURITY.md) and [Known limitations](#known-limitations) first.

## Configuration

Set these in the client config's `env`, in the environment, or in a `.env` file in `ethernetip-python/` (git-ignored). The server reads them when it starts, after loading `.env`; variables already set in the environment win over `.env`. An invalid value (a port that is not a number, `ENIP_WRITES_ENABLED=ture`, conflicting settings) stops the server at startup with a `configuration error` message on stderr and exit code 2. An empty value counts as unset.

| Variable | Default | Meaning |
|---|---|---|
| `ENIP_HOST` | `127.0.0.1` | Controller IP address, or the mock's host. May include a port (`10.0.0.5:44819`). |
| `ENIP_PORT` | `44818` | TCP port, for both backends: set it to the mock's port (`5025`) for the JSON bridge. A port written into `ENIP_HOST` or `ENIP_PATH` also works; if both are given they must agree. |
| `ENIP_SLOT` | `0` | Processor slot in the chassis (0–255). Not used with `ENIP_PATH` or `ENIP_MICRO800`. |
| `ENIP_PATH` | unset | Full CIP route (e.g. `10.0.0.5/backplane/2`); overrides `ENIP_HOST` and `ENIP_SLOT`. Not allowed with the JSON bridge. |
| `ENIP_MICRO800` | `false` | The target is a Micro800: connect to the IP alone (a non-zero `ENIP_SLOT` is a configuration error), and refuse the connection if the controller does not identify as a Micro800 (catalog `2080-*`). `pycomm3` detects a Micro800 on its own either way; `get_connection_status` shows what it detected. |
| `ENIP_TIMEOUT` | `5` | Seconds. On the CIP path, the socket timeout for connecting and for each reply (`pycomm3`'s own default is also 5 s). On the JSON bridge, the limit for each request. |
| `ENIP_JSON_BRIDGE` | `false` | Talk to the mock's JSON-over-TCP protocol instead of CIP. |
| `ENIP_MAX_RETRIES` | `3` | Retries (0–10) for connection-level failures: a call makes at most this + 1 attempts. Errors the device reports (unknown tag, wrong type) are not retried. |
| `ENIP_RETRY_BACKOFF_BASE` | `0.5` | Base delay of the retry backoff, in seconds: retry *n* waits this × 2<sup>n−1</sup>. |
| `ENIP_WRITES_ENABLED` | `false` | Allows the write tools. Off by default: a model will call a tool it has been given. |
| `ENIP_SYSTEM_CMDS_ENABLED` | `false` | Allows `set_plc_time`. |
| `ENIP_DEBUG` | `false` | Debug logging, including `pycomm3`'s, on stderr. |
| `TAG_MAP_FILE` | unset | JSON file of tag aliases with optional scaling, used by `list_tags` and the `*_by_alias` tools. |

The server connects once at startup. If the controller does not answer, it logs a warning on stderr and starts anyway; each tool call then reconnects and reports its own error, so the model sees `success: false` instead of a failed server.

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

`read_tag_by_alias` returns both `raw_value` and the scaled `value`; `write_tag_by_alias` takes engineering units and rounds the raw value for integer types (`SINT` to `ULINT`). A missing or malformed tag map file, a scaling with a zero span, or a non-numeric value on a scaled alias is reported as `success: false`.

## Tools

All tools return `{ success, data, error, meta }`. `success` is `true` only when the device confirmed the operation; `meta` carries the backend, the number of attempts and the duration.

- **Tags:** `read_tag`, `write_tag`, `read_array`, `write_array`, `read_string`, `write_string`, `read_multiple_tags`, `write_multiple_tags`
- **Discovery:** `get_tag_list` (no `program`: controller-scoped tags; `"*"`: all tags; a program name: that program's tags)
- **Tag map aliases:** `list_tags`, `read_tag_by_alias`, `write_tag_by_alias`
- **PLC info and time:** `get_plc_info`, `get_plc_time`, `set_plc_time`
- **Health:** `ping` (asks the device for its identity), `get_connection_status` (last known state, no I/O)

Some details:

- A read returns `data: {tag, value, data_type}`. Array elements follow `pycomm3`: `read_array` (or `read_tag` with `count`, or the `Tag{N}` syntax) returns a list of N elements; reading an array tag without a count returns one element.
- A list written with `write_tag` or `write_array` goes to consecutive elements (`Tag{N}`). Pass an object to write a structure.
- `read_multiple_tags` and `write_multiple_tags` return one `{tag, value, data_type, error}` entry per tag; if any entry failed, `success` is `false` and `data.results` shows which. Batch writes are not atomic.
- Bad arguments (a missing tag name, `elements: 0`, an empty list, a duplicate tag in a batch) come back as `success: false` with `Invalid arguments for <tool>: ...`, not as a protocol error.
- `get_plc_time` returns `data: {plc_time, microseconds}`; `set_plc_time` sets the controller clock to this host's current time.

## Known limitations

- **Not yet run against a physical controller.** The `pycomm3` calls are checked against `pycomm3`'s real signatures and behaviour as read from its source (1.2.14), but no CIP traffic has been exchanged with a Logix controller from this repository. Bench-test before relying on it.
- **`data_type` is not enforced on the CIP path.** `pycomm3` always encodes a write with the controller's own type for the tag; it has no data type argument. If the `data_type` you pass (or the tag map's) differs, the write still uses the controller's type and the response carries a `meta.warning` (or a per-entry `warning` in batches). The mock refuses a mismatched `data_type`.
- **`ENIP_TIMEOUT` depends on a `pycomm3` internal.** `pycomm3` 1.2.14 has no setting for its socket timeout (its `socket_timeout` setter writes a misspelled key), so the server sets `_cfg["socket_timeout"]` before connecting. The dependency is pinned to `pycomm3>=1.2.14,<1.3`; a test fails if that changes.
- **A failing call can take a while.** With the defaults, an unreachable controller that drops packets costs up to 4 attempts × `ENIP_TIMEOUT` (5 s) plus 3.5 s of backoff, about 24 s, before the tool answers; a refused connection fails within the 3.5 s of backoff. Lower `ENIP_MAX_RETRIES` (for example `0` against the mock) for faster answers.
- **The mock is not a controller.** It speaks JSON over TCP, not CIP, and has no structures (UDTs), no array element indexing (`Tag[1]`), and a single program. See [its README](ethernetip-mock-server/README.md).
- **mcp 1.x only.** The server uses `mcp.server.fastmcp`, which mcp 2.x renamed, so `mcp` is pinned to `<2`.

### Behaviour changes in this version

If you used an earlier commit: writes are now off by default (`ENIP_WRITES_ENABLED=false`); `ENIP_TIMEOUT` defaults to 5 s and now takes effect, as do `ENIP_PORT` and `ENIP_MICRO800` on the CIP path; `ENIP_INIT_INFO`, `ENIP_CACHE_TAG_LIST` and `ENIP_CACHE_TIMEOUT`, which never did anything, are no longer read; invalid settings stop the server at startup; read results are `data: {tag, value, data_type}` instead of `data: {tag, result: {...}}`; `get_plc_time` returns `{plc_time, microseconds}` instead of a nested `plc_time` object; `ping` now contacts the device and fails if it does not answer; and against the mock, `get_plc_info` and the clock tools now work, `get_tag_list` without `program` lists only controller-scoped tags, and reading an array tag without a count returns one element, as on a controller.

## Planned Capabilities

- Tag operations: structures (UDTs) in the mock, module info
- Tag discovery: UDT definitions, module inventory
- Session control (`forward_open`, `forward_close`)
- A CIP front end for the mock PLC

## Part of IndustriConnect

This server is one of the protocol servers in [IndustriConnect](https://github.com/IndustriAgents/IndustriConnect), a suite of MCP servers for industrial protocols (Modbus, MQTT/Sparkplug B, OPC UA, BACnet, DNP3, EtherCAT, EtherNet/IP, PROFIBUS, PROFINET and S7comm) that share one response envelope and consistent tool naming. The suite includes this repository as the `EtherNetIP-Project` git submodule, tracking `main`. Development happens here: open issues and pull requests against this repository, not IndustriConnect.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). To report a vulnerability, follow [SECURITY.md](SECURITY.md), not a public issue. Everyone taking part agrees to the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT. See [LICENSE](LICENSE).
