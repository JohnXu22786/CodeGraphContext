# Ingesting & Installing CodeGraphContext

CodeGraphContext (CGC) is packaged as a standard Python utility. The CLI and server components are installed using Python package managers.

---

## 1. CLI Installation

The upstream `codegraphcontext` distribution and this fork’s `codegraphcontext-fork` distribution share the `codegraphcontext` import namespace. Use a dedicated Python environment for this fork; never install both distributions into the same environment. The `uvx` and `pipx` commands below create isolated environments automatically. For runtime state and MCP registration isolation, also follow [Run the fork beside upstream](../guides/side-by-side-installations.md).

The `codegraphcontext-fork` package is not published on PyPI yet. The `uvx`, `pipx`, and `pip` commands on this page will work after the 0.7.0 publication.

### Method A: Execution via `uvx` (Recommended)
If you use [uv](https://github.com/astral-sh/uv), you can run the CGC CLI on demand without installing it globally:

```bash
uvx --from codegraphcontext-fork codegraphcontext --help
```

### Method B: Isolated Global Installation via `pipx`
To install the CLI in an isolated Python environment and make it globally available:

```bash
pipx install codegraphcontext-fork
```

### Method C: Standard Package Installation via `pip`
Create a dedicated virtual environment and install CGC inside it:

The commands below use Bash on macOS/Linux. In PowerShell, activate the environment with `.\.venv\Scripts\Activate.ps1`; in Command Prompt, use `.venv\Scripts\activate.bat`.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install codegraphcontext-fork
```

---

## 2. Database Driver Setup

The default installation includes **LadybugDB**, so an embedded database is ready without extra setup. FalkorDB Lite, KùzuDB, and remote database drivers are optional extras. See [Important defaults](../reference/config.md#important-defaults-read-this-first).

### Installing KuzuDB Drivers
KuzuDB is embedded and runs directly inside the Python process.
```bash
python -m pip install 'codegraphcontext-fork[kuzu]'
```

### Installing FalkorDB Drivers (Optional)
If using the FalkorDB backend:
- **Embedded Lite** (Unix and Python 3.12+ only):
  ```bash
  python -m pip install 'codegraphcontext-fork[falkordb-embedded]'
  ```
- **Remote Server Client**:
  ```bash
  python -m pip install 'codegraphcontext-fork[falkordb-remote]'
  ```

### Installing Neo4j Drivers (Optional)
If connecting to a standalone Neo4j instance:
```bash
python -m pip install 'codegraphcontext-fork[neo4j]'
```

### Installing Data-source Ingestion Drivers (Optional)
For MySQL, Cassandra, or Redis ingestion with `cgc datasource`:
```bash
python -m pip install 'codegraphcontext-fork[datasources]'
```

---

## 3. Configuring the Default Backend

Set your preferred default database backend in the global configuration:

```bash
cgc config db ladybugdb       # LadybugDB (default embedded backend)
cgc config db falkordb        # FalkorDB Lite (requires the falkordb-embedded extra)
cgc config db kuzudb          # KùzuDB (requires the kuzu extra)
cgc config db falkordb-remote # Remote FalkorDB (requires the falkordb-remote extra)
cgc config db neo4j           # Neo4j (requires the neo4j extra)
cgc config db nornic          # Nornic (also requires the neo4j extra)
```

For remote databases (FalkorDB Remote, Neo4j), refer to the database connection properties in the [Configuration Reference](../reference/config.md).

---

## 4. Validating the Installation

Verify that the CLI and its database bindings are correctly loaded using the diagnostics tool:

```bash
# Verify the installed CLI version
cgc version

# Run the system diagnostics check
cgc doctor
```

The `doctor` command executes self-tests on the configuration, tests database drivers, and confirms directory permissions.

---

## 5. Next Steps

Once the CLI is verified, continue to index your project workspace.

**[Proceed to Quickstart →](quickstart.md)**
