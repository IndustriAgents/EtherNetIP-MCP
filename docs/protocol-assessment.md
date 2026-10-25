# EtherNet/IP protocol assessment and capability decisions

Reviewed 2026-10-25 against this repository, OPCUA-MCP's feature/execution design,
and the locked pycomm3 1.2.14 implementation. This is an implementation assessment,
not ODVA conformance certification.

## Evidence and supported scope

[ODVA's EtherNet/IP overview](https://www.odva.org/technology-standards/key-technologies/ethernet-ip/)
identifies EtherNet/IP as a CIP-based industrial network. The full subscribed CIP
specification was not available for this assessment; we make no certification
claim. The current backend supports the selected Logix services, not every CIP
object or EtherNet/IP device role.

[pycomm3 1.2.14 LogixDriver API](https://pycomm3.readthedocs.io/en/stable/api_reference/logix_driver.html)
documents routed connection paths, tag initialization, tag reads/writes and PLC
time services. Its [Logix usage guide](https://github.com/ottowayi/pycomm3/blob/master/docs/usage/logixdriver.rst)
describes arrays, program-scoped names, structures, and automatic multiple-service
and fragmented requests. Our adapter checks the requested array length and batch
result cardinality before presenting data to an agent. No atomic snapshot or
transaction is inferred from a batched API call.

The [driver lifecycle guidance](https://pycomm3.readthedocs.io/en/stable/getting_started.html)
places connection maintenance and close responsibility on the application. This
server owns a serialized native session, checks idle sessions before controls,
and does not replay controls after possible dispatch. Library-supported services
remain subject to controller permissions, firmware and tag availability.

## Capability and gap matrix

| Workflow / tools | Existing capability | Gap addressed / acceptance evidence | Remaining boundary |
|---|---|---|---|
| Inspect: `get_connection_status` | Offline connection/configuration report | Typed `capabilities` reports every canonical tool, backend and native gates; `test_capabilities.py`, installed smoke | Static software support is not a device probe or policy authorization |
| Read: `read_tags` tag/batch/alias | Native Logix reads and configured engineering aliases | Reject wrong array lengths; preserve one failure slot per requested tag when batch counts cannot be correlated; `test_read_integrity.py` | No fabricated quality, server timestamps or process guarantees |
| Metadata: `get_tag_list`, `list_tags` | Controller metadata and configured aliases | Bounded snapshot pagination/filtering and cursor lifecycle; `test_tag_browse.py` | Native metadata upload allocation remains outside MCP page limits |
| Controls: `write_tags`, `set_plc_time` | Native gates, input prechecks, explicit outcomes, no replay | Cancellation ownership is fenced; native recovery occurs before session handoff; `test_session_ownership.py`, existing reply-loss/cancellation suites | Multiple writes may partially apply; time setting also needs system gate |
| Diagnostics: `get_plc_info`, `get_plc_time` | Identity and wall-clock access | Preserved and exercised by adapter/tool suites | PLC service support varies |
| Implicit cyclic I/O, arbitrary CIP, CIP Security | Not implemented | Explicit capability limitations | Separate controller/stack design and device validation required; not silently enabled |

The useful agent sequence is offline support inspection, controller identity/tag
metadata, bounded reads, an explicitly enabled write, then inspection of per-item
outcomes. A successful protocol write is not proof of physical process completion.
An unknown result must not trigger automatic replay.

## Architecture and migration

The existing MCP/application/domain/adapter split remains. Capabilities are pure
domain values and do not import the native driver or create connections. Status
output is additive and described in the contract. Existing names and aliases
remain callable. The JSON bridge reports `simulated: true`; its success is not
CIP wire interoperability evidence. `enabled` means native configuration only;
MCP policy and controller access are evaluated separately.

A call's holding-session snapshot is not enough to authorize interruption. The
call guard now spans ownership verification and socket interruption, so a stale
cancel cannot close a later caller's socket. Error recovery also runs before
releasing the owning session. No generic retry policy was added.

## Verification boundary

Tests use a signature-checked pycomm3 fake, localhost TCP peers, the JSON bridge,
real MCP process transport, and installed wheel/npm artifacts. Actual Logix
hardware, routed gateways and ODVA conformance equipment were not provided.
Hardware interoperability remains unverified and requires an authorized test PLC,
its firmware/access configuration and a test network. No production network was
scanned and no physical controller was changed.

All canonical tools now declare structural result shapes in `contract/tools.json`,
including typed outcome/completeness metadata and nullable failure data. Native
values retain explicit JSON value flexibility. CI validates live canonical MCP
responses against those schemas and checks text/structured result agreement;
legacy aliases retain their compatibility payload shapes.

## Integer precision migration

At the MCP wire boundary, integers outside `[-9007199254740991, 9007199254740991]`
are now decimal strings, including nested values. Safe integers remain JSON numbers
and booleans remain booleans. `meta.value_encoding: decimal_string` and
`meta.encoded_integer_paths` identify converted fields using JSON pointers. Internal
application arithmetic and native codecs retain exact integers before presentation.
Consumers should use an integer/decimal parser, not a JavaScript Number conversion,
for these strings. Write inputs retain their existing type requirements; do not
coerce a returned decimal string through a floating-point number before writing.
Modbus clients can use the preserved raw 16-bit register representation for exact
large-integer writes. This change prevents silent rounding in JSON consumers and
is an intentional output compatibility change for unsafe large integers.

## Second improvement pass: bounded controller metadata browsing

`get_tag_list` now accepts `limit` (default 100, maximum 500), a literal,
case-sensitive `prefix`, and an opaque `cursor`. Start without a cursor; follow
`meta.next_cursor` with the same `program` and `prefix`. `meta.complete` means
that the filtered metadata snapshot is exhausted, not that tag values are current
or atomic. A new successful upload replaces the one retained snapshot. Cursors
expire after 60 seconds or on replacement/restart; an expired cursor must be
restarted explicitly and never triggers an implicit upload.

Retained metadata is bounded to 20,000 filtered definitions and 4 MiB of encoded
JSON. Each page has at most 500 tags and 128 KiB of encoded tag-list content;
`meta.page_bytes` measures that tag-list content budget, excluding the surrounding
MCP envelope and metadata. A snapshot or single row that exceeds a limit fails
explicitly, without claiming completeness; narrow `program` or `prefix`.
Malformed rows are rejected, never silently dropped. A timer removes the retained
snapshot and a late expiry callback cannot remove a successor.

The [pycomm3 LogixDriver API](https://pycomm3.readthedocs.io/en/stable/api_reference/logix_driver.html)
provides a complete metadata upload, including program selection and `cache=False`.
Filtering and pagination happen after that upload: these bounds do **not** limit
native upload traffic, pycomm3 allocations, or its initial internal tag cache.
Pages retain metadata from the completed upload, which itself is not a controller
transactional snapshot. Native reads/writes retain their existing cache behavior.
`test_tag_browse.py` exercises real MCP continuation, bounded storage/output,
malformed metadata, scope/replacement/expiry rejection and no extra device work.

### Prioritized remaining improvements

1. Explicit operator-configured metadata upload budgets require changes/support
   in pycomm3's enumeration path; current MCP pagination cannot bound that work.
2. Add UDT member/schema browsing with separate byte/depth budgets, retaining
   protocol type information without exposing Python implementation objects.
3. Device-backed validation of routed controllers and very large tag catalogs;
   implicit cyclic I/O and CIP Security need a separate supported stack/design.
