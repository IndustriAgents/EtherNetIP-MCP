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
- When `main` moves, the `notify-industriconnect` workflow sends a
  `protocol-mcp-updated` dispatch to IndustriConnect (only once the
  `INDUSTRICONNECT_DISPATCH_TOKEN` secret is set; until then it logs a warning
  and skips). Whether and when the pointer is bumped is decided by
  IndustriConnect's own workflows, not by this repository.

## Repository layout

```text
EtherNetIP-MCP/
├── ethernetip-python/        # the MCP server: pyproject.toml + src/ethernetip_mcp/ + tests/
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
2. **Read-only by default.** Tools that write a tag change a running
   controller. They are refused unless `ENIP_WRITES_ENABLED=true`, and system
   commands such as `set_plc_time`, which also change the controller, need
   both that and `ENIP_SYSTEM_CMDS_ENABLED=true`. A new tool that writes must
   go through the same guard, and the defaults stay off.
3. **A write is sent at most once.** Only opening the connection may be
   retried. Pass `repeatable=False` to `_run_cip`/`_json_exchange` for any
   operation that changes the device, and report a lost reply as
   `outcome: "unknown"`, never as success and never by re-sending.
4. **The mock can answer it.** If you add a tool, the mock has to be able to
   answer it, or nobody can test it without a plant.
5. **Tool names line up with the rest of the suite.** Keep tool and argument
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
ENIP_HOST=127.0.0.1 ENIP_PORT=5025 ENIP_JSON_BRIDGE=true ENIP_WRITES_ENABLED=true uv run ethernetip-mcp
```

Leave out `ENIP_WRITES_ENABLED=true` to see the read-only default.

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

CI (`.github/workflows/ci.yml`) needs no device. Run the same steps before you
push, from the repository root:

```bash
cd ethernetip-python
uv sync --locked --extra dev
(cd ../ethernetip-mock-server && uv sync --locked)   # the integration tests start the mock
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked python -m compileall -q src
uv run --locked python -c "import ethernetip_mcp; from ethernetip_mcp.cli import main"
ENIP_REQUIRE_INTEGRATION=1 uv run --locked pytest -v

cd ../ethernetip-mock-server
uv sync --locked --extra dev
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m compileall -q eip_mock_server.py
uv run --locked python -c "import eip_mock_server"
uv run --locked ethernetip-mock-server --help
```

CI also builds the server in-process and checks that every tool registers with
a description; the exact snippet is in the workflow.

The test suite in `ethernetip-python/tests` has three layers:

- **Unit tests** for configuration, the tools (over an in-memory MCP session)
  and the `pycomm3` path. There is no controller in CI, so
  `tests/fake_pycomm3.py` provides a `LogixDriver` subclass that binds every
  call against the installed `pycomm3` method signatures and rejects anything
  `pycomm3` would reject. Use it for any change to how the client calls
  `pycomm3`.
- **JSON-bridge tests** that start the mock PLC on a free port and talk to it,
  plus small in-process bridges that lose or delay replies, to prove a write
  is never sent twice.
- **Integration tests** that start the mock and drive the real
  `ethernetip-mcp` process over stdio with the MCP client SDK, including a
  check that nothing but JSON-RPC reaches stdout.
- **A docs test** that fails if the README's configuration table misses a
  setting the code reads, or its "Behaviour changes" list misses a tool.

Add a test with every fix or tool change. Then, if you like, drive the server
the way a user would, through the Inspector or a real MCP client, against the
mock. Check the envelope, not just the value.

The JSON bridge (`ENIP_JSON_BRIDGE=true`) exercises the server's tools and the
client's bridge code, but not CIP traffic to a real controller. If your change
touches the `pycomm3` path, say so in the pull request and, if you have one,
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
- `ruff check` and `ruff format --check` must pass; both come with the `dev`
  extra (`uv run --extra dev ruff check src tests`), and CI enforces them in
  both projects.
- Read configuration when the server is built (after `load_dotenv()`), never
  at import time, and reject invalid values with a `ConfigError`.

## Pull requests

Branch off `main` and keep each pull request to one change. Say how you tested
it, and whether it touches anything that can write to a controller. If a change
was AI-assisted, keep the `Co-Authored-By:` trailer.

## Code of Conduct

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
