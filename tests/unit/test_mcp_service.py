"""Shared MCP dispatch contract across stdio and Streamable HTTP."""
import asyncio
import io
import json
import re
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from codegraphcontext import server as server_module
from codegraphcontext.mcp_service import MCP_SERVER_VERSION
from codegraphcontext.server import MCPServer
from codegraphcontext.tool_definitions import TOOLS


def _server(handler=None):
    """Build a dispatcher with the real registered tools but no database."""
    server = MCPServer.__new__(MCPServer)
    server.disabled_tools = set()
    server._context_note_pending = False
    server.tools = {"list_jobs": TOOLS["list_jobs"]}
    server.list_jobs_tool = handler or Mock(return_value={"ok": True})
    return server


def _call_body(name, arguments):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }


async def _stdio_request(server, monkeypatch, body):
    stdin = io.StringIO(json.dumps(body, ensure_ascii=False) + "\n")
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(server_module, "build_system_prompt", lambda: "test prompt")
    monkeypatch.setattr(server_module, "get_instance_name", lambda: "test-server")

    await MCPServer._run_loop(server, asyncio.get_running_loop())
    responses = [line for line in stdout.getvalue().splitlines() if line.startswith("{")]
    assert responses, "stdio emitted no JSON-RPC response"
    return json.loads(responses[-1])


async def _http_request(server, body):
    from codegraphcontext.api.app import create_app

    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=server):
        app = create_app()
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                headers = {
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": body["method"],
                }
                tool_name = body.get("params", {}).get("name")
                if tool_name:
                    headers["Mcp-Name"] = tool_name
                return await client.post("/api/v1/mcp", json=body, headers=headers)


@pytest.mark.asyncio
async def test_registered_tool_dispatch_and_utf8_payload_match_across_transports(monkeypatch):
    monkeypatch.delenv("CGC_OUTPUT_FORMAT", raising=False)
    payload = {"status": "ok", "message": "café 🧭 路径", "path": "/workspace/sample.py"}
    handler = Mock(return_value=payload)
    server = _server(handler)
    args = {"query": "café"}

    stdio = await _stdio_request(server, monkeypatch, _call_body("list_jobs", args))
    http = await _http_request(server, _call_body("list_jobs", args))

    assert "result" in stdio
    http_result = http.json()["result"]
    assert http_result["isError"] is False
    stdio_text = stdio["result"]["content"][0]["text"]
    http_text = http_result["content"][0]["text"]
    expected = {"status": "ok", "message": "café 🧭 路径", "path": "sample.py"}
    assert stdio_text == http_text
    assert json.loads(stdio_text) == expected
    assert json.loads(http_text) == expected
    assert handler.call_count == 2
    assert all(call.kwargs == args for call in handler.call_args_list)


@pytest.mark.asyncio
async def test_unknown_tool_uses_shared_error_payload_and_transport_envelopes(monkeypatch):
    server = _server()
    body = _call_body("not_registered", {})

    stdio = await _stdio_request(server, monkeypatch, body)
    http = await _http_request(server, body)

    assert stdio["error"]["code"] == -32000
    assert stdio["error"]["data"] == {"error": "Unknown tool: not_registered"}
    http_result = http.json()["result"]
    assert http_result["isError"] is True
    assert http_result["content"][0]["text"] == "Error: Unknown tool: not_registered"


@pytest.mark.asyncio
async def test_non_object_arguments_are_rejected_before_dispatch(monkeypatch):
    handler = Mock(return_value={"unexpected": True})
    server = _server(handler)
    body = _call_body("list_jobs", "not-an-object")

    stdio = await _stdio_request(server, monkeypatch, body)
    http = await _http_request(server, body)

    assert stdio["error"]["code"] == -32602
    assert "arguments" in stdio["error"]["message"].lower()
    assert handler.call_count == 0
    assert http.status_code == 400
    assert http.json()["error"]["code"] == -32602
    assert handler.call_count == 0


@pytest.mark.asyncio
async def test_handler_exception_keeps_internal_error_envelope(monkeypatch):
    handler = Mock(side_effect=RuntimeError("database failed"))
    server = _server(handler)
    body = _call_body("list_jobs", {})

    stdio = await _stdio_request(server, monkeypatch, body)
    http = await _http_request(server, body)

    assert stdio["error"]["code"] == -32603
    assert "database failed" in stdio["error"]["message"]
    assert http.status_code == 200
    http_error = http.json()["error"]
    assert http_error["code"] == -32603
    assert http_error["message"] == "Internal server error"
    assert "database failed" not in http_error["message"]


@pytest.mark.asyncio
async def test_default_tool_catalog_matches_exactly_across_transports(monkeypatch):
    server = _server()
    server.tools = TOOLS

    stdio = await _stdio_request(server, monkeypatch, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    http = await _http_request(
        server,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        }},
    )

    expected_names = set(TOOLS)
    stdio_names = {tool["name"] for tool in stdio["result"]["tools"]}
    http_names = {tool["name"] for tool in http.json()["result"]["tools"]}
    assert len(expected_names) == 29
    assert stdio_names == expected_names
    assert http_names == expected_names


@pytest.mark.asyncio
async def test_stdio_initialize_uses_server_metadata_version_separate_from_distribution(monkeypatch):
    server = _server()
    response = await _stdio_request(
        server,
        monkeypatch,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            },
        },
    )

    assert response["result"]["serverInfo"]["version"] == MCP_SERVER_VERSION
    project = Path(__file__).resolve().parents[2] / "pyproject.toml"
    project_metadata = project.read_text(encoding="utf-8").split("[project]", 1)[1].split("\n[", 1)[0]
    version_match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', project_metadata)
    assert version_match, "the Python distribution version should come from project metadata"
    distribution_version = version_match.group(1)
    assert distribution_version != MCP_SERVER_VERSION


def test_mcp_docs_match_catalog_and_keep_server_version_distinct():
    docs = [
        "docs/docs/reference/mcp.md",
        "docs/MCP_TOOLS.md",
    ]
    repo_root = Path(__file__).resolve().parents[2]
    for filename in docs:
        content = (repo_root / filename).read_text(encoding="utf-8")
        assert "29" in content
        assert "0.4.16" not in content
        assert "0.1.0" in content
        assert "distribution" in content.lower() or "package release" in content.lower()

    catalog = (repo_root / "docs/MCP_TOOLS.md").read_text(encoding="utf-8")
    documented_names = set(re.findall(r"(?m)^### `([^`]+)`$", catalog))
    registered_names = set(TOOLS)
    assert documented_names == registered_names
