# F1 feature and test baseline

## Scope and evidence

This baseline is bound to the source at [CodeGraphContext commit 1b595a61663d60862ac20aca2422ea187671c01c](https://github.com/CodeGraphContext/CodeGraphContext/tree/1b595a61663d60862ac20aca2422ea187671c01c), declared as version 0.6.13 in <code>pyproject.toml</code>. The fork branch on which this page was prepared is at <code>40216ca315f84efd15ffd2bc769c28374d6ddf88</code> (F0 merged). The source, packaging metadata, and tests in that branch have no diff from the pinned source snapshot. These are separate identifiers: the first is the feature/code comparison baseline; the second is the fork's F0 documentation checkpoint.

The page records source inspection, existing test references, and behavior exercised in the F1 environment as separate evidence:

- **Source verified** means the declaration, dispatcher, and implementation path were found in the pinned tree. It does not mean the operation ran successfully.
- **Existing test reference** means a matching test file exists in the repository. The test was not run in this environment.
- **Exercised** is reserved for an operation actually run in F1, with its command and result recorded below.
- Issue states summarize the evidence available at this pinned target. An upstream report, a source comment, or a test name alone does not prove current user impact.

All source line references below refer to the pinned tree linked above. The main evidence files are <code>src/codegraphcontext/tool_definitions.py</code>, <code>src/codegraphcontext/server.py</code>, <code>src/codegraphcontext/tools/handlers/</code>, and <code>src/codegraphcontext/tools/graph_builder.py</code>.

## Environment and reproducible checks

| Item | Declared or observed value |
| --- | --- |
| Package | <code>codegraphcontext</code> 0.6.13 (<code>pyproject.toml:1-8</code>) |
| Python requirement | <code>>=3.10</code>; classifiers include 3.10 through 3.14 (<code>pyproject.toml:8-20</code>) |
| F1 host | CPython 3.12.14, Linux x86_64, glibc 2.41 |
| CI reference | <code>.github/workflows/test.yml</code> uses Ubuntu and Python 3.12, 3.13, and 3.14; installs <code>.[dev,parsing]</code>; runs <code>tests/run_tests.sh fast</code> |
| Active host tools | <code>python3</code> and <code>pip</code> are available. <code>pytest</code>, <code>typer</code>, Tree-sitter packages, all six database drivers, and the <code>cgc</code> entry point are absent. |
| External services | None configured or contacted. No database, registry, or remote repository operation was run. |

The installed-package check reported <code>pytest</code>, <code>pytest-asyncio</code>, <code>typer</code>, <code>tree-sitter</code>, <code>tree-sitter-language-pack</code>, <code>tree-sitter-c-sharp</code>, <code>kuzu</code>, <code>ladybug</code>, <code>falkordb</code>, <code>falkordblite</code>, <code>neo4j</code>, <code>redis</code>, <code>watchdog</code>, <code>gcf-python</code>, <code>pymysql</code>, and <code>cassandra-driver</code> as not installed. No dependencies were installed for F1.

### Checks performed

| Check | Command or method | Result |
| --- | --- | --- |
| Fork checkpoint | <code>git rev-parse HEAD</code> | <code>40216ca315f84efd15ffd2bc769c28374d6ddf88</code> |
| Pinned source identity | <code>git diff --stat 1b595a61663d60862ac20aca2422ea187671c01c HEAD -- src pyproject.toml tests</code> | No product-source, packaging, or test diff |
| Tool catalog shape | Compared <code>TOOLS</code> keys in <code>tool_definitions.py</code> with the <code>tool_map</code> keys in <code>server.py</code> using Python AST | 29 definitions, 29 dispatch entries, same key set |
| Host runtime | <code>python3 --version</code>; Python <code>platform</code> metadata | Python 3.12.14; Linux x86_64 |
| Focused test commands | See commands below | Not run: <code>pytest</code> and project runtime dependencies are absent |
| CLI help | <code>cgc --help</code> | Not run: the <code>cgc</code> entry point and <code>typer</code> are absent |
| Database-backed flows | Local fixture, backend parity, and protocol tests | Not run: no database drivers are installed |

The following are the narrow existing checks to run in a supported development environment. They use repository fixtures and test-managed temporary directories; none was run here.

    # CI's declared development install; use a disposable virtual environment.
    python3 -m pip install -e '.[dev,parsing]'

    # Tool schema and wrapper contract.
    python3 -m pytest -q tests/unit/test_tool_definitions.py tests/unit/tools/test_mcp_contracts.py

    # Existing parser goldens; the test uses its fixtures and per-test temporary database state.
    python3 -m pytest -q tests/integration/test_parser_goldens.py

    # One end-to-end stdio session over a small Python fixture and a test-local database.
    python3 -m pytest -q tests/integration/test_mcp_stdio_session.py

The relevant samples are <code>tests/fixtures/sample_projects/sample_project/</code> (Python), <code>tests/fixtures/sample_projects/sample_project_typescript/sample_tsx.tsx</code> (TypeScript/TSX), and <code>tests/fixtures/sample_projects/sample_project_misc/</code> (HTML/CSS). <code>tests/integration/test_parser_goldens.py</code> discovers 22 committed golden directories. The fixtures establish regression examples; they do not by themselves establish successful behavior on this host.

## CLI and configuration inventory

The CLI is a Typer application in <code>src/codegraphcontext/cli/main.py</code>, exposed as both <code>cgc</code> and <code>codegraphcontext</code> by <code>pyproject.toml:156-158</code>. The source registers these command groups and notable commands:

| Group | Commands found in source |
| --- | --- |
| Root | <code>doctor</code>, <code>index</code>, <code>update</code>, <code>delete</code>, <code>stats</code>, <code>clean</code>, <code>setup-scip</code>, <code>list</code>, <code>add-package</code>, <code>report</code>, <code>visualize</code>, <code>watch</code>, <code>unwatch</code>, <code>watching</code>, <code>query</code>, <code>diagram</code>, <code>version</code>, <code>help</code>; hidden legacy <code>cypher</code> |
| <code>mcp</code> | <code>setup</code>, <code>start</code>, <code>tools</code> |
| <code>neo4j</code> | <code>setup</code> |
| <code>config</code> | <code>show</code>, <code>set</code>, <code>reset</code>, <code>db</code> |
| <code>context</code> | <code>list</code>, <code>create</code>, <code>delete</code>, <code>mode</code>, <code>default</code> |
| <code>prompt</code> | <code>add</code>, <code>list</code>, <code>remove</code> |
| <code>bundle</code> | <code>export</code>, <code>import</code>, <code>verify</code>, <code>inspect</code>, <code>diff</code>, <code>load</code>, <code>merge</code> |
| <code>hook</code> | <code>install</code>, <code>uninstall</code>, <code>status</code> |
| <code>registry</code> | <code>list</code>, <code>search</code>, <code>download</code>, <code>request</code> |
| <code>api</code> | <code>start</code> |
| <code>find</code> | <code>name</code>, <code>pattern</code>, <code>type</code>, <code>variable</code>, <code>content</code>, <code>decorator</code>, <code>argument</code> |
| <code>analyze</code> | <code>calls</code>, <code>callers</code>, <code>chain</code>, <code>kotlin-call-audit</code>, <code>deps</code>, <code>tree</code>, <code>complexity</code>, <code>dead-code</code>, <code>overrides</code>, <code>variable</code> |
| <code>datasource</code> | <code>mysql</code>, <code>cassandra</code>, <code>redis</code> |
| <code>simulate</code> | <code>metrics</code>, <code>run</code>, <code>compare</code>, <code>timeline</code> |
| Shortcuts | <code>m</code>, <code>n</code>, <code>i</code>, <code>ls</code>, <code>rm</code>, <code>v</code>, <code>w</code>, <code>export</code>, <code>load</code> |

The source inventory is also represented by <code>tests/integration/cli/test_cli_commands.py::test_cli_inventory_grouped_from_source</code>. No live <code>--help</code> output was available, so this is a source inventory, not a runtime CLI confirmation. <code>docs/CLI_COMPLETE_REFERENCE.md</code> reports 55 commands but omits groups now present in source, including <code>context</code>, <code>prompt</code>, <code>hook</code>, <code>api</code>, <code>datasource</code>, and <code>simulate</code>.

Configuration is read from <code>~/.codegraphcontext/.env</code> (<code>cli/config_manager.py:69-70</code>); per-context configuration lives in <code>.codegraphcontext/config.yaml</code> (<code>cli/config_manager.py:826</code> and context handling in <code>server.py</code>). Important defaults and switches include:

| Setting | Source default or meaning |
| --- | --- |
| <code>DEFAULT_DATABASE</code> | <code>falkordb</code>; <code>cgc config db</code> selects a backend |
| <code>INDEX_SOURCE</code> | <code>true</code> |
| <code>SCIP_INDEXER</code> | <code>false</code>; Tree-sitter remains the default path |
| <code>SCIP_LANGUAGES</code> | <code>python,typescript,javascript,go,rust,java,dart,cpp,c,csharp</code> |
| <code>MAX_TOOL_RESPONSE_TOKENS</code>, <code>MAX_PROMPT_CHARS</code> | Both <code>0</code> (unlimited); a positive value enables a cap |
| <code>TOOL_RESULT_LIMITS</code> | Empty override map by default; tools with a built-in limit still use it (<code>utils/tool_limits.py:21-86</code>) |
| <code>CACHE_ENABLED</code> | <code>true</code> |
| <code>CGC_ALLOWED_ROOTS</code> | Adds permitted filesystem roots; current working directory is allowed by default |
| <code>CGC_ALLOW_ALL_PATHS</code> | Explicitly disables path sandboxing when true |
| <code>CGC_RUNTIME_DB_TYPE</code>, <code>CGC_RUNTIME_DB_PATH</code> | Per-invocation database type and path overrides |
| <code>CGC_EMBEDDED_BUFFER_POOL_MB</code> | Embedded Kùzu/Ladybug memory budget override |
| <code>CGC_API_KEY</code> | Optional API key for the HTTP gateway |

The effective backend selection and context routing also depend on backend-specific variables such as <code>FALKORDB_HOST</code>, <code>FALKORDB_PATH</code>, <code>NEO4J_URI</code>, and <code>NORNIC_URI</code>. Credentials were not read or recorded.

## Declared languages and file types

The approved inventory names 25 languages. <code>GraphBuilder.parsers</code> maps the extensions below (<code>tools/graph_builder.py:64-107</code>); <code>TreeSitterParser</code> dispatches to the language parser classes (<code>tools/tree_sitter_parser.py:23-50</code>). The fixture column identifies an existing sample project, not a passing test.

| Inventory language | Source extensions | Existing sample fixture |
| --- | --- | --- |
| Python | <code>.py</code>, <code>.ipynb</code> | <code>sample_project</code> |
| JavaScript | <code>.js</code>, <code>.jsx</code>, <code>.mjs</code>, <code>.cjs</code> | <code>sample_project_javascript</code> |
| TypeScript | <code>.ts</code>, <code>.mts</code>, <code>.cts</code>, <code>.d.ts</code> | <code>sample_project_typescript</code> |
| Java | <code>.java</code> | <code>sample_project_java</code> |
| C | <code>.c</code> | <code>sample_project_c</code> |
| C++ | <code>.cpp</code>, <code>.h</code>, <code>.hpp</code>, <code>.hh</code> | <code>sample_project_cpp</code> |
| C# | <code>.cs</code> | <code>sample_project_csharp</code> |
| Go | <code>.go</code> | <code>sample_project_go</code> |
| Rust | <code>.rs</code> | <code>sample_project_rust</code> |
| Ruby | <code>.rb</code> | <code>sample_project_ruby</code> |
| PHP | <code>.php</code> | <code>sample_project_php</code> |
| Swift | <code>.swift</code> | <code>sample_project_swift</code> |
| Kotlin | <code>.kt</code> | <code>sample_project_kotlin</code> |
| Dart | <code>.dart</code> | <code>sample_project_dart</code> |
| Perl | <code>.pl</code>, <code>.pm</code> | <code>sample_project_perl</code> |
| Lua | <code>.lua</code> | <code>sample_project_lua</code> |
| Scala | <code>.scala</code>, <code>.sc</code> | <code>sample_project_scala</code> |
| Haskell | <code>.hs</code> | <code>sample_project_haskell</code> |
| Elixir | <code>.ex</code>, <code>.exs</code> | <code>sample_project_elixir</code> |
| Emacs Lisp | <code>.el</code> | <code>sample_project_elisp</code> |
| HTML | <code>.html</code> | <code>sample_project_misc</code> |
| CSS | <code>.css</code> | <code>sample_project_misc</code> |
| TSX | <code>.tsx</code> | <code>sample_project_typescript/sample_tsx.tsx</code> |
| Solidity | <code>.sol</code> | <code>sample_project_solidity</code> and <code>sample_project_solidity_foundry</code> |
| HCL | <code>.tf</code>, <code>.tfvars</code>, <code>.hcl</code> | <code>sample_project_hcl</code> |

The parser registry also includes Svelte (<code>.svelte</code>) and Vue (<code>.vue</code>) formats; these are extra source-discovered parsers, not extra entries in the approved 25-language inventory (<code>graph_builder.py:102-103</code>, <code>tree_sitter_parser.py:47-48</code>). Configuration and documentation files are separate from parsed languages: <code>.toml</code>, <code>.sh</code>, <code>.yaml</code>, <code>.yml</code>, <code>.json</code>, <code>.ini</code>, <code>.cfg</code>, <code>.md</code>, <code>.txt</code>, <code>.bat</code>, <code>.ps1</code>, <code>Dockerfile</code>, <code>Makefile</code>, <code>.gitignore</code>, <code>.dockerignore</code>, and <code>.env</code> are admitted as minimal File nodes (<code>graph_builder.py:109-120</code>, <code>indexing/discovery.py:12-22</code>). The source does not map <code>.cc</code> or <code>.cxx</code> in <code>GraphBuilder.parsers</code>.

The <code>add_package_to_graph</code> MCP tool has a narrower package-resolution language enum than the parser inventory: Python, JavaScript, TypeScript, Java, C, Go, Ruby, PHP, and C++ (<code>tool_definitions.py:169-189</code>). This is a package resolver contract, not a claim that other parser languages are unsupported.

## Database backends

The factory exposes six backends (<code>core/__init__.py:201-295</code>). Backend availability is platform- and service-dependent; no backend was installed or exercised in F1.

| Backend | Source and configuration | Declared dependency information | F1 state |
| --- | --- | --- | --- |
| KùzuDB | Embedded, <code>KUZUDB_PATH</code>; explicit type <code>kuzudb</code> | <code>kuzu</code> is conditional: Python <code><3.14</code> or Linux (<code>pyproject.toml:79-92</code>) | Source verified; runtime unavailable |
| LadybugDB | Embedded, <code>LADYBUGDB_PATH</code>; native engine is probed before use | <code>ladybug>=0.19,<0.20</code> with platform marker (<code>pyproject.toml:92</code>) | Source verified; runtime unavailable |
| FalkorDB Lite | Embedded local backend, <code>FALKORDB_PATH</code>; source availability check is Unix/Python 3.12+ | <code>falkordblite>=0.7,<0.10</code> on non-Windows Python 3.12+; <code>falkordb>=1.0,<1.6</code>; <code>redis>=5,<6</code> (<code>pyproject.toml:55-76</code>) | Source verified; runtime unavailable |
| FalkorDB Remote | External service via <code>FALKORDB_HOST</code>, port 6379 by default, optional username/password/TLS | <code>falkordb>=1.0,<1.6</code>; <code>redis>=5,<6</code> (<code>pyproject.toml:73-76</code>, <code>core/database_falkordb_remote.py:53-60</code>) | No service configured or contacted |
| Neo4j | External service via <code>NEO4J_URI</code>, credentials, optional database | <code>neo4j>=5.15.0</code> (<code>pyproject.toml:22-24</code>) | No service configured or contacted |
| Nornic | Neo4j-driver-compatible external service via <code>NORNIC_URI</code> and Nornic credentials | Uses the declared Neo4j Python driver; no separate Nornic Python driver declared (<code>core/database_nornic.py:49-83</code>) | No service configured or contacted |

Named extras exist for <code>falkordb-embedded</code>, <code>falkordb-remote</code>, <code>kuzu</code>, <code>ladybug</code>, <code>neo4j</code>, <code>gcf</code>, <code>dev</code>, and <code>datasources</code> (<code>pyproject.toml:99-150</code>). The current base dependency list still declares several of the same backend drivers; the extras do not yet make the default install backend-light.

## MCP tool matrix

The source has 29 tool definitions and 29 entries in <code>MCPServer.handle_tool_call</code>'s dispatch map. <code>MCPServer._init_tools</code> exposes those definitions except entries disabled by <code>disabledTools</code> (<code>server.py:248-257</code>); wrappers call the handler or inline implementation shown below. The key set was compared statically for this baseline. No live <code>tools/list</code> or tool call was made.

In the first column, **T** is the schema entry line in <code>tool_definitions.py</code>; **S** is the wrapper line in <code>server.py</code>. All tools share dispatch map <code>server.py:731-760</code>. The common test <code>tests/unit/tools/test_mcp_contracts.py::test_every_tool_definition_has_a_matching_wrapper</code> checks declared definitions against wrapper methods; it does not assert runtime results. Other test references are identified as direct or related, and none was run here. Sample arguments are contract examples only; they are not executed.

| Tool (T/S) | Supported contract and sample arguments | Handler/source | Existing test evidence in tree | F1 state |
| --- | --- | --- | --- | --- |
| <code>add_code_to_graph</code> (T10/S377) | Start a background scan of a repository; <code>{"repo_path":"&lt;scratch-repo&gt;","is_dependency":false}</code> | <code>indexing_handlers.py:12</code>; returns a job ID | <code>test_add_code_graph_name.py</code>; <code>test_mcp_stdio_session.py</code> | Source verified; not exercised |
| <code>check_job_status</code> (T31/S365) | Read job state by ID; <code>{"job_id":"&lt;job-id&gt;"}</code> | <code>management_handlers.py:58</code> | <code>test_management_handlers_jobs.py</code> | Source verified; not exercised |
| <code>list_jobs</code> (T46/S368) | List tracked background jobs; <code>{}</code> | <code>management_handlers.py:112</code> | <code>test_management_handlers_jobs.py</code> | Source verified; not exercised |
| <code>find_code</code> (T55/S356) | Search indexed names/content, optionally fuzzy and repository-scoped; <code>{"query":"greet","repo_path":"&lt;repo&gt;"}</code> | <code>analysis_handlers.py:124</code> | <code>test_mcp_stdio_session.py</code> | Source verified; not exercised |
| <code>analyze_code_relationships</code> (T85/S353) | Query callers/callees, imports, hierarchy, overrides, chains, complexity and other relationship types; <code>{"query_type":"find_callers","target":"greet"}</code> | <code>analysis_handlers.py:82</code> | <code>test_query_correctness_batch.py</code>; <code>test_tool_definitions.py</code> | Source verified; not exercised |
| <code>watch_directory</code> (T133/S395) | Start a watcher for a repository; <code>{"repo_path":"&lt;scratch-repo&gt;"}</code> | <code>watcher_handlers.py:34</code> | <code>test_watcher_live_events.py</code>; <code>test_watcher_rename_and_concurrency.py</code> | Source verified; not exercised |
| <code>execute_cypher_query</code> (T148/S338) | Execute a read-only query with optional parameters; <code>{"cypher_query":"MATCH (n:Function) RETURN n LIMIT 10"}</code> | <code>query_handlers.py:15</code> | <code>test_cypher_readonly_enforcement.py</code>; <code>test_tool_definitions.py</code> | Source verified; not exercised |
| <code>add_package_to_graph</code> (T169/S386) | Resolve an installed package for one of nine enumerated languages and index it; <code>{"package_name":"requests","language":"python"}</code> | <code>indexing_handlers.py:108</code> | <code>test_package_resolver_flat_module.py</code> (resolver only; no MCP call located) | Source verified; not exercised |
| <code>find_dead_code</code> (T192/S344) | Return possible unused functions, with optional decorator and repository filters; <code>{"repo_path":"&lt;repo&gt;"}</code> | <code>analysis_handlers.py:8</code>; <code>CodeFinder</code> adds confidence and reason fields | <code>test_dead_code_real_chain.py</code>; <code>test_dead_code_analysis.py</code> | Source verified; not exercised |
| <code>calculate_cyclomatic_complexity</code> (T211/S347) | Find a named function, optionally by file and repo; <code>{"function_name":"greet","path":"src/app.py"}</code> | <code>analysis_handlers.py:39</code> | <code>test_tool_definitions.py</code>; <code>test_mcp_server.py</code> | Source verified; not exercised |
| <code>find_most_complex_functions</code> (T226/S350) | List top functions by complexity; default limit 10; <code>{"limit":10,"repo_path":"&lt;repo&gt;"}</code> | <code>analysis_handlers.py:64</code> | <code>test_analyze_complexity_file_path.py</code>; <code>test_query_correctness_batch.py</code> | Source verified; not exercised |
| <code>list_indexed_repositories</code> (T239/S359) | List indexed repositories; <code>{}</code> | <code>management_handlers.py:11</code> | <code>test_cli_commands.py</code> (CLI path only; no direct MCP test located) | Source verified; not exercised |
| <code>delete_repository</code> (T250/S362) | Delete all graph data for one repository; destructive; <code>{"repo_path":"&lt;scratch-repo&gt;"}</code> | <code>management_handlers.py:25</code> | <code>test_management_handlers_delete.py</code>; <code>test_mcp_contracts.py</code> | Source verified; not exercised |
| <code>visualize_graph_query</code> (T272/S341) | Generate a Neo4j Browser URL for a Cypher query; <code>{"cypher_query":"MATCH (n) RETURN n LIMIT 25"}</code> | <code>query_handlers.py:93</code> | <code>test_visualize_graph.py</code> (visualization helper; no direct MCP call located) | Source verified; not exercised |
| <code>list_watched_paths</code> (T285/S371) | List active watch roots; <code>{}</code> | <code>watcher_handlers.py:8</code> | <code>test_watcher_live_events.py</code> (related watcher coverage; no direct tool-name test located) | Source verified; not exercised |
| <code>unwatch_directory</code> (T294/S374) | Stop watching a repository; <code>{"repo_path":"&lt;scratch-repo&gt;"}</code> | <code>watcher_handlers.py:16</code> | <code>test_watcher_stop_state.py</code> (watcher stop cleanup) | Source verified; not exercised |
| <code>load_bundle</code> (T306/S404) | Load a local or registry <code>.cgc</code> bundle; <code>clear_existing</code> defaults false and is destructive when true; <code>{"bundle_name":"&lt;scratch&gt;.cgc","clear_existing":false}</code> | <code>management_handlers.py:137</code> | <code>test_mcp_contracts.py</code> checks destructive warnings; bundle unit tests cover adjacent bundle paths | Source verified; not exercised |
| <code>search_registry_bundles</code> (T337/S407) | Search public bundle registry; <code>{"query":"flask","unique_only":true}</code> | <code>management_handlers.py:254</code>; registry access may use network | <code>test_registry_command.py</code> (CLI path; no direct MCP call located) | Source verified; not exercised |
| <code>get_repository_stats</code> (T349/S410) | Return repository/database counts; <code>{"repo_path":"&lt;repo&gt;"}</code> | <code>management_handlers.py:333</code> | <code>test_cli_commands.py</code> (CLI stats path; no direct MCP call located) | Source verified; not exercised |
| <code>discover_codegraph_contexts</code> (T361/S511) | Scan for child <code>.codegraphcontext</code> directories, default depth 1; <code>{"repo_path":"&lt;parent&gt;","max_depth":1}</code> | <code>server.py:511-535</code> | <code>test_context_repo_registration.py</code> covers context registration, not this MCP call | Source verified; not exercised |
| <code>switch_context</code> (T373/S579) | Switch the active context and optionally persist the mapping; <code>{"context_path":"&lt;repo&gt;","save":false}</code> | <code>server.py:579-675</code>; blocked while jobs are active | <code>test_mcp_server.py</code>; <code>test_jobs.py</code> | Source verified; not exercised |
| <code>list_graphs</code> (T389/S413) | List graphs where the selected backend supports it; <code>{}</code> | <code>management_handlers.py:418</code>; unsupported backends return an empty list and message | No direct tool-specific test located | Source verified; not exercised |
| <code>generate_report</code> (T398/S416) | Write a Markdown report under an allowed path; <code>{"output_path":"&lt;scratch&gt;/report.md"}</code> | <code>server.py:416-439</code>; <code>tools/report_generator.py:395</code> | No direct MCP report test located | Source verified; not exercised |
| <code>find_java_spring_endpoints</code> (T413/S441) | Filter parsed Spring endpoints by method/path; <code>{"http_method":"GET","path_pattern":"/health"}</code> | <code>analysis_handlers.py:169</code> | <code>test_mcp_contracts.py</code> checks row-cap metadata; <code>test_java_parser.py</code> covers extraction | Source verified; not exercised |
| <code>find_java_spring_beans</code> (T427/S444) | Filter Spring bean classes by stereotype; <code>{"stereotype":"SERVICE"}</code> | <code>analysis_handlers.py:215</code> | <code>test_mcp_contracts.py</code> checks row-cap metadata; <code>test_java_parser.py</code> covers extraction | Source verified; not exercised |
| <code>find_datasource_nodes</code> (T443/S447) | Query datasource/table/key-pattern nodes; kind is MySQL, Cassandra, or Redis; <code>{"kind":"mysql","include_columns":false}</code> | <code>analysis_handlers.py:258</code> | <code>test_build_and_datasource_kuzu_writes.py</code>; <code>test_datasource_ssl_params.py</code> (ingesters/writes, not MCP call) | Source verified; not exercised |
| <code>simulate_metrics</code> (T460/S450) | Load a graph twin and report architecture metrics; <code>{"repo_path":"&lt;repo&gt;"}</code> | <code>server.py:450-457</code>; <code>core/simulator.py</code> | <code>test_simulator.py</code>; <code>test_simulator_kuzu.py</code> | Source verified; not exercised |
| <code>simulate_architectural_change</code> (T478/S459) | Apply in-memory decomposition/dependency/node changes and compare metrics; <code>{"changes":[{"type":"add_dependency","source":"A","target":"B"}]}</code> | <code>server.py:459-486</code>; <code>core/simulator.py</code> | <code>test_simulator.py</code> | Source verified; not exercised |
| <code>analyze_architectural_evolution</code> (T531/S488) | Analyze Git churn, repository growth, and hotspots; default 50 commits; <code>{"repo_path":"&lt;repo&gt;","commits":50}</code> | <code>server.py:488-509</code>; <code>core/simulator.py</code> | No direct tool-specific test located | Source verified; not exercised |

The source-backed inventory corrects the older MCP references: <code>docs/MCP_TOOLS.md:3</code> and <code>docs/docs/reference/mcp.md:3</code> still say version 0.4.16 and 25 tools. The pinned package declares 0.6.13 and the source catalog has 29 definitions. The stdio session test checks that <code>tools/list</code> returns at least 20 entries and calls <code>find_code</code>; it does not assert exactly 29 or call all tools.

## Other extension inventory

| Area | Current source capability and relevant sample/command | Evidence and F1 state |
| --- | --- | --- |
| SCIP | Optional index path; <code>SCIP_INDEXER</code> defaults off. <code>cgc setup-scip</code> audits local indexer availability. <code>GraphBuilder</code> falls back to Tree-sitter when the requested SCIP indexer is unavailable. The config default lists 10 languages; the source fallback string in <code>graph_builder.py</code> lists 15. | <code>tools/scip_indexer.py</code>, <code>tools/indexing/scip_pipeline.py</code>, <code>graph_builder.py:576-655</code>, <code>cli/main.py:1802-1810</code>; external indexers absent, no indexing run |
| Jobs | Background in-memory job tracking supports pending/running/completed/failed/cancelled states, progress, and bounded failure tracebacks. Example: index, then inspect with <code>check_job_status</code> or <code>list_jobs</code>. | <code>core/jobs.py:19-25,73-190</code>; <code>tests/unit/tools/test_management_handlers_jobs.py</code>; not run |
| Watcher and Git hooks | Watchdog-based incremental updates and <code>cgc watch</code>, <code>cgc unwatch</code>, <code>cgc watching</code>; Git hook install/uninstall/status commands are registered. Example: <code>cgc hook status</code>. | <code>core/watcher.py:199-220,382-405,461-497</code>, <code>cli/hook_manager.py</code>, <code>cli/main.py:1087-1127</code>; no watcher or hook run |
| Contexts and graphs | Global and per-repository contexts, create/list/delete/mode/default CLI commands, context discovery/switch MCP tools, and graph listing for backends that implement it. Example: <code>cgc context list</code>. | <code>cli/main.py:234-292</code>, <code>server.py:511-675</code>, <code>management_handlers.py:418-433</code>; no context switched |
| HTTP API | REST routes under <code>/api/v1</code> for status, tool list/call, index, query, and repositories; health route <code>/health</code>. Start with <code>cgc api start</code> (default bind <code>0.0.0.0:8000</code>). | <code>api/router.py:45-158</code>, <code>api/app.py:137-142</code>, <code>cli/main.py:1292-1311</code>; not started |
| MCP transports | Stdio via <code>cgc mcp start</code>; Streamable HTTP at <code>/api/v1/mcp</code>; legacy SSE at <code>/api/v1/mcp/sse</code> plus <code>/api/v1/mcp/messages</code>. The SSE route is explicitly deprecated but retained. | <code>server.py:784-903</code>, <code>api/app.py:144-205</code>, <code>api/mcp_sse.py:17-64</code>; no protocol session run |
| Bundles and registry | Export/import/verify/inspect/diff/load/merge <code>.cgc</code> bundles; CLI registry list/search/download/request. Example: <code>cgc bundle inspect &lt;file.cgc&gt;</code>. | <code>core/cgc_bundle.py</code>, <code>core/bundle_registry.py</code>, <code>cli/main.py:711-1087,1289-1382</code>; registry/network not accessed |
| Visualization and reports | Local graph UI, diagram CLI, query-to-Neo4j Browser URL, and Markdown report generation. Example: <code>cgc diagram</code> or MCP <code>generate_report</code> to a scratch path. | <code>cli/visualizer.py</code>, <code>viz/server.py</code>, <code>tools/handlers/query_handlers.py</code>, <code>tools/report_generator.py</code>; not started or written |
| Spring and data sources | Java parser extracts Spring endpoints/beans and data-access relations; MCP tools query Spring and datasource nodes; <code>cgc datasource</code> has MySQL, Cassandra, and Redis ingestion commands. | <code>tools/languages/java.py</code>, <code>tools/query_tool_languages/java_toolkit.py</code>, <code>tools/datasources/</code>, <code>analysis_handlers.py:169-348</code>; no external data source used |
| Architectural simulation | In-memory code-graph twin computes metrics, simulates dependency/service/node changes, and combines Git churn with complexity. CLI group: <code>cgc simulate metrics|run|compare|timeline</code>. | <code>core/simulator.py</code>, <code>server.py:450-509</code>, <code>cli/simulator.py:96-276</code>; no graph/Git evolution analysis run |

One source-level protocol distinction remains relevant to Q08: stdio uses <code>encode_response</code> and its own JSON-RPC error envelope (<code>server.py:858-869</code>); the HTTP MCP handler formats successful results with <code>json.dumps</code> and reports tool errors through <code>CallToolResult.isError</code> (<code>api/mcp_sse.py:30-47</code>). The HTTP surface is implemented by the MCP SDK and was not exercised here.

## Q01-Q15 issue evidence at the pinned target

These are F1 evidence states, not claims that an upstream issue is currently open or reproduced in a user's workload. References point to source inspection or historical reports; behavior requiring a database, a stress run, or the legacy repository remains unverified.

| ID | F1 evidence state | Evidence and scope conclusion |
| --- | --- | --- |
| Q01 — Output and relevance | **Partially present in source; not behaviorally reproduced.** | <code>server.py:118-178</code> supports response truncation, but the defaults <code>MAX_TOOL_RESPONSE_TOKENS=0</code> and <code>MAX_PROMPT_CHARS=0</code> mean no serialized response cap by default (<code>cli/config_manager.py:116-124</code>). The <code>TOOL_RESULT_LIMITS</code> override map defaults to <code>{}</code>, but built-in count limits still apply through <code>get_tool_result_limit</code> (<code>utils/tool_limits.py:21-86</code>; e.g. 20 for <code>find_code</code>, 50 for <code>find_dead_code</code>, and 100 for <code>execute_cypher_query</code>). No large-output or ranking comparison was run. Historical report: [#412](https://github.com/CodeGraphContext/CodeGraphContext/issues/412). |
| Q02 — Watcher resources | **Partially addressed in source; no stress reproduction.** | Per-path debounce timers replace same-path timers and remove fired entries (<code>core/watcher.py:199-220</code>), addressing the timer-map retention described in older notes. Separate paths still create separate timer threads; the comment identifies concurrent handlers and a shared update lock (<code>:199-205</code>). Deletion events are debounced (<code>:390-392</code>). Existing unit coverage includes rename/concurrency, but no event-storm or long-duration memory test ran. Historical report: [#744](https://github.com/CodeGraphContext/CodeGraphContext/issues/744). |
| Q03 — Privacy and file scope | **Partially implemented in source; runtime cases not checked.** | Path sandboxing defaults to the working directory plus <code>CGC_ALLOWED_ROOTS</code>; <code>CGC_ALLOW_ALL_PATHS</code> or <code>CGC_ALLOWED_ROOTS=*</code> disables it (<code>utils/path_sandbox.py:23-61</code>). No dry-run/strict-local workflow was found in the reviewed source. Allowed-root, symlink, watcher, and secret-redaction behavior was not executed. Historical request: [#999](https://github.com/CodeGraphContext/CodeGraphContext/issues/999). |
| Q04 — Backend performance and disk | **Not reproduced.** | The cited report is historical and specifically needs retesting against the pinned backend/dependency state. No write benchmark, repeated rebuild, disk-growth measurement, or external backend run was performed; backend packages are absent. Historical report: [#1168](https://github.com/CodeGraphContext/CodeGraphContext/issues/1168). |
| Q05 — Installation and native dependencies | **Source pins verified; platform behavior not rerun.** | Dependency ranges and markers constrain Tree-sitter, Redis/FalkorDB, Kùzu, and Ladybug (<code>pyproject.toml:30-92</code>). <code>tests/unit/test_environment_pins.py</code> exists; it was not run. No wheel install was attempted on Windows, macOS, or Python 3.14. Historical reports: [#691](https://github.com/CodeGraphContext/CodeGraphContext/issues/691), [#1708](https://github.com/CodeGraphContext/CodeGraphContext/issues/1708). |
| Q06 — Database ownership | **Not reproduced; ownership handoff remains unverified.** | The source has embedded and server-backed managers plus runtime type/path overrides (<code>core/__init__.py:201-310</code>, <code>cli/config_manager.py:865-873</code>). No active-gateway/CLI/Git-hook concurrency test was run, and the F1 evidence does not establish whether a CLI command delegates to an existing gateway. Historical report: [#1683](https://github.com/CodeGraphContext/CodeGraphContext/issues/1683). |
| Q07 — True cancellation | **Source-level gap: status exists, cancellation operation not found.** | <code>JobStatus.CANCELLED</code> is declared (<code>core/jobs.py:19-25</code>), but <code>JobManager</code> exposes create/update/read/list/cleanup and no cancellation method (<code>:73-190</code>); no cancel MCP tool is declared. Existing tests can set the status directly, which does not prove worker cancellation. No active indexing job was cancelled. Historical reports: [#829](https://github.com/CodeGraphContext/CodeGraphContext/issues/829), [#1228](https://github.com/CodeGraphContext/CodeGraphContext/issues/1228). |
| Q08 — Protocol behavior differences | **Source difference confirmed; runtime compatibility not tested.** | Stdio serializes with <code>encode_response</code> and returns custom JSON-RPC errors (<code>server.py:858-869</code>); HTTP MCP serializes with <code>json.dumps</code> and uses SDK <code>isError</code> results (<code>api/mcp_sse.py:30-47</code>). This confirms separate paths, not the impact of any client incompatibility. |
| Q09 — Heavy default dependencies | **Packaging layout confirmed in source.** | The base dependency list contains backend drivers and runtime packages; named backend extras repeat some of those ranges (<code>pyproject.toml:22-97,99-150</code>). The current extras do not yet make the default install lean. No install-size measurement was made. |
| Q10 — Feature docs and version | **Source/document mismatch verified.** | <code>pyproject.toml</code> declares 0.6.13 and source declares 29 MCP tools; <code>docs/MCP_TOOLS.md:3</code> and <code>docs/docs/reference/mcp.md:3</code> say 0.4.16 and 25 tools. The CLI reference's command count and group list are also behind the source/test inventory. HTTP and MCP API metadata use 0.1.0 (<code>api/app.py:119-123</code>, <code>api/mcp_sse.py:50-56</code>); this is a distinct gateway/server version field, not the package version. |
| Q11 — Confidence and evidence | **Partially implemented; MCP consumer parity not verified.** | <code>CodeFinder.find_dead_code</code> classifies candidates and returns confidence/reason/counts (<code>tools/code_finder.py:989-1131</code>). The CLI exposes <code>--show-all</code> and passes <code>include_low_confidence</code> (<code>cli/main.py:3211-3247</code>), but the MCP schema and <code>analysis_handlers.find_dead_code</code> do not expose/pass that option (<code>tool_definitions.py:192-209</code>, <code>analysis_handlers.py:8-16</code>). This is a source-level capability gap in MCP; no result comparison was run. Historical report: [#885](https://github.com/CodeGraphContext/CodeGraphContext/issues/885). |
| Q12 — Incremental/cache correctness | **Not checked by behavior.** | Source persists a repository commit hash (<code>tools/indexing/persistence/writer.py:322-353</code>) and has incremental update paths; <code>GraphBuilder</code>'s thread-local cache holds parser instances (<code>tools/graph_builder.py:123-150</code>). These facts do not settle content invalidation, grammar/config invalidation, or move/delete equivalence. No incremental-versus-clean-rebuild comparison ran. Historical request: [#1288](https://github.com/CodeGraphContext/CodeGraphContext/issues/1288). |
| Q13 — Branches and snapshots | **Feature request; not part of current tool inventory.** | The reviewed MCP and CLI inventories contain no dedicated branch/worktree graph snapshot or graph-diff command. No behavior defect is asserted. Historical requests: [#736](https://github.com/CodeGraphContext/CodeGraphContext/issues/736), [#1312](https://github.com/CodeGraphContext/CodeGraphContext/issues/1312); contributor draft [#1736](https://github.com/CodeGraphContext/CodeGraphContext/pull/1736). |
| Q14 — Legacy project migration | **Not checked in this unit.** | The old <code>JohnXu22786/codegraph</code> tree and its runtime data are outside this pinned CodeGraphContext source worktree. F1 did not inspect or modify that project, nor run an import/reindex migration. Keep its <code>.gitignore</code> and cache notes as historical comparison requirements for F4/G2. |
| Q15 — Package naming collision | **Target package identity source-verified; registry collision not rechecked.** | The target distribution and import namespace are <code>codegraphcontext</code> (<code>pyproject.toml:1-4,156-158</code>). The approved queue records the separate PyPI <code>codegraph</code> name as a prior verified collision; this F1 run did not query PyPI again or change the package identity. |

## F1 completion boundary

The source inventory, line references, sample fixtures, test references, environment, and issue evidence are recorded here. Runtime parity is not implied: no existing tests or product commands could run because the host lacks the project and development dependencies. F1 adds no product behavior, dependencies, test files, or user data. The next full language/backend/protocol acceptance remains F3's responsibility.
