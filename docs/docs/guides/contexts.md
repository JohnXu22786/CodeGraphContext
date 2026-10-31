# Configuration Contexts & Workspaces

CodeGraphContext (CGC) uses a context resolution system to determine where graph database files are stored and resolved. This allows developers to isolate codebases, use named workspaces, or share a single global database.

---

## Workspace Directory Structure

Below is the standard directory structure under global and local scopes:

```text
${CGC_HOME:-~/.codegraphcontext}/ <-- Global configuration directory
    config.yaml                 <-- Active context mode and registry
    .env                        <-- Database credentials and tuning configurations
    global/
        .cgcignore              <-- Global ignore patterns
        db/
            ladybugdb/          <-- Global-mode default embedded storage
            kuzudb/             <-- Global-mode KuzuDB storage directory
    contexts/
        ProjectA/
            db/
                ladybugdb/     <-- Named-context default storage directory
            .cgcignore          <-- Context-specific ignore patterns
```

---

## Context Resolution Precedence

When executing a CLI command (e.g., `cgc index`) or starting an MCP session, CGC resolves the target database location in this priority order:

1. **Context Override Flag**: If `--context <name>` or `-c <name>` is provided, CGC routes all writes and queries to the specified named context.
2. **Local Repository Scope**: If the current directory contains the selected repository-context folder (default `.codegraphcontext`), CGC operates in per-repo mode.
3. **Global Config Setting**: CGC reads the active mode (`global`, `per-repo`, or `named`) and default context name specified in `CGC_HOME/config.yaml` (default `~/.codegraphcontext/config.yaml`).
4. **Default Fallback**: Uses the included LadybugDB backend under `CGC_HOME/global/db/ladybugdb/`. Install and select an optional backend extra to use FalkorDB Lite, KùzuDB, or a remote database.

---

## Context Modes

### 1. Global Mode (Default)

In Global Mode, all indexed repositories populate a single shared database.

```bash
# Verify active mode settings
cgc context list

# Set mode to global
cgc context mode global
```

When indexing multiple repositories, their nodes are ingested into the same graph structure, which enables cross-project relationship tracing:

```bash
cd ~/projects/service-api
cgc index .

cd ~/projects/service-gateway
cgc index .

# List all ingested repositories
cgc list
```

---

### 2. Per-Repo Mode

In Per-Repo Mode, each repository maintains its own local context directory (default `.codegraphcontext`, similar to how Git uses `.git/`). Set `CGC_REPO_CONTEXT_DIR` to a different one-component name when two installations need separate local graphs in the same repository.

```bash
cgc context mode per-repo
```

When indexing inside a project, a local database folder is created within the repository root:

```bash
cd ~/projects/service-api
cgc index .
# Creates: ~/projects/service-api/.codegraphcontext/db/ladybugdb/ (or the selected CGC_REPO_CONTEXT_DIR)
```

Graphs are completely isolated, and commands run within a repository only inspect the local database.

When you first index in per-repo mode, CGC auto-creates the selected repository-context directory and seeds a local `config.yaml` from your global `DEFAULT_DATABASE`. Project-local `<CGC_REPO_CONTEXT_DIR>/.env` and `.env` files are loaded **only in this mode** (unless you set `CGC_LOAD_PROJECT_ENV=1`). In global or named mode, `CGC_HOME/.env` wins so cloned repos cannot override your credentials. The default roots remain `~/.codegraphcontext` and `.codegraphcontext`.

---

### 3. Named Context Mode

Named contexts act as logical workspaces. You can assign a specific name (e.g., `ClientA`, `StagingGraph`) and associate multiple codebases with it.

```bash
# Switch to named context mode
cgc context mode named

# Create a named context
cgc context create ProjectA

# Index codebases into the named context
cgc index ~/projects/api --context ProjectA
cgc index ~/projects/web --context ProjectA
```

Setting a default context name eliminates the need to pass the `--context` flag:

```bash
cgc context default ProjectA

# Future commands use the default named context
cgc list
cgc stats
```

---

## Managing Named Contexts via CLI

### Create a Named Context
Create a context and optionally specify its target database driver and storage path:
```bash
python -m pip install 'codegraphcontext-fork[kuzu]'
cgc context create mobile-app --database kuzudb   # Or use shorthand aliases: --db, -db, -d
cgc context create mobile-app --db-path /mnt/fast/cgc
```

### List Contexts
Displays active modes, registered contexts, database backend configurations, and associated repository directories:
```bash
cgc context list
```

### Delete a Context
Deletes the named context from the active registry:
```bash
cgc context delete mobile-app
```
*Note: Deleting a context removes its registration from `config.yaml`. The underlying database files on disk are preserved to prevent data loss. You can delete the files manually if needed.*

---

## Ingest Ignore Configurations (`.cgcignore`)

CGC filters files using `.cgcignore` config files. The location of the active ignore file depends on the context mode:

| Mode | Active `.cgcignore` Path |
| :--- | :--- |
| **Global** | `CGC_HOME/global/.cgcignore` (default `~/.codegraphcontext/global/.cgcignore`) |
| **Per-Repo** | `<repo_root>/<CGC_REPO_CONTEXT_DIR>/.cgcignore` (default `<repo_root>/.codegraphcontext/.cgcignore`) |
| **Named** | `CGC_HOME/contexts/<name>/.cgcignore` (default `~/.codegraphcontext/contexts/<name>/.cgcignore`) |

### Default Global `.cgcignore` Template

```text
node_modules/
venv/
.venv/
dist/
build/
__pycache__/
*.pyc
.git/
.idea/
.vscode/
```
