# Migrate from JohnXu22786/codegraph

This guide covers the standalone <code>JohnXu22786/codegraph</code> Python project (version 1.0.0), which stores a local SQLite index. It explains how to start a fresh CodeGraphContext (CGC) index for the same source repository. It does not describe an in-place database conversion.

Keep the existing CodeGraph environment, configuration, and <code>.cg/cg.sqlite</code> index until the new CGC index has been checked. The two projects use different packages, configuration formats, MCP schemas, and database layouts.

## What changes

| Area | Legacy CodeGraph | CodeGraphContext | Migration impact |
| --- | --- | --- | --- |
| Python package and CLI | Distribution <code>codegraph</code>; import <code>codegraph</code>; command <code>codegraph</code> or <code>python -m codegraph</code>. | Distribution <code>codegraphcontext</code>; import <code>codegraphcontext</code>; commands <code>cgc</code> and <code>codegraphcontext</code>. | Install CGC separately and update scripts to use its package, command, and API. |
| Project settings | <code>&lt;repo&gt;/codegraph.json</code>, with optional <code>CODEGRAPH_*</code> environment overrides. | Global settings under <code>CGC_HOME</code> (default <code>~/.codegraphcontext</code>), including <code>.env</code> and <code>config.yaml</code>. Repository-local settings and <code>.cgcignore</code> live under <code>.codegraphcontext</code> by default when per-repository mode is used. <code>CGC_REPO_CONTEXT_DIR</code> selects a different one-component local directory name. | Do not copy <code>codegraph.json</code> into a CGC context. Review and translate only the settings you still need; the names, units, and behavior differ. |
| Index storage | SQLite file <code>&lt;repo&gt;/.cg/cg.sqlite</code> by default; the legacy CLI accepts <code>--db</code> to override it. | A selected graph backend. Global storage defaults under <code>CGC_HOME/global/db/&lt;backend&gt;/</code>; a named context uses <code>CGC_HOME/contexts/&lt;name&gt;/db/&lt;backend&gt;/</code>; per-repository mode uses <code>&lt;repo&gt;/.codegraphcontext/db/&lt;backend&gt;/</code> by default. <code>CGC_RUNTIME_DB_TYPE</code> and <code>CGC_RUNTIME_DB_PATH</code> override the global backend and database path. | These stores are not interchangeable. CGC does not open or convert the old SQLite index. |
| CLI indexing | <code>codegraph index</code> builds or incrementally refreshes the SQLite index; <code>--force</code> reparses it. | <code>cgc index &lt;repo&gt;</code> indexes into the selected CGC context; <code>cgc update &lt;repo&gt;</code> refreshes it. | Re-read source files into a new CGC context. A fresh context needs no force rebuild. |
| MCP server | <code>python -m codegraph serve</code> exposes eight stdio MCP-compatible tools. | <code>cgc mcp start</code> starts the CGC stdio server; <code>cgc mcp setup</code> configures supported clients. The CGC catalog has 29 tool definitions. <code>CGC_INSTANCE_NAME</code> selects the MCP registration and display name. | Client entries, names, schemas, and result shapes must be updated. Similar names do not guarantee equivalent behavior. |
| DSH integration | The legacy README describes a Node bridge using <code>plugin.json</code>, <code>index.js</code>, <code>dsh.bundle</code>, and <code>cordis.patch.yml</code>. The checked-out source contains the manifest, JavaScript entry point, and patch file, but no <code>dsh.bundle</code>. | The CGC repository has no matching CodeGraph Node/Cordis bundle. CGC provides a generic stdio MCP server. | Check the existing DSH installation or distribution for the README-described bundle; do not assume it is present in the source checkout. Use DSH's generic stdio MCP client for CGC, or maintain a custom bridge if the old aliases and behavior are required. |
| Backends | One local SQLite database. | Embedded and remote graph database backends; see [Database Backends](../concepts/backends.md). | F3 exercised only KuzuDB, LadybugDB, and FalkorDB Lite. Neo4j, Nornic, and remote FalkorDB were not verified in that run. |

