import asyncio
import importlib
import json
import sys
import types

from codegraphcontext.api.schemas import QueryRequest
from codegraphcontext.server import MAX_TOOL_RESPONSE_BYTES, _apply_final_output_limits

import pytest
from fastapi import HTTPException


class FakeServer:
    def __init__(self):
        self.tool_name = None
        self.arguments = None

    async def handle_tool_call(self, tool_name, arguments):
        self.tool_name = tool_name
        self.arguments = arguments
        return {"rows": [{"count": 1}]}


def test_execute_query_passes_cypher_query_argument(monkeypatch):
    server_module = types.ModuleType("codegraphcontext.server")
    server_module.MCPServer = object
    server_module._apply_final_output_limits = _apply_final_output_limits
    monkeypatch.setitem(sys.modules, "codegraphcontext.server", server_module)

    router_module = importlib.import_module("codegraphcontext.api.router")

    server = FakeServer()
    request = QueryRequest(
        query="MATCH (n) RETURN count(n) AS count",
        params={"limit": 10},
    )

    response = asyncio.run(router_module.execute_query(request, server=server))

    assert response.status == "ok"
    assert response.data == {"rows": [{"count": 1}]}
    assert server.tool_name == "execute_cypher_query"
    assert server.arguments == {
        "cypher_query": "MATCH (n) RETURN count(n) AS count",
        "params": {"limit": 10},
    }


def test_execute_query_forwards_page_window(monkeypatch):
    server_module = types.ModuleType("codegraphcontext.server")
    server_module.MCPServer = object
    server_module._apply_final_output_limits = _apply_final_output_limits
    monkeypatch.setitem(sys.modules, "codegraphcontext.server", server_module)
    router_module = importlib.import_module("codegraphcontext.api.router")

    server = FakeServer()
    request = QueryRequest(
        query="MATCH (n:Function) RETURN n.uid ORDER BY n.uid",
        params={},
        offset=5,
        page_size=7,
    )

    response = asyncio.run(router_module.execute_query(request, server=server))

    assert response.status == "ok"
    assert server.arguments == {
        "cypher_query": "MATCH (n:Function) RETURN n.uid ORDER BY n.uid",
        "params": {},
        "offset": 5,
        "page_size": 7,
    }


def test_execute_query_forwards_repository_scope_options(monkeypatch):
    server_module = types.ModuleType("codegraphcontext.server")
    server_module.MCPServer = object
    server_module._apply_final_output_limits = _apply_final_output_limits
    monkeypatch.setitem(sys.modules, "codegraphcontext.server", server_module)
    router_module = importlib.import_module("codegraphcontext.api.router")

    server = FakeServer()
    request = QueryRequest(
        query="MATCH (f:Function) RETURN f.uid ORDER BY f.uid",
        params={},
        repo_path="/workspace/repo",
        global_scope=True,
    )

    response = asyncio.run(router_module.execute_query(request, server=server))

    assert response.status == "ok"
    assert server.arguments == {
        "cypher_query": "MATCH (f:Function) RETURN f.uid ORDER BY f.uid",
        "params": {},
        "repo_path": "/workspace/repo",
        "global_scope": True,
    }


