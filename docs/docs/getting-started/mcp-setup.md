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
Add the following key under the `mcpServers` object:

```json
{
  "mcpServers": {
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

*Note: The `codegraphcontext-fork` PyPI package is currently unpublished. Use the absolute path to the `cgc` executable in your dedicated virtual environment until the 0.7.0 publication.*

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