See the [F1 feature and test baseline](../reference/f1-baseline.md) for the source inventory. Its [F3 verification update](../reference/f1-baseline.md#f3-verification-update) records the current evidence and limits.

## Legacy MCP tools and closest CGC capabilities

The legacy MCP server advertises these eight names: <code>callers</code>, <code>callees</code>, <code>deps</code>, <code>dependents</code>, <code>search</code>, <code>impact</code>, <code>overview</code>, and <code>reindex</code>. Their schemas are defined in the legacy repository's <code>plugin.json</code>. The table below names the closest CGC capability where the current tool schema and implementation support a useful comparison; it is not a drop-in mapping.

| Legacy tool | Closest CGC capability | Compatibility boundary |
| --- | --- | --- |
| <code>callers(symbol, limit)</code> | <code>analyze_code_relationships</code> with <code>query_type=find_callers</code> and <code>target</code>. | CGC uses different arguments and result shapes. Check direct-call interpretation and limits in the client. |
| <code>callees(symbol, limit)</code> | <code>analyze_code_relationships</code> with <code>query_type=find_callees</code> and <code>target</code>. | Different schema and result shape; do not pass the legacy arguments unchanged. |
| <code>deps(module, limit)</code> | <code>analyze_code_relationships</code> with <code>query_type=module_deps</code> and <code>target</code>. | Module identifiers, limits, and returned dependency details are not guaranteed to match. |
| <code>dependents(module, limit)</code> | <code>analyze_code_relationships</code> with <code>query_type=find_importers</code> and <code>target</code>. | This is the nearest reverse-import query, with a different schema and response. |
| <code>search(query, limit)</code> | <code>find_code(query)</code>. | CGC finds relevant code snippets and supports optional fuzzy matching. Legacy search is documented as full-text search over indexed names, docstrings, and signatures; results are not equivalent. |
| <code>impact(symbol, depth, limit)</code> | <code>analyze_code_relationships</code> with <code>query_type=find_all_callers</code>, <code>target</code>, and optional <code>depth</code>. | Query limits and traversal semantics differ. Compare representative results before changing an automated workflow. |
| <code>overview()</code> | <code>get_repository_stats(repo_path)</code> for one repository. | The legacy summary includes resolution rates, language counts, root, and last-indexed time. CGC's per-repository statistics schema is different; no identical global overview mapping is established. |
| <code>reindex(force)</code> | No direct CGC MCP equivalent. In the CLI, use <code>cgc index &lt;repo&gt;</code> for a new context or <code>cgc update &lt;repo&gt;</code> to refresh. The MCP tool <code>add_code_to_graph(repo_path)</code> starts a background indexing job; use <code>check_job_status(job_id)</code> to follow it. | CGC's MCP indexing path is asynchronous and has no legacy <code>force</code> parameter. It writes to the selected CGC backend, not the SQLite file. |

The legacy DSH built-in MCP client used the <code>codegraph</code> server name, exposing names such as <code>mcp__codegraph__callers</code>. The README describes a separate Node bridge with aliases <code>codegraph_callers</code>, <code>codegraph_callees</code>, <code>codegraph_deps</code>, <code>codegraph_dependents</code>, <code>codegraph_search</code>, <code>codegraph_impact</code>, <code>codegraph_overview</code>, and <code>codegraph_reindex</code>. It says the bridge keeps a Python server alive per configured root and handles timeouts and cancellation. The checked-out legacy source contains <code>plugin.json</code>, <code>index.js</code>, and <code>cordis.patch.yml</code>, but not the README-mentioned <code>dsh.bundle</code>; verify that artifact separately in an existing installation or distribution. CGC does not register those names or ship a matching DSH-specific Node/Cordis bundle. A generic MCP stdio client can start <code>cgc mcp start</code>, but existing prompts and bridge calls must be adapted. A custom bridge is required to retain the <code>codegraph_*</code> aliases or the old per-root bridge behavior.

For other supported MCP clients, see [MCP Setup](../getting-started/mcp-setup.md). To keep CGC separate from another CodeGraphContext installation, use a distinct <code>CGC_HOME</code> and MCP instance name as described in [Run the fork beside upstream](side-by-side-installations.md).

## Re-index the source repository into a fresh context

The legacy SQLite file is an implementation detail of CodeGraph. It is not an importable CGC database or a CGC bundle. There is no supported in-place conversion from <code>.cg/cg.sqlite</code>. Leave that file in place and index the repository's source files into a new CGC context.

1. Keep the legacy environment and index. Do not uninstall the old package or delete <code>.cg</code> while validating the migration.
2. Install the CGC fork source into a separate virtual environment. The fork-only <code>CGC_HOME</code> isolation described below may not be available in the published PyPI package installed by <code>pip install codegraphcontext</code>. The example clones the fork and uses the project's supported editable install. If you already have a checkout of this fork that includes the isolation settings, skip the clone and use that checkout's path. The example uses a POSIX shell; on Windows, activate the new environment and use its <code>Scripts</code> directory.

       mkdir -p "$HOME/src"
       git clone https://github.com/JohnXu22786/CodeGraphContext.git "$HOME/src/CodeGraphContext-fork"
       python3 -m venv "$HOME/.venvs/cgc-migration"
       "$HOME/.venvs/cgc-migration/bin/python" -m pip install -e "$HOME/src/CodeGraphContext-fork"
       . "$HOME/.venvs/cgc-migration/bin/activate"

3. Choose a new, absolute <code>CGC_HOME</code> outside the repository being indexed. Create a named context using a backend installed for your platform. The example uses KuzuDB; see [Installation](../getting-started/installation.md) for backend availability and setup.

       export CGC_HOME="$HOME/.codegraphcontext-migration"
       cgc context create codegraph-migration --database kuzudb
       cgc context mode named
       cgc context default codegraph-migration

   Named mode and the default context make MCP startup use this context; <code>cgc mcp start</code> has no per-command context option. These settings are stored under this <code>CGC_HOME</code>. If it already exists, confirm it is the migration home you intend to use. Do not point <code>CGC_HOME</code>, <code>CGC_RUNTIME_DB_PATH</code>, or backend-specific path overrides at the old SQLite file or another existing CGC database.
4. Index the source repository by its absolute path. Do not pass the old database file as the input, and do not run <code>cgc index</code> from the legacy project checkout by default.

       cgc index /absolute/path/to/your/source-repository --context codegraph-migration

   For each additional repository, repeat the command with that repository's path. CGC stores the results in the named context under the new <code>CGC_HOME</code>; it does not need to write a context directory into the source repository.
5. Verify that the expected repository path appears in the selected context and inspect its counts:

       cgc list --context codegraph-migration
       cgc stats /absolute/path/to/your/source-repository --context codegraph-migration

   Then run one or more known queries and compare them with the source code and the questions you care about. For example:

       cgc analyze callers known_function --context codegraph-migration

   A matching repository path and plausible query results are the migration check. Counts can differ because the parsers, graph model, backend, and query semantics differ.

Do not copy <code>codegraph.json</code> into the new context. Recreate only the desired indexing rules using CGC configuration and <code>.cgcignore</code> for the selected context; see [Configuration Contexts](contexts.md) and [Configuration Reference](../reference/config.md). The legacy <code>include</code>, <code>exclude</code>, <code>max_file_kb</code>, <code>engine</code>, and <code>language_map</code> settings do not form a CGC configuration file.

After the CLI workflow is verified, configure an MCP client for the same CGC environment. The MCP server resolves its database from the active context mode and default saved under <code>CGC_HOME</code>; keep the MCP entry's <code>CGC_HOME</code> consistent with the CLI run. For DSH, use its generic stdio MCP path if available; a host that requires the old plugin manifest or Node/Cordis bundle needs a separately maintained bridge.

## Feature support and verification boundary

| Area | CGC surface recorded in F1 | F3 evidence | What remains unverified |
| --- | --- | --- | --- |
| Source inventory | 29 MCP definitions and matching dispatch entries were inventoried; 25 approved language entries and the CLI/config inventory are listed in the baseline. | A stubbed dispatch smoke reached all 29 wrappers. | Dispatch reachability does not establish correct behavior for each tool. |
| Test behavior | F1 recorded source and existing test references; it did not run the suite because its host lacked the runtime and development dependencies. | The F3 report accounts for 1,844 test items across separate invocations: 1,836 passed, 8 skipped, and 0 failed. The initial fast-suite invocation was interrupted, so this is combined coverage, not a single uninterrupted run. Twenty-two parser goldens passed. | The tested fixture set does not establish every language option or production workload. |
| MCP tools | The baseline lists the complete 29-tool catalog and its schemas. | One real stdio MCP call to <code>find_code</code> returned a symbol from an indexed scratch repository. Twenty-one tools had partial underlying handler, CLI, contract, or unit-test evidence. | Seven tools had no behavior test in F3: <code>add_package_to_graph</code>, <code>visualize_graph_query</code>, <code>list_watched_paths</code>, <code>unwatch_directory</code>, <code>search_registry_bundles</code>, <code>discover_codegraph_contexts</code>, and <code>list_graphs</code>. The other 21 partial results are not full MCP end-to-end verification. |
| Embedded databases | F1 inventories the available backends and recorded no runtime database checks. | KuzuDB, LadybugDB, and FalkorDB Lite indexed a disposable fixture copy with matching node-label and relationship-type counts; an isolated KuzuDB context → index → query → list workflow passed. | These results cover those embedded backends and fixtures only. Neo4j, Nornic, and remote FalkorDB were not contacted or verified. |

The complete source inventory and per-tool evidence are in the [F1 baseline](../reference/f1-baseline.md#f3-verification-update). Do not interpret the 29-route smoke as 29 tools tested end-to-end.