def test_rest_tool_call_limits_the_wrapped_api_response(monkeypatch):
    server_module = types.ModuleType("codegraphcontext.server")
    server_module.MCPServer = object
    server_module._apply_final_output_limits = _apply_final_output_limits
    monkeypatch.setitem(sys.modules, "codegraphcontext.server", server_module)
    router_module = importlib.import_module("codegraphcontext.api.router")

    class LargeServer:
        async def handle_tool_call(self, tool_name, arguments):
            return {"rows": [{"value": "x" * (MAX_TOOL_RESPONSE_BYTES * 2)}]}

    response = asyncio.run(
        router_module.call_tool(
            router_module.ToolCallRequest(name="execute_cypher_query", arguments={}),
            server=LargeServer(),
        )
    )

    assert response.status == "ok"
    assert response.data["truncated"] is True
    encoded = json.dumps(
        {
            "status": response.status,
            "message": response.message,
            "data": response.data,
            "error": response.error,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) <= MAX_TOOL_RESPONSE_BYTES


def test_rest_repositories_limits_the_wrapped_api_response(monkeypatch):
    server_module = types.ModuleType("codegraphcontext.server")
    server_module.MCPServer = object
    server_module._apply_final_output_limits = _apply_final_output_limits
    monkeypatch.setitem(sys.modules, "codegraphcontext.server", server_module)
    router_module = importlib.import_module("codegraphcontext.api.router")

    class LargeRepositoryServer:
        async def handle_tool_call(self, tool_name, arguments):
            return {
                "repositories": [
                    {"path": f"/workspace/repo-{index}", "description": "x" * 4096}
                    for index in range(40)
                ]
            }

    response = asyncio.run(
        router_module.list_repositories(server=LargeRepositoryServer())
    )

    assert response.status == "ok"
    assert response.data["truncated"] is True
    encoded = json.dumps(
        {
            "status": response.status,
            "message": response.message,
            "data": response.data,
            "error": response.error,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) <= MAX_TOOL_RESPONSE_BYTES


def test_execute_query_limits_the_wrapped_api_response(monkeypatch):
    server_module = types.ModuleType("codegraphcontext.server")
    server_module.MCPServer = object
    server_module._apply_final_output_limits = _apply_final_output_limits
    monkeypatch.setitem(sys.modules, "codegraphcontext.server", server_module)
    router_module = importlib.import_module("codegraphcontext.api.router")

    class LargeServer:
        async def handle_tool_call(self, tool_name, arguments):
            return {"rows": [{"value": "x" * (MAX_TOOL_RESPONSE_BYTES * 2)}]}

    response = asyncio.run(
        router_module.execute_query(
            QueryRequest(query="MATCH (n) RETURN n", params={}),
            server=LargeServer(),
        )
    )

    assert response.status == "ok"
    assert response.data["truncated"] is True
    encoded = json.dumps(
        {
            "status": response.status,
            "message": response.message,
            "data": response.data,
            "error": response.error,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) <= MAX_TOOL_RESPONSE_BYTES


@pytest.mark.parametrize("route", ["tools_call", "query"])
def test_rest_error_response_limits_the_wrapped_api_response(monkeypatch, route):
    server_module = types.ModuleType("codegraphcontext.server")
    server_module.MCPServer = object
    server_module._apply_final_output_limits = _apply_final_output_limits
    monkeypatch.setitem(sys.modules, "codegraphcontext.server", server_module)
    router_module = importlib.import_module("codegraphcontext.api.router")

    class LargeErrorServer:
        async def handle_tool_call(self, tool_name, arguments):
            if tool_name.startswith("unknown_tool"):
                return {"error": f"Unknown tool: {tool_name}"}
            return {"error": "query failed: " + "x" * (MAX_TOOL_RESPONSE_BYTES * 2)}

    server = LargeErrorServer()
    if route == "tools_call":
        response = asyncio.run(
            router_module.call_tool(
                router_module.ToolCallRequest(
                    name="unknown_tool" + "x" * (MAX_TOOL_RESPONSE_BYTES * 2),
                    arguments={},
                ),
                server=server,
            )
        )
    else:
        response = asyncio.run(
            router_module.execute_query(
                QueryRequest(query="MATCH (n) RETURN n", params={}),
                server=server,
            )
        )

    assert response.status == "error"
    assert response.data["truncated"] is True
    encoded = json.dumps(
        {
            "status": response.status,
            "message": response.message,
            "data": response.data,
            "error": response.error,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) <= MAX_TOOL_RESPONSE_BYTES


class FailingServer:
    async def handle_tool_call(self, tool_name, arguments):
        raise OSError("connection refused")


def test_execute_query_returns_503_when_database_unavailable(
    monkeypatch,
):
    server_module = types.ModuleType(
        "codegraphcontext.server"
    )
    server_module.MCPServer = object
    server_module._apply_final_output_limits = _apply_final_output_limits

    monkeypatch.setitem(
        sys.modules,
        "codegraphcontext.server",
        server_module,
    )

    router_module = importlib.import_module(
        "codegraphcontext.api.router"
    )

    request = QueryRequest(
        query="MATCH (n) RETURN count(n) AS count",
        params={},
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            router_module.execute_query(
                request,
                server=FailingServer(),
            )
        )

    assert exc.value.status_code == 503
    assert exc.value.detail == "Database service unavailable"
