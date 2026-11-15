# Changelog

## 0.7.0 — 2026-10-31

Published as the independent `codegraphcontext-fork` distribution on [PyPI](https://pypi.org/project/codegraphcontext-fork/), in the [GitHub Release](https://github.com/JohnXu22786/CodeGraphContext/releases/tag/v0.7.0), and as the public [`ghcr.io/johnxu22786/codegraphcontext:0.7.0` image](https://github.com/JohnXu22786/CodeGraphContext/pkgs/container/codegraphcontext); the GHCR image supports anonymous pulls. This fork release is based on upstream commit [`1b595a61663d60862ac20aca2422ea187671c01c`](https://github.com/CodeGraphContext/CodeGraphContext/commit/1b595a61663d60862ac20aca2422ea187671c01c). It retains the MIT license, upstream attribution, `codegraphcontext` import namespace, `cgc` and `codegraphcontext` commands, and existing user data paths.

### Included

- Independent distribution metadata for `codegraphcontext-fork` version `0.7.0`, with the fork repository as its homepage and issue tracker.
- Runtime and MCP registration isolation work, while retaining the upstream import and CLI identities.
- F3E validation of the 29-tool MCP catalog and representative behavior paths.
- Q16 Spring Bean and Q17 datasource query fixes for KùzuDB, with regression coverage.
- Fork-owned GHCR image publishing and installation guidance.

### Limitations

- Install the fork in a dedicated Python environment. The upstream and fork distributions install into the same `codegraphcontext` import namespace and expose the same CLI commands.
- KùzuDB does not support the optional Redis key-pattern enrichment path, which remains best effort. Neo4j, Nornic, and remote FalkorDB services were not contacted or validated for this release; FalkorDB coverage depends on the available local driver.
