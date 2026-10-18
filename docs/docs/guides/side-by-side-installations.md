# Run the fork beside upstream

The fork currently uses the same public package and Python import names as upstream. Until a distinct package, import, and CLI identity is selected, install each checkout into a separate Python environment. Installing both into one environment would overwrite the `codegraphcontext` module and the `cgc` / `codegraphcontext` commands.

The project metadata currently declares distribution `codegraphcontext` version `0.6.13`, imports from `codegraphcontext`, and exposes `cgc` and `codegraphcontext` entry points (`pyproject.toml`). This guide isolates runtime state and client registrations; it does not rename those public identities.

## Use a separate Python environment

Create an environment for the fork and install the fork checkout into it:

```bash
python -m venv "$HOME/.venvs/codegraphcontext-fork"
"$HOME/.venvs/codegraphcontext-fork/bin/python" -m pip install -e /path/to/CodeGraphContext-fork
```

Use that environment's interpreter or CLI path explicitly. Do not rely on whichever `cgc` command happens to appear first on `PATH`.

## Give the fork its own state

Set these variables for the fork process:

```bash
CGC_HOME="$HOME/.codegraphcontext-fork" \
CGC_REPO_CONTEXT_DIR=".codegraphcontext-fork" \
CGC_INSTANCE_NAME="CodeGraphContext Fork" \
"$HOME/.venvs/codegraphcontext-fork/bin/python" -m codegraphcontext mcp setup
```

The values have separate purposes:

| Variable | Default | Effect |
| --- | --- | --- |
| `CGC_HOME` | `~/.codegraphcontext` | Selects user configuration, named-context state, global database defaults, logs, and imported bundle storage. With a distinct custom root, the default debug log is also stored under `<CGC_HOME>/logs/`; with no custom root, the legacy `~/mcp_debug.log` location is retained. Use an absolute path for predictable behavior. |
| `CGC_REPO_CONTEXT_DIR` | `.codegraphcontext` | Selects the single directory name used for repository-local configuration, ignore patterns, and embedded database files. The value must be one path component. |
| `CGC_INSTANCE_NAME` | `CodeGraphContext` | Selects the MCP registration key and the server name shown to MCP and HTTP clients. Choose a name distinct from upstream. |

When an isolation variable is set, `cgc mcp setup` writes the configured values into the generated MCP entry and uses the Python interpreter that ran setup. It also merges that entry into an existing project `mcp.json`, preserving other server entries. Supported IDE files are still updated by the setup wizard; review the generated entry before enabling it in another client.

For the fork's CLI, use the same environment with an explicit interpreter:

```bash
CGC_HOME="$HOME/.codegraphcontext-fork" \
CGC_REPO_CONTEXT_DIR=".codegraphcontext-fork" \
CGC_INSTANCE_NAME="CodeGraphContext Fork" \
"$HOME/.venvs/codegraphcontext-fork/bin/python" -m codegraphcontext config show
```

If the fork indexes a repository in per-repo mode, it uses `<repo>/.codegraphcontext-fork/` while upstream keeps using `<repo>/.codegraphcontext/`. Add the selected folder to that repository's `.gitignore` if the generated database and settings should stay local. The repository-root `.env` remains a shared project file; set `CGC_IGNORE_PROJECT_ENV=1` when the fork should not load it.

`CGC_RUNTIME_DB_PATH` remains an explicit override for the global database path. The more specific `FALKORDB_PATH`, `FALKORDB_SOCKET_PATH`, `KUZUDB_PATH`, and `LADYBUGDB_PATH` values can also select individual backend paths.

These path settings isolate CodeGraphContext-owned files. They do not create or isolate remote database instances. If both installations connect to the same Neo4j, Nornic, or remote FalkorDB service, configure distinct `NEO4J_DATABASE`, `NORNIC_DATABASE`, or `FALKORDB_GRAPH_NAME` values (or use separate endpoints) before indexing. This guide does not connect to remote databases.

## HTTP API instances

Both instances default to API port `8000`. Start the fork on a separate available port and use that port in its client configuration:

```bash
CGC_HOME="$HOME/.codegraphcontext-fork" \
CGC_REPO_CONTEXT_DIR=".codegraphcontext-fork" \
CGC_INSTANCE_NAME="CodeGraphContext Fork" \
"$HOME/.venvs/codegraphcontext-fork/bin/cgc" api start --port 8001
```

The HTTP API title and MCP server identity use `CGC_INSTANCE_NAME`; the port remains controlled by `cgc api start --port`.

The checked-in Docker and Kubernetes examples still use upstream image, resource, and volume names. For parallel deployments, use separate Docker volumes and a separate Kubernetes namespace; no container image or release identity is being changed by this runtime-path work.

## Existing settings and graph data

No settings or databases are migrated automatically. With no `CGC_HOME`, existing installs keep using `~/.codegraphcontext`; setting a new root starts with that root's defaults and does not read or copy the old root. Re-enter only the settings needed for the isolated instance and reindex repositories into its chosen graph store.

Likewise, changing `CGC_REPO_CONTEXT_DIR` leaves an existing `.codegraphcontext/` directory untouched and selects a new local directory. Reindex into the new directory if a separate graph is needed. Keep the old directory until you have checked the new index; do not copy embedded database files across backend or application versions as a migration method.

The package resolver also reads shared language-package locations such as the current repository's `node_modules`, the global npm root, `~/.m2/repository`, and `~/.pub-cache`. These are lookup sources for dependency resolution, not CodeGraphContext graph or configuration stores. Parser and query caches are process-local; no additional persistent analysis-cache root is configured here.
