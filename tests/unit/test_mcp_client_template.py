"""Contract tests for the checked-in Claude Desktop MCP configuration example."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "docs" / "docs" / "examples" / "claude-desktop-config.json"


def test_claude_desktop_template_uses_cgc_stdio_entry():
    """Prevent drift from the client JSON schema and CGC stdio startup command."""
    config = json.loads(TEMPLATE.read_text(encoding="utf-8"))

    assert set(config) == {"mcpServers"}
    assert set(config["mcpServers"]) == {"codegraphcontext-fork"}
    server = config["mcpServers"]["codegraphcontext-fork"]
    assert server["command"] == "/absolute/path/to/cgc"
    assert server["args"] == ["mcp", "start"]
    assert set(server["env"]) == {
        "CGC_HOME",
        "CGC_REPO_CONTEXT_DIR",
        "CGC_INSTANCE_NAME",
    }


def test_claude_desktop_template_uses_isolated_state_without_legacy_aliases():
    """Keep the example isolated and avoid promising legacy tool-name parity."""
    config = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    server_name, server = next(iter(config["mcpServers"].items()))

    assert server_name == server["env"]["CGC_INSTANCE_NAME"]
    assert server["env"]["CGC_HOME"] == "/absolute/path/to/cgc-home"
    assert server["env"]["CGC_REPO_CONTEXT_DIR"] == ".codegraphcontext-fork"
    assert not {"NEO4J_PASSWORD", "API_KEY", "TOKEN"} & set(server["env"])
    assert not any(name.startswith("codegraph_") for name in server)
