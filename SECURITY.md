# Security Policy

## What this server is, in security terms

The EtherNet/IP MCP server is a bridge between a language model and a
Rockwell/Allen-Bradley Logix controller. It speaks MCP over stdio to its client
and EtherNet/IP (CIP explicit messaging, through
[`pycomm3`](https://github.com/ottowayi/pycomm3)) to the controller, which means
the trust boundary sits *inside* the server: whatever the model decides to
call, the controller is asked to do.

EtherNet/IP as used here carries no authentication or encryption of its own.
`pycomm3` does not implement CIP Security, so anyone who can reach the
controller's TCP port 44818 can read and write its tags. Whatever protection
there is comes from your network and from the controller's own settings (key
switch position, tag external-access attributes), not from this code.

So treat this server as equipment on the control network, not as an
application on the office network.

## Running it safely

- **Start against the mock.** This repository ships
  [`ethernetip-mock-server`](ethernetip-mock-server/) precisely so that an agent
  can be exercised end to end without touching a real controller. Do that
  first, every time.
- **Keep writes off until you mean otherwise.** Write tools change tag values
  on a running controller, which can move physical equipment. There is no undo,
  and a language model will call a tool it has been given. Like the rest of the
  IndustriConnect suite, this server is **read-only by default**: the write
  tools are refused unless `ENIP_WRITES_ENABLED=true`. Turn it on only for a
  session that needs it, against a controller where that is safe.
- **Leave system commands off.** `set_plc_time` changes the controller, so it
  is refused unless both `ENIP_WRITES_ENABLED=true` and
  `ENIP_SYSTEM_CMDS_ENABLED=true`. Keep it that way unless you need it.
- **Safety switches never come from an implicit `.env`.** Without
  `--env-file`, the server loads only `ethernetip-python/.env` of a source
  checkout, found from the package's own location: never the working
  directory (usually the MCP client's project) or any parent directory, and
  nothing at all for an installed package. That file may set only `ENIP_*`
  and `TAG_MAP_FILE` settings; `ENIP_WRITES_ENABLED`,
  `ENIP_SYSTEM_CMDS_ENABLED` and anything else in it (`PATH`, `PYTHONPATH`,
  `LD_PRELOAD`, …) are ignored with a warning on stderr, whatever their
  letter case. Enable writes in the MCP client's `env`, or in a file you pass
  explicitly with `--env-file`.
- **Every device exchange has a deadline.** `ENIP_DEADLINE` bounds each tool
  call from its start, batches included; nothing is sent once it has passed.
  A write that runs out of time after it may have been sent is reported
  `unknown`, never retried.
- **A cancelled write stays cancelled.** If the MCP client cancels a call or
  disconnects before a write was sent (queued, connecting or waiting to
  retry), the write is never sent afterwards; on disconnect the server stops
  all pending device requests and exits.
- **A write is sent at most once.** If its reply is lost, the server reports
  that the write may have been applied (`meta.outcome: "unknown"`,
  `meta.request_sent: true`) instead of sending it again; read the tag back
  before repeating it.
- **Do not expose the server beyond the host running the client.** It is a
  stdio process meant to run beside the MCP client, not a network service.
- **Keep the mock on loopback.** The mock's JSON bridge has no authentication
  and accepts writes from anyone who can connect. It binds `127.0.0.1` by
  default; do not bind it to a routable address.
- **Segment the network.** The server should sit where a PLC engineering
  workstation would sit, behind whatever separates your control network from
  everything else.
- **Never point it at a safety system.** GuardLogix safety tasks and any other
  safety-instrumented function are out of scope for this repository.

## Supported versions

This project is pre-1.0. Security fixes land on `main`. The
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) suite picks
them up through its `EtherNetIP-Project` submodule.

## Reporting a vulnerability

Please report privately, through
[GitHub private vulnerability reporting](https://github.com/IndustriAgents/EtherNetIP-MCP/security/advisories/new).
If you cannot use GitHub, email hi@industriagents.com instead.

Please do not open a public issue for a vulnerability.

Include the component affected (`ethernetip-python` or
`ethernetip-mock-server`), the commit, what an attacker would gain, and a
reproduction if you have one. We will acknowledge within a week and keep you
updated as we work on a fix.

## Scope

In scope: the MCP server (`ethernetip-python/`) and the mock PLC
(`ethernetip-mock-server/`) in this repository.

Out of scope: vulnerabilities in EtherNet/IP or CIP themselves (the lack of
authentication is a property of the protocol), in third-party libraries such
as `pycomm3` and the MCP SDK (report those upstream), and in vendor controller
firmware.
