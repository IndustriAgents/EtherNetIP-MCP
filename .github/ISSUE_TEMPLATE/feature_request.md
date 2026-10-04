---
name: Feature request
about: Suggest a new tool or an improvement
title: "[Feature] "
labels: enhancement
assignees: ""
---

**What problem does this solve?**
The use case, from the point of view of someone asking an agent to do something.

**Which component?**
- [ ] MCP server (`ethernetip-python`)
- [ ] Mock PLC (`ethernetip-mock-server`)

**Proposed solution**
If it is a new tool: its name, arguments, and what it returns. This server is
part of the [IndustriConnect](https://github.com/IndustriAgents/IndustriConnect)
suite, whose tool names and arguments are kept consistent across protocols, so
say how it lines up with the equivalent tool in the other servers.

**Does it write to a controller?**
- [ ] No, read-only
- [ ] Yes, it changes tag values or controller state

**Can the mock answer it?**
What the mock PLC would need to be able to simulate for this to be testable
without a real controller.

**Additional context**
Links to the CIP / EtherNet/IP specification, Rockwell documentation, example
controllers, or related issues.
