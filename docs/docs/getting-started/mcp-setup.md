# Model Context Protocol Setup

CodeGraphContext (CGC) implements the Model Context Protocol (MCP). This enables LLM-powered applications and IDE extensions to discover and invoke tools that fetch context directly from your code graph.

---

## 1. Automated Setup (Recommended)

CGC includes an interactive wizard that detects supported IDEs and applications on your system and configures their MCP client settings automatically.

Run the wizard from your terminal:

```bash
cgc mcp setup
```

The wizard will locate configuration files for Claude Desktop, Cursor, and other compatible environments, and request permission to add CodeGraphContext as a local tool provider.

---

## 2. Manual Client Configuration

If you prefer to configure your workspace manually, refer to the client configurations below.

MCP clients start the configured executable directly and do not inherit an activated virtual environment from your terminal. The `pipx` examples below will work after `codegraphcontext-fork` 0.7.0 is published to PyPI. Until then, install the checkout in a dedicated virtual environment and use the absolute path to its `cgc` executable in the client configuration. The client must be able to find `pipx` on its own `PATH` after publication.

### Claude Desktop

To configure Claude Desktop to run the local CGC server, add a configuration entry to the `claude_desktop_config.json` file.

#### Configuration File Locations:
- **Linux**: `~/.config/Claude/claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

#### Configuration Schema:
Back up your existing configuration first, then merge this server entry under its `mcpServers` object. Do not replace the rest of the file. A copy of the same entry is available at [`claude-desktop-config.json`](../examples/claude-desktop-config.json).

```json
{
  "mcpServers": {
    "codegraphcontext-fork": {
      "command": "/absolute/path/to/cgc",
      "args": ["mcp", "start"],
      "env": {
        "CGC_HOME": "/absolute/path/to/cgc-home",
        "CGC_REPO_CONTEXT_DIR": ".codegraphcontext-fork",
        "CGC_INSTANCE_NAME": "codegraphcontext-fork"
      }
    }
  }
}
```

Replace `/absolute/path/to/cgc` with the `cgc` executable in the dedicated environment used for this fork (`/path/to/.venv/bin/cgc` on POSIX or `C:/path/to/.venv/Scripts/cgc.exe` on Windows, using an absolute path). Forward slashes keep the Windows path valid as a JSON string; if you use backslashes, double each one for JSON. Choose a new absolute `CGC_HOME` outside the source repository. The MCP server key and `CGC_INSTANCE_NAME` must match. These settings isolate CGC runtime files; they do not make the old `.cg/cg.sqlite` index compatible or create a DSH plugin.

Restart Claude Desktop to load the entry. To verify the connection, call `list_indexed_repositories`; a new `CGC_HOME` starts with separate state, so its list may be empty. Before using `find_code`, index the source repository into this CGC state with the same `CGC_HOME`, then query that repository. Existing indexes under another CGC home are not visible. To roll back, remove only the `codegraphcontext-fork` entry you added and restart the client; keep the backup until the new entry works.

### Legacy CodeGraph or DSH users

This configuration starts CGC's generic stdio MCP server. It does not expose the legacy `codegraph_*` aliases or install the old Node/Cordis plugin. Review [Migrate from legacy CodeGraph](../guides/migration-from-codegraph.md) before replacing an old client entry; it lists the verified tool comparisons and the operations that require prompt or workflow changes. If your DSH installation accepts generic stdio MCP servers, use its own documented config format and the same executable, arguments, and environment values. Otherwise a separately maintained bridge is required.

---

### Cursor IDE

Cursor supports local MCP servers via direct process execution:

1. Open **Cursor Settings** (Preferences / Settings -> Features -> MCP).
2. Click **+ Add New MCP Server**.
3. Fill in the fields:
   - **Name**: `CodeGraphContext`
   - **Type**: `command`
   - **Command**: `pipx`
   - **Arguments**: `run --spec codegraphcontext-fork codegraphcontext mcp start`
4. Click **Save**.

---

### OpenCode

OpenCode manages MCP in its own UI. Follow the vendor's guide at [OpenCode MCP servers](https://opencode.ai/docs/ko/mcp-servers/#_top) to register a stdio server.

Use the following configuration details:
- **Type**: `stdio`
- **Command**: `pipx`
- **Arguments**: `run --spec codegraphcontext-fork codegraphcontext mcp start`

*Note: Ensure your database credentials and configurations match what `cgc mcp setup` generated (in `CGC_HOME/.env`, default `~/.codegraphcontext/.env`).*

---

### VS Code (via Continue extension)

If you use VS Code with the [Continue.dev](https://continue.dev) plugin:

1. Open your Continue configuration file (`~/.continue/config.json`).
2. Add the server details inside the `contextProviders` or `mcp` settings array:

```json
{
  "mcp": {
    "codegraphcontext": {
      "command": "pipx",
      "args": [
        "run",
        "--spec",
        "codegraphcontext-fork",
        "codegraphcontext",
        "mcp",
        "start"
      ]
    }
  }
}
```

---

## 3. Verifying Tool Connectivity

After restarting your IDE or Claude Desktop app, verify that the 29 MCP tools are active. You should see commands like:

- `find_code` (Keyword search across symbols and file contents)
- `analyze_code_relationships` (Lookup callers, callees, and inheritance paths)
- `execute_cypher_query` (Execute direct database Cypher statements)

You can verify it by prompting the assistant:
> "Analyze the call path between the `process_data` and `db_commit` functions in my current codebase."

---

## 4. Connection Troubleshooting

If the tools do not load:
1. **Command Resolution**: The client does not inherit your terminal's active `.venv`. Use an absolute path to the executable in that environment (for example, `/path/to/project/.venv/bin/cgc` or `C:\\path\\to\\project\\.venv\\Scripts\\cgc.exe`). After 0.7.0 is published, you can use the `pipx` configurations above if `pipx` is on the client's own `PATH`.
2. **Process Integrity**: Test the configured command and arguments from the client's environment. It should listen on standard input/output (stdin/stdout) for JSON-RPC messages and not exit immediately.
3. **Database Selection**: Ensure your default database is configured and has indexed data. Run `cgc doctor` to verify configuration.
