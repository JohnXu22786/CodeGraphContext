# tests/unit/api/test_mcp_streamable_http.py
"""MCP endpoint tests (Streamable HTTP, spec 2026-07-28).

The transport is mcp.server.lowlevel.Server.streamable_http_app(), mounted
in api/app.py at /api/v1/mcp behind the same API-key check the REST router
uses. The old two-endpoint HTTP+SSE transport (/api/v1/mcp/sse,
/api/v1/mcp/messages) is kept mounted alongside it for existing client
configs (see docs/INTEGRATION_GUIDE.md); tests below cover both.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import anyio
import httpx
import pytest
import uvicorn
from httpx import ASGITransport, AsyncClient

from codegraphcontext.server import MAX_JSONRPC_ID_BYTES, MAX_TOOL_RESPONSE_BYTES


def _modern_request(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    body_params = dict(params or {})
    body_params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    return {"jsonrpc": "2.0", "method": method, "id": request_id, "params": body_params}


_MODERN_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
}


class _FakeMCPServer:
    """Stand-in for router.get_server(), so tests exercise the real
    on_list_tools/on_call_tool handlers without needing a configured DB."""

    tools = {
        "ping": {"description": "Ping the server", "inputSchema": {"type": "object", "properties": {}}},
    }

    async def handle_tool_call(self, tool_name, args):
        if tool_name == "boom":
            return {"error": "simulated failure"}
        return {"ok": True, "tool": tool_name, "args": args}


@asynccontextmanager
async def _running_client(env=None, monkeypatch=None):
    """Build the HTTP app without opening a native database engine."""
    from codegraphcontext.api import app as app_module
    from codegraphcontext.api.app import create_app

    async def fake_server_startup():
        return SimpleNamespace(db_manager=None)

    monkeypatch.setattr(app_module, "get_server_for_gateway_startup", fake_server_startup)
    monkeypatch.setattr(app_module, "shutdown_server", lambda: None)
    with patch.dict("os.environ", env or {}):
        app = create_app()
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
            ) as client:
                yield client


@pytest.mark.asyncio
async def test_tools_list_returns_real_tools(monkeypatch):
    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=_FakeMCPServer()):
        async with _running_client(monkeypatch=monkeypatch) as client:
            resp = await client.post(
                "/api/v1/mcp",
                json=_modern_request("tools/list"),
                headers={**_MODERN_HEADERS, "Mcp-Method": "tools/list"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    tool_names = [t["name"] for t in body["result"]["tools"]]
    assert tool_names == ["ping"]


@pytest.mark.asyncio
async def test_tools_call_returns_result(monkeypatch):
    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=_FakeMCPServer()):
        async with _running_client(monkeypatch=monkeypatch) as client:
            resp = await client.post(
                "/api/v1/mcp",
                json=_modern_request("tools/call", {"name": "ping", "arguments": {}}),
                headers={**_MODERN_HEADERS, "Mcp-Method": "tools/call", "Mcp-Name": "ping"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["isError"] is False
    assert '"tool": "ping"' in body["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_streamable_http_sse_event_framing_stays_within_response_byte_limit(
    monkeypatch,
):
    from codegraphcontext.mcp_service import MCPToolResult

    async def large_dispatch(_server, _tool_name, _arguments):
        return MCPToolResult(payload={"large": "x" * (MAX_TOOL_RESPONSE_BYTES * 2)})

    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=_FakeMCPServer()), \
         patch("codegraphcontext.api.mcp_sse.dispatch_tool_call", side_effect=large_dispatch):
        async with _running_client(monkeypatch=monkeypatch) as client:
            initialize = await client.post(
                "/api/v1/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )
            session_id = initialize.headers["mcp-session-id"]
            response = await client.post(
                "/api/v1/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "large", "arguments": {}},
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "MCP-Protocol-Version": "2025-06-18",
                    "MCP-Session-Id": session_id,
                },
            )

    assert response.headers["content-type"].startswith("text/event-stream")
    assert len(response.content) <= MAX_TOOL_RESPONSE_BYTES
    event_messages = [
        json.loads(line.removeprefix(b"data: "))
        for line in response.content.splitlines()
        if line.startswith(b"data: ")
    ]
    tool_response = next(message for message in event_messages if message.get("id") == 2)
    assert tool_response["jsonrpc"] == "2.0"
    assert tool_response["result"]["isError"] is False
    limited_text = tool_response["result"]["content"][0]["text"]
    assert json.loads(limited_text)["truncated"] is True


@pytest.mark.asyncio
async def test_streamable_http_rejects_an_oversized_request_id_before_dispatch(monkeypatch):
    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=_FakeMCPServer()):
        async with _running_client(monkeypatch=monkeypatch) as client:
            response = await client.post(
                "/api/v1/mcp",
                json=_modern_request(
                    "tools/call",
                    {"name": "ping", "arguments": {}},
                    request_id="x" * (MAX_JSONRPC_ID_BYTES + 1),
                ),
                headers={**_MODERN_HEADERS, "Mcp-Method": "tools/call", "Mcp-Name": "ping"},
            )

    assert response.status_code == 400
    assert response.json()["id"] is None
    assert response.json()["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_streamable_http_bounds_numeric_id_beyond_python_json_digit_limit(monkeypatch):
    body = (
        b'{"jsonrpc":"2.0","id":'
        + b"9" * 5000
        + b',"method":"tools/list","params":{}}'
    )

    async with _running_client(monkeypatch=monkeypatch) as client:
        response = await client.post(
            "/api/v1/mcp",
            content=body,
            headers={
                **_MODERN_HEADERS,
                "Content-Type": "application/json",
                "Mcp-Method": "tools/list",
            },
        )

    assert response.status_code == 400
    assert len(response.content) <= MAX_TOOL_RESPONSE_BYTES
    assert response.json()["id"] is None
    assert response.json()["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_oversized_request_id_is_rejected_for_a_large_body_before_dispatch():
    from codegraphcontext.api.app import _JsonRpcRequestIdLimitMiddleware

    body = json.dumps({
        "jsonrpc": "2.0",
        "id": "x" * (MAX_JSONRPC_ID_BYTES + 1),
        "method": "tools/call",
        "params": {"padding": "y" * (1024 * 1024 + 1)},
    }).encode()
    body_chunks = [
        {"type": "http.request", "body": body[:512 * 1024], "more_body": True},
        {"type": "http.request", "body": body[512 * 1024:], "more_body": False},
    ]
    receive_calls = 0
    dispatched = False
    sent = []

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        return body_chunks.pop(0)

    async def app(_scope, _receive, _send):
        nonlocal dispatched
        dispatched = True

    async def send(message):
        sent.append(message)

    middleware = _JsonRpcRequestIdLimitMiddleware(app)
    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/mcp",
            "headers": [(b"content-length", str(len(body)).encode())],
        },
        receive,
        send,
    )

    assert not dispatched
    assert receive_calls == 2
    response_start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = next(message["body"] for message in sent if message["type"] == "http.response.body")
    assert response_start["status"] == 400
    assert len(response_body) <= MAX_TOOL_RESPONSE_BYTES
    assert json.loads(response_body)["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_streamable_http_dispatches_a_large_request_with_a_bounded_id(monkeypatch):
    from codegraphcontext.mcp_service import MCPToolResult

    received = []

    async def dispatch(_server, tool_name, arguments):
        received.append((tool_name, arguments["padding"]))
        return MCPToolResult(payload={"accepted": True})

    padding = "x" * (1024 * 1024 + 1)
    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=_FakeMCPServer()), \
         patch("codegraphcontext.api.mcp_sse.dispatch_tool_call", side_effect=dispatch):
        async with _running_client(monkeypatch=monkeypatch) as client:
            response = await client.post(
                "/api/v1/mcp",
                json=_modern_request(
                    "tools/call",
                    {"name": "ping", "arguments": {"padding": padding}},
                    request_id=7,
                ),
                headers={**_MODERN_HEADERS, "Mcp-Method": "tools/call", "Mcp-Name": "ping"},
            )

    assert response.status_code == 200
    assert received == [("ping", padding)]


@pytest.mark.asyncio
async def test_request_id_guard_replays_original_chunks_without_full_body_parse(monkeypatch):
    from codegraphcontext.api import app as app_module
    from codegraphcontext.api.app import _JsonRpcRequestIdLimitMiddleware

    prefix = b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"padding":"'
    body_chunks = [prefix + b"x" * (512 * 1024), b"x" * (512 * 1024) + b'"}}']
    input_messages = [
        {"type": "http.request", "body": chunk, "more_body": index == 0}
        for index, chunk in enumerate(body_chunks)
    ]
    receive_index = 0
    received_chunks = []
    parsed_lengths = []
    original_loads = app_module.json.loads

    def record_json_loads(value, *args, **kwargs):
        parsed_lengths.append(len(value))
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(app_module.json, "loads", record_json_loads)

    async def receive():
        nonlocal receive_index
        message = input_messages[receive_index]
        receive_index += 1
        return message

    async def downstream(_scope, replay_receive, _send):
        while True:
            message = await replay_receive()
            if message["type"] != "http.request":
                break
            received_chunks.append(message["body"])
            if not message.get("more_body", False):
                break

    async def send(_message):
        raise AssertionError("valid request must reach the downstream app")

    middleware = _JsonRpcRequestIdLimitMiddleware(downstream)
    await middleware(
        {"type": "http", "method": "POST", "path": "/api/v1/mcp", "headers": []},
        receive,
        send,
    )

    assert received_chunks == body_chunks
    assert all(actual is original for actual, original in zip(received_chunks, body_chunks))
    assert parsed_lengths and max(parsed_lengths) <= 128


def test_request_id_scan_continues_after_an_unusually_long_top_level_key():
    from codegraphcontext.api.app import _has_invalid_jsonrpc_id

    body = json.dumps({
        "x" * 256: "ignored",
        "jsonrpc": "2.0",
        "method": "tools/call",
        "id": "r" * (MAX_JSONRPC_ID_BYTES + 1),
    }).encode("utf-8")

    assert _has_invalid_jsonrpc_id([body]) is True


def test_request_id_scan_decodes_escaped_top_level_member_names():
    from codegraphcontext.api.app import _has_invalid_jsonrpc_id

    invalid_id = b'{"i\\u0064":"' + b"r" * (MAX_JSONRPC_ID_BYTES + 1) + b'"}'
    valid_id = b'{"i\\u0064":7}'

    assert _has_invalid_jsonrpc_id([invalid_id]) is True
    assert _has_invalid_jsonrpc_id([valid_id]) is False


def test_request_id_scan_preserves_short_ids_with_long_unicode_escape_spelling():
    from codegraphcontext.api.app import _has_invalid_jsonrpc_id

    escaped_id = b'{"id":"' + b"\\u0061" * 1_022 + b'"}'
    escaped_oversized_id = b'{"id":"' + b"\\u0061" * 1_023 + b'"}'

    assert _has_invalid_jsonrpc_id([escaped_id]) is False
    assert _has_invalid_jsonrpc_id([escaped_oversized_id]) is True


@pytest.mark.asyncio
async def test_oversized_rest_response_does_not_treat_nested_jsonrpc_as_envelope():
    from codegraphcontext.api.app import _JsonResponseByteLimitMiddleware

    response_body = json.dumps(
        {
            "data": {
                "jsonrpc": "2.0",
                "id": "nested-id",
                "error": {"code": -32601},
            },
            "detail": "x" * (MAX_TOOL_RESPONSE_BYTES * 2),
        }
    ).encode("utf-8")
    sent = []

    async def app(_scope, _receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": response_body, "more_body": False})

    async def send(message):
        sent.append(message)

    middleware = _JsonResponseByteLimitMiddleware(app)
    await middleware({"type": "http", "method": "GET", "path": "/api/v1/query"}, None, send)

    final_body = next(message["body"] for message in sent if message["type"] == "http.response.body")
    replacement = json.loads(final_body)
    assert len(final_body) <= MAX_TOOL_RESPONSE_BYTES
    assert replacement == {"detail": "Serialized response exceeded the maximum size."}


@pytest.mark.asyncio
async def test_streamable_http_bounds_oversized_unknown_method_error(monkeypatch):
    unknown_method = "unknown." + ("x" * (MAX_TOOL_RESPONSE_BYTES + 1024))
    async with _running_client(monkeypatch=monkeypatch) as client:
        response = await client.post(
            "/api/v1/mcp",
            json=_modern_request(unknown_method),
            headers={**_MODERN_HEADERS, "Mcp-Method": unknown_method},
        )

    assert response.content
    assert len(response.content) <= MAX_TOOL_RESPONSE_BYTES
    payload = response.json()
    assert payload["id"] == 1
    assert payload["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_legacy_sse_bounds_raw_protocol_error_response(monkeypatch):
    from codegraphcontext.api import mcp_sse

    error_body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": -32601,
            "message": "Method not found: " + ("x" * (MAX_TOOL_RESPONSE_BYTES + 1024)),
        },
    }).encode("utf-8")

    async def write_raw_error(_scope, _receive, sender):
        await sender({
            "type": "http.response.start",
            "status": 400,
            "headers": [(b"content-type", b"application/json")],
        })
        await sender({"type": "http.response.body", "body": error_body, "more_body": False})

    with patch.object(mcp_sse.sse, "handle_post_message", side_effect=write_raw_error):
        async with _running_client(monkeypatch=monkeypatch) as client:
            response = await client.post(
                "/api/v1/mcp/messages",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}},
            )

    assert response.status_code == 400
    assert len(response.content) <= MAX_TOOL_RESPONSE_BYTES
    payload = response.json()
    assert payload["id"] == 1
    assert payload["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_fastapi_validation_error_is_bounded(monkeypatch):
    async with _running_client(monkeypatch=monkeypatch) as client:
        response = await client.post(
            "/api/v1/query",
            json={"query": "MATCH (f:Function) RETURN f", "page_size": "x" * (MAX_TOOL_RESPONSE_BYTES + 1024)},
        )

    assert response.status_code == 422
    assert len(response.content) <= MAX_TOOL_RESPONSE_BYTES
    assert "maximum size" in response.json()["detail"]


@pytest.mark.asyncio
async def test_streamable_http_leaves_jsonrpc_batches_to_protocol_validation(monkeypatch):
    calls = []

    class _RecordingMCPServer(_FakeMCPServer):
        async def handle_tool_call(self, tool_name, args):
            calls.append(tool_name)
            return {"ok": True, "tool": tool_name, "args": args}

    batch = [
        _modern_request("tools/call", {"name": "ping", "arguments": {}}, request_id=7),
        _modern_request(
            "tools/call",
            {"name": "ping", "arguments": {}},
            request_id="x" * (MAX_JSONRPC_ID_BYTES + 1),
        ),
    ]
    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=_RecordingMCPServer()):
        async with _running_client(monkeypatch=monkeypatch) as client:
            response = await client.post(
                "/api/v1/mcp",
                json=batch,
                headers={**_MODERN_HEADERS, "Mcp-Method": "tools/call", "Mcp-Name": "ping"},
            )

    assert response.status_code == 400
    payload = response.json()
    assert payload["id"] is None
    assert payload["error"]["code"] == -32600
    assert "single JSON-RPC request" in payload["error"]["message"]
    assert calls == []


@pytest.mark.asyncio
async def test_legacy_sse_messages_reject_an_oversized_request_id():
    import json
    from starlette.requests import Request
    from codegraphcontext.api import mcp_sse

    raw_body = json.dumps({
        "jsonrpc": "2.0",
        "id": "x" * (MAX_JSONRPC_ID_BYTES + 1),
        "method": "tools/call",
        "params": {"name": "ping", "arguments": {}},
    }).encode()
    body_sent = False

    async def receive():
        nonlocal body_sent
        if body_sent:
            return {"type": "http.disconnect"}
        body_sent = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    with patch.object(mcp_sse.sse, "handle_post_message", side_effect=AssertionError("must not dispatch")):
        response = await mcp_sse.handle_messages(request)

    assert response.status_code == 400
    assert json.loads(response.body)["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_legacy_sse_bounds_numeric_id_beyond_python_json_digit_limit():
    from starlette.requests import Request
    from codegraphcontext.api import mcp_sse

    raw_body = (
        b'{"jsonrpc":"2.0","id":'
        + b"9" * 5000
        + b',"method":"tools/list"}'
    )
    body_sent = False

    async def receive():
        nonlocal body_sent
        if body_sent:
            return {"type": "http.disconnect"}
        body_sent = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    with patch.object(
        mcp_sse.sse,
        "handle_post_message",
        side_effect=AssertionError("oversized ID must not dispatch"),
    ):
        response = await mcp_sse.handle_messages(request)

    assert response.status_code == 400
    assert len(response.body) <= MAX_TOOL_RESPONSE_BYTES
    assert json.loads(response.body)["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_legacy_sse_passes_jsonrpc_batches_unchanged_to_protocol_validation():
    import json
    from starlette.requests import Request
    from codegraphcontext.api import mcp_sse

    valid = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "ping", "arguments": {}},
    }
    oversized = {
        "jsonrpc": "2.0",
        "id": "x" * (MAX_JSONRPC_ID_BYTES + 1),
        "method": "tools/call",
        "params": {"name": "ping", "arguments": {}},
    }
    body = json.dumps([valid, oversized]).encode()
    body_sent = False
    dispatched = []

    async def receive():
        nonlocal body_sent
        if body_sent:
            return {"type": "http.disconnect"}
        body_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def capture_batch(scope, receive_body, sender):
        message = await receive_body()
        dispatched.extend(json.loads(message["body"]))

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    with patch.object(mcp_sse.sse, "handle_post_message", side_effect=capture_batch):
        response = await mcp_sse.handle_messages(request)

    assert response.status_code == 202
    assert dispatched == [valid, oversized]


@pytest.mark.asyncio
async def test_tools_call_error_result_sets_is_error(monkeypatch):
    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=_FakeMCPServer()):
        async with _running_client(monkeypatch=monkeypatch) as client:
            resp = await client.post(
                "/api/v1/mcp",
                json=_modern_request("tools/call", {"name": "boom", "arguments": {}}),
                headers={**_MODERN_HEADERS, "Mcp-Method": "tools/call", "Mcp-Name": "boom"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["isError"] is True
    assert "simulated failure" in body["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_malformed_json_returns_400(monkeypatch):
    async with _running_client(monkeypatch=monkeypatch) as client:
        resp = await client.post(
            "/api/v1/mcp",
            content=b'{"jsonrpc":"2.0","method":"tools/list","id":1',  # missing closing }
            headers={"Content-Type": "application/json", **_MODERN_HEADERS},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_no_trailing_slash_redirect(monkeypatch):
    """A Mount would 307 a request without the trailing slash, which can
    drop the Authorization header on the redirected hop; add_route must not."""
    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=_FakeMCPServer()):
        async with _running_client(monkeypatch=monkeypatch) as client:
            resp = await client.post(
                "/api/v1/mcp",
                json=_modern_request("tools/list"),
                headers={**_MODERN_HEADERS, "Mcp-Method": "tools/list"},
            )

    assert resp.status_code != 307


@pytest.mark.asyncio
async def test_requires_api_key_when_configured(monkeypatch):
    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=_FakeMCPServer()):
        async with _running_client(env={"CGC_API_KEY": "s3cret"}, monkeypatch=monkeypatch) as client:
            no_key_resp = await client.post(
                "/api/v1/mcp",
                json=_modern_request("tools/list"),
                headers={**_MODERN_HEADERS, "Mcp-Method": "tools/list"},
            )
            wrong_key_resp = await client.post(
                "/api/v1/mcp",
                json=_modern_request("tools/list"),
                headers={**_MODERN_HEADERS, "Mcp-Method": "tools/list", "X-API-Key": "wrong"},
            )
            right_key_resp = await client.post(
                "/api/v1/mcp",
                json=_modern_request("tools/list"),
                headers={**_MODERN_HEADERS, "Mcp-Method": "tools/list", "X-API-Key": "s3cret"},
            )

    assert no_key_resp.status_code == 401
    assert wrong_key_resp.status_code == 401
    assert right_key_resp.status_code == 200


@pytest.mark.asyncio
async def test_origin_validation_default_on(monkeypatch):
    """Origin is checked by default (DNS-rebinding protection): a browser tab
    on an untrusted origin is rejected, a non-browser client sending no
    Origin at all is allowed, and localhost is allowed by default."""
    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=_FakeMCPServer()):
        async with _running_client(monkeypatch=monkeypatch) as client:
            no_origin_resp = await client.post(
                "/api/v1/mcp",
                json=_modern_request("tools/list"),
                headers={**_MODERN_HEADERS, "Mcp-Method": "tools/list"},
            )
            evil_origin_resp = await client.post(
                "/api/v1/mcp",
                json=_modern_request("tools/list"),
                headers={**_MODERN_HEADERS, "Mcp-Method": "tools/list", "Origin": "http://evil.example"},
            )
            localhost_origin_resp = await client.post(
                "/api/v1/mcp",
                json=_modern_request("tools/list"),
                headers={**_MODERN_HEADERS, "Mcp-Method": "tools/list", "Origin": "http://localhost:3000"},
            )

    assert no_origin_resp.status_code == 200
    assert evil_origin_resp.status_code == 403
    assert localhost_origin_resp.status_code == 200


def test_legacy_sse_routes_still_registered():
    """Clients still configured with the pre-migration /sse URL (see
    docs/INTEGRATION_GUIDE.md) must keep working during the deprecation
    window, not 404."""
    from codegraphcontext.api.app import create_app

    app = create_app()
    route_paths = {getattr(r, "path", None): getattr(r, "methods", None) for r in app.routes}
    assert route_paths.get("/api/v1/mcp/sse") == {"GET"}
    assert route_paths.get("/api/v1/mcp/messages") == {"POST"}
    assert "/api/v1/mcp" in route_paths


@asynccontextmanager
async def _running_server(port: int):
    """Run the real app under uvicorn on localhost. ASGITransport can't drive
    an open SSE stream (it waits for the app callable to finish before
    returning), so the legacy transport needs a real socket to prove it
    actually completes a request/response cycle under mcp 2.x."""
    from codegraphcontext.api.app import create_app

    async def fake_server_startup():
        return SimpleNamespace(db_manager=None)

    with (
        patch("codegraphcontext.api.app.get_server_for_gateway_startup", fake_server_startup),
        patch("codegraphcontext.api.app.shutdown_server"),
    ):
        app = create_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        try:
            while not server.started:
                await asyncio.sleep(0.05)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            await task


@pytest.mark.asyncio
async def test_legacy_sse_transport_end_to_end():
    """A real MCP client (mcp.client.sse) against a real server proves the
    legacy /sse + /messages routes still work under the mcp 2.x SDK, not
    just that the route table has entries for them."""
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=_FakeMCPServer()):
        async with _running_server(port=18791) as base_url:
            with anyio.fail_after(15):
                async with sse_client(f"{base_url}/api/v1/mcp/sse") as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert [t.name for t in tools.tools] == ["ping"]
                        result = await session.call_tool("ping", {})
                        assert result.is_error is False


@pytest.mark.asyncio
async def test_legacy_streamable_http_initialize_handshake():
    """A 2025-era client (initialize handshake, no MCP-Protocol-Version
    header) hitting the new single /api/v1/mcp endpoint gets a session ID
    back — the SDK's era classifier routes it to the legacy handler rather
    than rejecting it, so one mount really does serve both eras."""
    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=_FakeMCPServer()):
        async with _running_server(port=18792) as base_url:
            with anyio.fail_after(15):
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"{base_url}/api/v1/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-06-18",
                                "capabilities": {},
                                "clientInfo": {"name": "test", "version": "1.0"},
                            },
                        },
                        headers={
                            "Accept": "application/json, text/event-stream",
                            "Content-Type": "application/json",
                        },
                    )

    assert resp.status_code == 200
    assert resp.headers.get("mcp-session-id")
    data = next(line.removeprefix("data: ") for line in resp.text.splitlines() if line.startswith("data: "))
    body = json.loads(data)
    assert body["result"]["protocolVersion"] == "2025-06-18"
    from codegraphcontext.mcp_service import MCP_SERVER_VERSION

    assert body["result"]["serverInfo"]["version"] == MCP_SERVER_VERSION


@pytest.mark.asyncio
async def test_legacy_routes_enforce_origin_and_api_key():
    """The legacy /sse and /messages routes don't go through
    _TransportSecurityMiddleware (that only wraps the new mount) — they must
    still reject a bad Origin and a missing/wrong API key via
    require_mcp_transport_security / require_api_key on the route itself."""
    with patch("codegraphcontext.api.mcp_sse.get_server", return_value=_FakeMCPServer()):
        async with _running_server(port=18793) as base_url:
            with anyio.fail_after(15):
                async with httpx.AsyncClient() as client:
                    evil_origin_resp = await client.post(
                        f"{base_url}/api/v1/mcp/messages",
                        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                        headers={"Origin": "http://evil.example"},
                    )

    assert evil_origin_resp.status_code == 403
