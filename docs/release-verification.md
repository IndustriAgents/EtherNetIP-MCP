# 0.3.0 local release verification

Verified on 2026-10-15. Package: `industriagents-ethernetip-mcp`.

This records local build verification, not registry publication. Check the
GitHub Actions runs and registry version pages for publication status. Registry
authentication/trusted publishers must be configured before release; GitHub
repository access alone does not authorize registry publication.

## Checks

- Full Python regression run: 358 passed.
- Final architecture/contract tests: 15 passed (a separate run, not additional unique tests).
- Ruff lint and formatting checks passed for source and tests.
- `uv lock --check` and contract/package version parity passed.
- Three npm launcher tests passed.
- Installed wheel and installed npm tarball both initialized over stdio outside
  the checkout, reported version 0.3.0 and listed 8 canonical tools.
  Their schemas matched the contract; invalid mode calls returned MCP errors.
- `twine check --strict` passed for the wheel and source distribution.
- The npm tarball contains exactly the same wheel bytes as the PyPI artifact.
- `git diff --check` passed.

No tests were skipped in the full regression run.

These are software and mock/loopback checks, not physical-device certification.
See [protocol capability limits](architecture.md#protocol-capability-limits).

## Artifact SHA-256

Artifacts are local build outputs in `dist/` and `npm/`, excluded from Git.
Rebuilding can change archive hashes; these identify the verified files.

| Artifact | SHA-256 |
|---|---|
| `industriagents_ethernetip_mcp-0.3.0-py3-none-any.whl` | `d4485ddd106908fe7e445e2b46f0801677f798487b91d4ca3ec7f6f426acd912` |
| `industriagents_ethernetip_mcp-0.3.0.tar.gz` | `779f94e8856d94a63fc3d8302480779a1ab9189e2f531cb716d8f956a00c6407` |
| `industriagents-ethernetip-mcp-0.3.0.tgz` | `04e33d75d5bb4d1a4f5bd06f232c163b8eaf73ef730936ae6d6f397c7954a5e6` |

See [release instructions](release.md) for independent registry publication.
