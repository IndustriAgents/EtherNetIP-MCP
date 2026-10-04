# ethernetip-mock-server

Developer-focused mock PLC that emulates a handful of EtherNet/IP tags, arrays, and status bits. It does **not** implement the CIP protocol; it exposes an in-memory tag database over a simple JSON-over-TCP bridge so you can exercise the [MCP server](../ethernetip-python/) without a Rockwell controller.

## Usage

Run these from this directory (`ethernetip-mock-server/`):

```bash
uv sync
uv run ethernetip-mock-server
```

By default the mock listens on `127.0.0.1:5025` for newline-delimited JSON requests:

```json
{ "op": "read", "tag": "Program:MainProgram.MotorSpeed" }
{ "op": "write", "tag": "Program:MainProgram.MotorSpeed", "value": 1200.0 }
{ "op": "list" }
```

Each request gets one JSON line back, `{ "success": true, "data": ... }` or `{ "success": false, "error": "..." }`.

Use `--help` for the configuration flags: `--host`, `--port`, `--update-interval` (seconds between simulated value updates) and `--verbose`. The same settings can come from `MOCK_ENIP_HOST`, `MOCK_ENIP_PORT`, `MOCK_ENIP_UPDATE_INTERVAL` and `MOCK_ENIP_VERBOSE`.

## Tags

| Tag | Type | Simulated |
|---|---|---|
| `Program:MainProgram.MotorSpeed` | `REAL` | yes |
| `Program:MainProgram.MotorTorque` | `REAL` | yes |
| `Program:MainProgram.Conveyor_Status.Running` | `BOOL` | yes |
| `Program:MainProgram.Tank_Levels` | `REAL[3]` | yes |
| `Program:MainProgram.Alarm_Message` | `STRING` | no |

Simulated tags are overwritten with random values every update interval, so a value written to one of them only lasts until the next update.

## Connecting the MCP server

Point the MCP server at the mock and switch it to the JSON bridge:

```bash
ENIP_HOST=127.0.0.1 ENIP_PORT=5025 ENIP_JSON_BRIDGE=true uv run ethernetip-mcp   # from ../ethernetip-python
```

In JSON-bridge mode the server sends tag reads, writes and the tag list to the mock. `get_plc_time` answers with the host's clock and `set_plc_time` changes nothing. `get_plc_info` has no JSON-bridge path and fails against the mock.

> **Note:** This is a scaffold meant for rapid development; the CIP front-end is still a TODO. Extend `eip_mock_server.py` to translate between the JSON protocol and a true EtherNet/IP stack or to pipe data into higher-level tests.
