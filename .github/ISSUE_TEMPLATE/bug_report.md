---
name: Bug report
about: Report a problem with the EtherNet/IP MCP server or the mock PLC
title: "[Bug] "
labels: bug
assignees: ""
---

**Which component?**
- [ ] MCP server (`ethernetip-python`)
- [ ] Mock PLC (`ethernetip-mock-server`)

**Describe the bug**
What happens, and what you expected instead.

**To reproduce**
The MCP client you used (Claude Desktop / Claude Code / Cursor / Inspector /
IndustriConnect's mcp-manager-ui), the tool you called, and the arguments you
passed.

1.
2.
3.

**Tool output**
```json
paste the { success, data, error, meta } envelope, or the error
```

**What was on the other end?**
- [ ] The mock PLC from this repo (`ENIP_JSON_BRIDGE=true`)
- [ ] A real controller. Family, model and firmware (e.g. ControlLogix 1756-L83E v33, CompactLogix, Micro800):

**Configuration**
The `ENIP_*` variables you set: host, slot or `ENIP_PATH` route, and whether
writes or system commands are enabled. Leave out anything sensitive.

**Environment**
- OS:
- Python version:
- Commit SHA:

**Additional context**
Tag names and data types, stderr logs, whatever would let someone else
reproduce it against the mock.
