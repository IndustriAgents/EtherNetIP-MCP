# ethernetip-mock-server

Developer-focused mock PLC that emulates a handful of Logix tags, an array and a string. It does **not** implement the CIP protocol; it exposes an in-memory tag database over a simple JSON-over-TCP bridge so you can exercise the [MCP server](../ethernetip-python/) without a Rockwell controller.

## Usage

Run these from this directory (`ethernetip-mock-server/`):

```bash
uv sync
uv run ethernetip-mock-server
```

By default the mock listens on `127.0.0.1:5025` for newline-delimited JSON requests. Each request gets one JSON line back, `{ "success": true, "data": ... }` or `{ "success": false, "error": "..." }`.

| Request | Answer (`data`) |
|---|---|
| `{"op": "read", "tag": "...", "count": N}` | `{tag, value, data_type}`. `count` is optional. Like `pycomm3`, an array tag read without a count returns its first element; with a count of 2 or more, a list and a type such as `REAL[2]`. |
| `{"op": "write", "tag": "...", "value": ..., "data_type": "..."}` | `{tag, value, data_type}` of what was written. `data_type` is optional and must match the tag's type. The value is type-checked (`REAL` takes a number, `DINT` an integer in range, `BOOL` true/false or 1/0, `STRING` up to 82 characters). A list writes the leading elements of an array. |
| `{"op": "list", "program": "..."}` | One `{tag, data_type, dimensions, tag_type, alias, external_access, description, value}` per tag, the same keys the server reports for a real controller (`data_type` is the element type, `dimensions` the array size). Like `pycomm3`'s `get_tag_list`: no `program` lists controller-scoped tags, `"*"` lists all, `"MainProgram"` lists that program's tags. |
| `{"op": "info"}` | The mock's identity: `name`, `vendor` (`IndustriAgents (mock)`), `product_name` (`ethernetip-mock-server`), `revision`, `serial`, `keyswitch`. |
| `{"op": "get_time"}` | `{microseconds}`: the mock's clock, µs since 1970-01-01. |
| `{"op": "set_time", "microseconds": N}` | Sets the mock's clock (kept as an offset from the host clock until the mock restarts). |

An unknown tag answers `{"success": false, "error": "Unknown tag 'Name'"}`.

Use `--help` for the configuration flags: `--host`, `--port`, `--update-interval` (seconds between simulated value updates) and `--verbose` (log each connection). The same settings can come from `MOCK_ENIP_HOST`, `MOCK_ENIP_PORT`, `MOCK_ENIP_UPDATE_INTERVAL` and `MOCK_ENIP_VERBOSE`, in the environment or in a `.env` file; they are read when the mock starts.

## Tags

| Tag | Type | Simulated |
|---|---|---|
| `Line_Speed` | `REAL` | no |
| `Batch_Count` | `DINT` | no |
| `Program:MainProgram.MotorSpeed` | `REAL` | yes |
| `Program:MainProgram.MotorTorque` | `REAL` | yes |
| `Program:MainProgram.Conveyor_Status.Running` | `BOOL` | yes |
| `Program:MainProgram.Tank_Levels` | `REAL[3]` | yes |
| `Program:MainProgram.Alarm_Message` | `STRING` | no |

Simulated tags are overwritten with random values every update interval, so a value written to one of them only lasts until the next update. Use `--update-interval 3600` to keep them still while testing.

What the mock does not have: CIP, structures (UDTs), array element indexing (`Tank_Levels[1]` is an unknown tag), and more than one program.

## Connecting the MCP server

Point the MCP server at the mock and switch it to the JSON bridge:

```bash
ENIP_HOST=127.0.0.1 ENIP_PORT=5025 ENIP_JSON_BRIDGE=true uv run ethernetip-mcp   # from ../ethernetip-python
```

In JSON-bridge mode every tool reaches the mock: tag reads and writes, the tag list, `get_plc_info` (the mock's identity), `get_plc_time` and `set_plc_time` (the mock's clock), and `ping`. Writes still need `ENIP_WRITES_ENABLED=true`, and `set_plc_time` needs both that and `ENIP_SYSTEM_CMDS_ENABLED=true`.

## Development

```bash
uv sync --extra dev
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
```

The server's test suite (`../ethernetip-python/tests`) starts this mock and exercises its protocol.
