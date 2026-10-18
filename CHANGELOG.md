# Changelog

## 0.7.0 — release preparation (not published)

This independent fork release candidate is based on upstream commit [`1b595a61663d60862ac20aca2422ea187671c01c`](https://github.com/CodeGraphContext/CodeGraphContext/commit/1b595a61663d60862ac20aca2422ea187671c01c). The fork keeps the MIT license, upstream attribution, `codegraphcontext` import namespace, `cgc` and `codegraphcontext` commands, and existing user data paths.

### Included

- Independent distribution metadata for `codegraphcontext-fork` version `0.7.0`, with the fork repository as its homepage and issue tracker.
- Runtime and MCP registration isolation work, while retaining the upstream import and CLI identities.
- F3E validation of the 29-tool MCP catalog and representative behavior paths.
- Q16 Spring Bean and Q17 datasource query fixes for KùzuDB, with regression coverage.
- GHCR-only image publishing configuration for the fork and matching installation guidance.
- The fork-owned GHCR package is not published yet. As part of the first image publication, the owner will set the package visibility to Public; anonymous pulls will be available after that change takes effect.

### Limitations

- Install the fork in a dedicated Python environment. The upstream and fork distributions install into the same `codegraphcontext` import namespace and expose the same CLI commands.
- KùzuDB does not support the optional Redis key-pattern enrichment path, which remains best effort. Neo4j, Nornic, and remote FalkorDB services were not contacted or validated for this release preparation; FalkorDB coverage depends on the available local driver.
- This release candidate has not been published. Package upload, signed tag, and GitHub release are separate post-merge steps.
