# Contributing to EtherNet/IP MCP

Thanks for taking an interest. This repository is an MCP server for
Rockwell/Allen-Bradley Logix controllers over EtherNet/IP, plus a mock PLC so
that every change can be tested without a real controller.

## Where changes go

This server is also shipped inside the
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) suite, as
the `EtherNetIP-Project` git submodule. That folder is a pointer to this
repository's `main`, not a copy, so **changes land here** and not in
IndustriConnect:

- Open issues and pull requests against
  [IndustriAgents/EtherNetIP-MCP](https://github.com/IndustriAgents/EtherNetIP-MCP).
- IndustriConnect cannot take edits to files under `EtherNetIP-Project/`; it
  only records which commit of this repository it uses.
- When `main` moves, the `notify-industriconnect` workflow asks IndustriConnect
  to bump that pointer. IndustriConnect also checks for a new `main` on a
  nightly schedule.

## Repository layout

```text
EtherNetIP-MCP/
├── ethernetip-python/        # the MCP server: pyproject.toml + src/ethernetip_mcp/
├── ethernetip-mock-server/   # a simulated PLC, so nothing is rehearsed on live plant
└── README.md                 # quickstart, configuration and tool list
```

Each of the two folders is its own [`uv`](https://docs.astral.sh/uv/) project
with a committed `uv.lock`.

## What holds the suite together

The value of IndustriConnect is that all of its servers behave the same way. A
change that breaks one of these needs a reason in the pull request:

1. **One response envelope.** Every tool returns `{ success, data, error, meta }`.
   A client that can read one IndustriConnect server's output can read all of
   them.
2. **Writes are gated.** Tools that write a tag change a running controller.
   They are refused when `ENIP_WRITES_ENABLED=false`, and system commands such
   as `set_plc_time` are refused unless `ENIP_SYSTEM_CMDS_ENABLED=true`. A new
   tool that writes must go through the same guard.
3. **The mock can answer it.** If you add a tool, the mock has to be able to
   answer it, or nobody can test it without a plant.
4. **Tool names line up with the rest of the suite.** Keep tool and argument
   names consistent with the equivalent tools in the other IndustriConnect
   servers.

## Getting set up

You need Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). Start the mock
first, since it is the thing the server talks to:

```bash
cd ethernetip-mock-server
uv sync
uv run ethernetip-mock-server        # listens on 127.0.0.1:5025
```

Then, in a second terminal from the repository root, the server, pointed at the
mock through its JSON bridge:

```bash
cd ethernetip-python
uv sync --extra dev
ENIP_HOST=127.0.0.1 ENIP_PORT=5025 ENIP_JSON_BRIDGE=true uv run ethernetip-mcp
```

The server speaks MCP over stdio, so started like this it waits for a client on
stdin. To drive it by hand, use the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) from the
repository root:

```bash
npx @modelcontextprotocol/inspector \
  -e ENIP_HOST=127.0.0.1 -e ENIP_PORT=5025 -e ENIP_JSON_BRIDGE=true \
  uv --directory ethernetip-python run ethernetip-mcp
```

## Testing a change

There is no automated test suite yet. CI (`.github/workflows/ci.yml`) runs
cheap checks that need no device. Run them before you push, from the
repository root:

```bash
cd ethernetip-python
uv sync --locked --extra dev
uv run --locked python -m compileall -q src
uv run --locked python -c "import ethernetip_mcp; from ethernetip_mcp.cli import main"

cd ../ethernetip-mock-server
uv sync --locked
uv run --locked python -m compileall -q eip_mock_server.py
uv run --locked python -c "import eip_mock_server"
uv run --locked ethernetip-mock-server --help
```

CI also builds the server in-process and checks that its tools register; the
exact snippet is in the workflow.

Then drive the server the way a user would, through the Inspector or a real MCP
client, against the mock. Call the tool you changed, and check the envelope,
not just the value.

The JSON bridge (`ENIP_JSON_BRIDGE=true`) exercises the server's tools and the
client's bridge code, but not the `pycomm3` code path that talks CIP to a real
controller. If your change touches that path, say so in the pull request and
describe the bench you verified it on: controller family, firmware, and slot or
route.

**Do not test against production equipment.**

If you add dependencies, update the lockfile in the same pull request
(`uv lock` in the project you changed). CI installs with `--locked` and fails if
`uv.lock` is out of date.

## House rules for the code

- MCP speaks over **stdio**, so `stdout` belongs to the protocol. All logging
  goes to `stderr`. A stray `print()` corrupts the session.
- An error is a returned `{ success: false, error }`, not an exception that
  kills the server. The model needs to be told what went wrong.
- Fail at startup, not at the first tool call, when configuration is unusable.
- Keep tool names and argument names aligned with the other IndustriConnect
  servers.
- `ruff` is available through the `dev` extra (`uv run ruff check src`). The
  existing code is not yet clean under it, so CI does not enforce it, but new
  code should not add findings.

## Pull requests

Branch off `main` and keep each pull request to one change. Say how you tested
it, and whether it touches anything that can write to a controller. If a change
was AI-assisted, keep the `Co-Authored-By:` trailer.

## Code of Conduct

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
