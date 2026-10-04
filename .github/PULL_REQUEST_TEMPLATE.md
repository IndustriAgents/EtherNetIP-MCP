## Summary

<!-- What does this PR do, and why? -->

## Component(s) affected

- [ ] MCP server (`ethernetip-python`)
- [ ] Mock PLC (`ethernetip-mock-server`)
- [ ] Docs / CI only

## Type of change

- [ ] Bug fix
- [ ] New tool
- [ ] Documentation
- [ ] Refactor / chore

## Does this change what can be written to a controller?

- [ ] No
- [ ] Yes. Describe the new capability and how it is gated (`ENIP_WRITES_ENABLED` / `ENIP_SYSTEM_CMDS_ENABLED`):

## Checklist

- [ ] Tools still return the shared `{ success, data, error, meta }` envelope.
- [ ] The mock PLC can exercise the change, and I tested against it.
- [ ] If the change touches the `pycomm3` path to real controllers, I described the bench I tested it on.
- [ ] Nothing was tested against production equipment.
- [ ] `stdout` is still clean: all logging goes to `stderr`.
- [ ] The CI checks pass locally (see [CONTRIBUTING.md](https://github.com/IndustriAgents/EtherNetIP-MCP/blob/main/CONTRIBUTING.md#testing-a-change)), and `uv.lock` is updated if dependencies changed.
- [ ] I updated the README where relevant.

## How to test

<!-- The exact commands and tool calls a reviewer can run against the mock. -->
