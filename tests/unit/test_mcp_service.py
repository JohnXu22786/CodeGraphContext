"""Shared MCP dispatch contract across stdio and Streamable HTTP."""
import asyncio
import io
import json
import re
import sys
import threading
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


def test_stdio_cancellation_tracking_does_not_require_task_cancelling_method():
    class Python310Task:
        def __init__(self):
            self.cancel_calls = 0

        def done(self):
            return False

        def cancel(self):
            self.cancel_calls += 1
            return True

    task = Python310Task()
    active_requests = {"request-1": task}
    cancelling_requests = set()

    server_module._cancel_stdio_tool_request(
        "request-1", active_requests, cancelling_requests
    )
    server_module._cancel_stdio_tool_request(
        "request-1", active_requests, cancelling_requests
    )

    assert task.cancel_calls == 1
    assert cancelling_requests == {"request-1"}


@pytest.mark.asyncio
async def test_stdio_cancellation_notification_cancels_active_tool_request(monkeypatch):
    handler_started = threading.Event()
    release_handler = threading.Event()
    allow_notification = threading.Event()
    cancellation_started = threading.Event()
    allow_repeat_notification = threading.Event()
    allow_cancel_cleanup = asyncio.Event()
    job_cancelled = threading.Event()

    class CancellationInput:
        def __init__(self):
            self._read_count = 0

        def readline(self):
            self._read_count += 1
            if self._read_count == 1:
                return json.dumps(_call_body("add_code_to_graph", {})) + "\n"
            if self._read_count == 2:
                assert allow_notification.wait(timeout=5), "test did not release cancellation notification"
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": 1, "reason": "test cancellation"},
                    }
                ) + "\n"
            if self._read_count == 3:
                assert allow_repeat_notification.wait(timeout=5), "test did not release repeated cancellation notification"
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": 1, "reason": "repeated test cancellation"},
                    }
                ) + "\n"
            return ""

    class JobManager:
        async def cancel_job_async(self, job_id):
            assert job_id == "job-1"
            cancellation_started.set()
            await allow_cancel_cleanup.wait()
            job_cancelled.set()
            return {"success": True, "cancelled": True}

    def add_code_to_graph_tool(**_args):
        handler_started.set()
        assert release_handler.wait(timeout=5), "test did not release the blocking tool handler"
        return {"success": True, "job_id": "job-1"}

    server = _server()
    server.add_code_to_graph_tool = add_code_to_graph_tool
    server.job_manager = JobManager()
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", CancellationInput())
    monkeypatch.setattr(sys, "stdout", stdout)

    run_task = asyncio.create_task(MCPServer._run_loop(server, asyncio.get_running_loop()))
    try:
        assert await asyncio.to_thread(handler_started.wait, 5), "tool handler did not start"
        allow_notification.set()
        await asyncio.sleep(0.05)
        release_handler.set()
        assert await asyncio.to_thread(cancellation_started.wait, 5), "request cancellation did not reach its indexing job"
        allow_repeat_notification.set()
        await asyncio.sleep(0.05)
    finally:
        allow_cancel_cleanup.set()
        release_handler.set()

    await run_task
    assert job_cancelled.is_set(), "stdio cancellation did not cancel the scheduled indexing job"
    responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.startswith("{")]
    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32800, "message": "Request cancelled"},
        }
    ]


@pytest.mark.asyncio
async def test_context_switch_waits_until_indexing_registers_job(monkeypatch):
    from codegraphcontext.core.jobs import JobManager

    server = _server()
    jobs = JobManager()
    server.job_manager = jobs
    server.loop = asyncio.get_running_loop()
    server._graph_builder = object()
    server.list_indexed_repositories_tool = Mock(return_value={"repositories": []})
    server._context_transition_lock = threading.RLock()
    server._stop_current_watcher = Mock(return_value=False)
    server._close_db_manager_for_context_switch = Mock(
        return_value={"error": "context switch passed the active-job check"}
    )
    handler_started = threading.Event()
    release_handler = threading.Event()

    def prepare_index(_graph_builder, job_manager, _loop, _list_repos_func, **_args):
        handler_started.set()
        assert release_handler.wait(timeout=2)
        job_id = job_manager.create_job("/tmp/index-request")
        return {"success": True, "job_id": job_id}

    monkeypatch.setattr(server_module.indexing_handlers, "add_code_to_graph", prepare_index)
    index_request = asyncio.create_task(
        server.handle_tool_call("add_code_to_graph", {"path": "/tmp/index-request"})
    )
    assert await asyncio.to_thread(handler_started.wait, 2)

    switch_request = asyncio.create_task(
        server.handle_tool_call("switch_context", {"context_path": "global"})
    )
    await asyncio.sleep(0.05)
    switch_waited_for_registration = not switch_request.done()
    release_handler.set()
    index_result = await index_request
    switch_result = await switch_request

    assert switch_waited_for_registration
    assert switch_result["active_jobs"][0]["job_id"] == index_result["job_id"]


@pytest.mark.asyncio
async def test_local_update_registers_job_under_context_transition_gate(monkeypatch, tmp_path):
    from codegraphcontext.api import router
    from codegraphcontext.core.jobs import JobManager

    server = _server()
    jobs = JobManager()
    server.job_manager = jobs
    server.loop = asyncio.get_running_loop()
    server._graph_builder = object()
    server.list_indexed_repositories_tool = Mock(return_value={"repositories": []})
    server._context_transition_lock = threading.RLock()
    server._stop_current_watcher = Mock(return_value=False)
    server._close_db_manager_for_context_switch = Mock(
        return_value={"error": "context switch passed the active-job check"}
    )
    handler_started = threading.Event()
    release_handler = threading.Event()

    def prepare_update(*_args):
        handler_started.set()
        assert release_handler.wait(timeout=2)
        return 4, 0.25

    monkeypatch.setattr(router, "_prepare_local_update", prepare_update)
    update_request = asyncio.create_task(
        router._prepare_local_update_and_create_job_async(
            server, tmp_path, ".cgcignore", timeout_seconds=None
        )
    )
    assert await asyncio.to_thread(handler_started.wait, 2)

    switch_request = asyncio.create_task(
        server.handle_tool_call("switch_context", {"context_path": "global"})
    )
    await asyncio.sleep(0.05)
    switch_waited_for_registration = not switch_request.done()
    release_handler.set()
    update_result = await update_request
    switch_result = await switch_request

    assert switch_waited_for_registration
    assert switch_result["active_jobs"][0]["job_id"] == update_result[2]
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
async def test_stdio_returns_response_for_an_explicit_null_request_id(monkeypatch):
    server = _server()

    response = await _stdio_request(
        server,
        monkeypatch,
        {"jsonrpc": "2.0", "id": None, "method": "tools/list"},
    )

    assert response["id"] is None
    assert "result" in response


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
    assert len(expected_names) == 30
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
        assert "30" in content
        assert "0.4.16" not in content
        assert "0.1.0" in content
        assert "distribution" in content.lower() or "package release" in content.lower()

    catalog = (repo_root / "docs/MCP_TOOLS.md").read_text(encoding="utf-8")
    documented_name_list = re.findall(r"(?m)^### `([^`]+)`$", catalog)
    assert len(documented_name_list) == len(set(documented_name_list))
    documented_names = set(documented_name_list)
    registered_names = set(TOOLS)
    assert documented_names == registered_names
