# 0.3.0 local release verification

Verified on 2026-10-24. Package: `industriagents-ethernetip-mcp`.

These are local development builds from the current working tree, using the
existing 0.3.0 package manifests. Nothing was published to a registry.

## Checks

- Full Python regression run: 385 passed.
- Ruff lint and formatting checks passed for source and tests, including mock sources.
- `uv lock --check` and contract/package version parity passed.
- Three npm launcher tests passed.
- Installed wheel and installed npm tarball both initialized over stdio outside
  the checkout, reported the manifest version and listed 8 canonical tools.
  Their schemas matched the contract; invalid mode calls returned MCP errors.
- `twine check --strict` passed for the wheel and source distribution.
- Workspace artifact verification matched every wheel Python source to the
  current checkout and the packaged contract to its reviewed copy. The npm
  tarball contains exactly the same wheel bytes as the Python artifact.
- `git diff --check` passed.

No tests were skipped in the full regression run.

New regressions cover request depth/size/JSON bounds before dispatch, unknown
arguments on legacy/direct routes, cancellation cleanup, audit-result failures
for structured and text-only results, and runtime/validation exceptions after
handler dispatch. The latter preserve unknown control outcomes without retries.
Application/domain imports are checked against native driver and MCP dependencies.

The follow-up review added three malformed native batch-response cases. The
fake driver applies both requested writes but returns too few or too many
results; every attempted entry remains unknown, locally refused entries stay
not_sent, and exactly one native write occurs. The final suite also includes
nine regular-file audit and bounded-policy loading checks.

These are software and mock/loopback checks, not physical-device certification.
See [protocol capability limits](architecture.md#protocol-capability-limits).

## Artifact SHA-256

Artifacts are local build outputs in `dist/` and `npm/`, excluded from Git.
Rebuilding can change archive hashes; these identify the verified files.

| Artifact | SHA-256 |
|---|---|
| `industriagents_ethernetip_mcp-0.3.0-py3-none-any.whl` | `81da94ca688c301f9ce30c11e656fe4b8062eeec08358860190adc71d9b8c857` |
| `industriagents_ethernetip_mcp-0.3.0.tar.gz` | `c17bcf7df105efbbed3a4c845fed6499dbb517715effe7f0f0939a5ca6799b42` |
| `industriagents-ethernetip-mcp-0.3.0.tgz` | `bf1c9a80726003be7b31685fb93989697ff989d9409cf24f832bd02b9d080e25` |

See [release instructions](release.md) for independent registry publication.
