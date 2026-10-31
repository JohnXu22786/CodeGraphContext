# src/codegraphcontext/api/app.py
from contextlib import asynccontextmanager
import asyncio
import json
from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from .router import (
    get_server_for_gateway_startup,
    local_router,
    router,
    shutdown_server,
)
import logging

from .auth import (
    get_configured_api_key,
    get_mcp_transport_security_config,
    log_auth_status,
    require_api_key,
    require_mcp_transport_security,
    _extract_provided_key,
    _host_matches,
    _keys_match,
    _origin_matches,
)
from codegraphcontext.server import (
    MAX_JSONRPC_ID_BYTES,
    MAX_TOOL_RESPONSE_BYTES,
    _valid_jsonrpc_id,
)
from .mcp_sse import handle_messages, handle_sse, mcp_server
from mcp.server.transport_security import TransportSecuritySettings
from codegraphcontext.runtime_paths import get_instance_name
from codegraphcontext.core.database_ownership import (
    DatabaseOwnershipLease,
    restore_process_gateway_endpoint,
    set_process_gateway_endpoint,
)
import uvicorn

logger = logging.getLogger(__name__)

# A string ID can shrink by up to six bytes per character when JSON escapes
# such as ``\u0061`` are decoded and the value is serialized again.
_MAX_JSONRPC_ID_RAW_TOKEN_BYTES = MAX_JSONRPC_ID_BYTES * 6 + 2


class _TransportSecurityMiddleware:
    """DNS-rebinding protection: Origin validated by default, Host opt-in.

    A browser tab on an attacker's page can still send same-origin-looking
    requests to a server bound to localhost/0.0.0.0 (DNS rebinding); checking
    Origin blocks that without breaking CLI/desktop MCP clients, which don't
    send an Origin header at all. Host is left opt-in (CGC_MCP_ALLOWED_HOSTS)
    because this gateway commonly sits behind a proxy or a non-loopback
    hostname with no single safe default.
    """

    def __init__(self, app, allowed_hosts: list[str], allowed_origins: list[str]):
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.allowed_origins = allowed_origins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])

        if self.allowed_hosts:
            host = headers.get(b"host", b"").decode("latin-1") or None
            if not _host_matches(host, self.allowed_hosts):
                logger.warning("Rejected MCP request with Host %r; add it to CGC_MCP_ALLOWED_HOSTS if legitimate.", host)
                response = Response(content="Invalid Host header", status_code=421)
                await response(scope, receive, send)
                return

        origin = headers.get(b"origin", b"").decode("latin-1") or None
        if not _origin_matches(origin, self.allowed_origins):
            logger.warning("Rejected MCP request with Origin %r; add it to CGC_MCP_ALLOWED_ORIGINS if legitimate.", origin)
            response = Response(content="Invalid Origin header", status_code=403)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class _ApiKeyMiddleware:
    """ASGI middleware enforcing the same API-key check as require_api_key().

    The mounted Streamable HTTP app is a plain Starlette app, not a FastAPI
    route, so it can't take a FastAPI ``Depends`` — this reuses the same
    header-extraction/comparison logic at the ASGI layer instead.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        configured = get_configured_api_key()
        if configured:
            headers = dict(scope["headers"])
            authorization = headers.get(b"authorization", b"").decode("latin-1") or None
            x_api_key = headers.get(b"x-api-key", b"").decode("latin-1") or None
            provided = _extract_provided_key(authorization, x_api_key)
            if not provided or not _keys_match(provided, configured):
                response = Response(
                    content='{"error": "Invalid or missing API key"}',
                    status_code=401,
                    media_type="application/json",
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


class _ChunkedJsonCursor:
    """Read JSON token boundaries across ASGI chunks without joining the body."""

    _WHITESPACE = {0x20, 0x09, 0x0A, 0x0D}

    def __init__(self, chunks):
        self._chunks = [memoryview(chunk) for chunk in chunks if chunk]
        self._chunk_index = 0
        self._position = 0

    def peek(self):
        while self._chunk_index < len(self._chunks):
            chunk = self._chunks[self._chunk_index]
            if self._position < len(chunk):
                return chunk[self._position]
            self._chunk_index += 1
            self._position = 0
        return None

    def read(self):
        value = self.peek()
        if value is not None:
            self._position += 1
        return value

    def skip_whitespace(self):
        while self.peek() in self._WHITESPACE:
            self.read()

    def read_string(self, capture_limit):
        if self.read() != 0x22:
            return None, False, False
        token = bytearray(b'"') if capture_limit else None
        too_long = False
        while True:
            current = self.read()
            if current is None:
                return None, too_long, False
            if token is not None:
                if len(token) < capture_limit:
                    token.append(current)
                else:
                    too_long = True
            if current == 0x5C:
                escaped = self.read()
                if escaped is None:
                    return None, too_long, False
                if token is not None:
                    if len(token) < capture_limit:
                        token.append(escaped)
                    else:
                        too_long = True
            elif current == 0x22:
                return (bytes(token) if token is not None and not too_long else None), too_long, True

    def consume_value(self, capture_limit=0):
        self.skip_whitespace()
        first = self.peek()
        if first is None:
            return None, False, False
        if first == 0x22:
            token, too_long, complete = self.read_string(capture_limit)
            return ("string", token, too_long), True, complete
        if first in (0x7B, 0x5B):
            stack = [0x7D if first == 0x7B else 0x5D]
            self.read()
            while stack:
                current = self.read()
                if current is None:
                    return None, False, False
                if current == 0x22:
                    self._position -= 1
                    _, _, complete = self.read_string(0)
                    if not complete:
                        return None, False, False
                elif current == 0x7B:
                    stack.append(0x7D)
                    if len(stack) > 1_000:
                        return None, False, False
                elif current == 0x5B:
                    stack.append(0x5D)
                    if len(stack) > 1_000:
                        return None, False, False
                elif current in (0x7D, 0x5D):
                    if not stack or current != stack.pop():
                        return None, False, False
            return ("composite", None, False), True, True

        token = bytearray()
        too_long = False
        while True:
            current = self.peek()
            if current is None or current in self._WHITESPACE or current in (0x2C, 0x5D, 0x7D):
                break
            current = self.read()
            if capture_limit and len(token) < capture_limit:
                token.append(current)
            elif capture_limit:
                too_long = True
        return ("scalar", bytes(token) if not too_long else None, too_long), True, bool(token) or too_long


def _has_invalid_jsonrpc_id(chunks) -> bool:
    """Inspect only the top-level id while leaving full request parsing downstream."""
    cursor = _ChunkedJsonCursor(chunks)
    cursor.skip_whitespace()
    if cursor.peek() != 0x7B:  # Ignore batches and non-object JSON for protocol validation.
        return False
    cursor.read()
    invalid_id = False
    found_id = False
    while True:
        cursor.skip_whitespace()
        if cursor.peek() == 0x7D:
            cursor.read()
            cursor.skip_whitespace()
            return found_id and invalid_id and cursor.peek() is None
        key_token, key_too_long, key_complete = cursor.read_string(128)
        if not key_complete:
            return False
        if key_too_long or key_token is None:
            # A valid spelling of the two-character key "id" is far shorter
            # than this bound, so an oversized key cannot select that member.
            key = None
        else:
            try:
                key = json.loads(key_token)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False
        cursor.skip_whitespace()
        if cursor.read() != 0x3A:
            return False
        value_token, value_ok, value_complete = cursor.consume_value(
            capture_limit=_MAX_JSONRPC_ID_RAW_TOKEN_BYTES if key == "id" else 0
        )
        if not value_ok or not value_complete or value_token is None:
            return False
        if key == "id":
            found_id = True
            value_type, raw_value, too_long = value_token
            if value_type == "composite" or too_long:
                invalid_id = True
            else:
                try:
                    request_id = json.loads(raw_value)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    invalid_id = False
                except ValueError:
                    # Python refuses to parse extremely long integer tokens;
                    # those tokens already exceed the reflected-ID budget.
                    invalid_id = True
                else:
                    invalid_id = not _valid_jsonrpc_id(request_id)
        cursor.skip_whitespace()
        separator = cursor.read()
        if separator == 0x2C:
            continue
        if separator == 0x7D:
            cursor.skip_whitespace()
            return found_id and invalid_id and cursor.peek() is None
        return False


class _JsonRpcRequestIdLimitMiddleware:
    """Reject reflected request IDs that could exceed the MCP output budget."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        request_messages = []
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            request_messages.append(message)
            more_body = message.get("more_body", False)

        # MCP's Streamable HTTP endpoint accepts one request or notification
        # per POST; it rejects JSON-RPC batches before dispatch. Leave a list
        # to that protocol parser instead of treating its members as calls.
        body_chunks = [message.get("body", b"") for message in request_messages]
        if _has_invalid_jsonrpc_id(body_chunks):
            response = Response(
                content=json.dumps({
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": f"Invalid Request: id must serialize to at most {MAX_JSONRPC_ID_BYTES} bytes.",
                    },
                }, separators=(",", ":")),
                status_code=400,
                media_type="application/json",
            )
            await response(scope, receive, send)
            return

        replay_index = 0

        async def replay_receive():
            nonlocal replay_index
            if replay_index < len(request_messages):
                message = request_messages[replay_index]
                replay_index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)


class _JsonResponseByteLimitMiddleware:
    """Apply the final serialized-output ceiling to every JSON ASGI response.

    Tool handlers already retain useful truncation metadata. This outer
    boundary covers responses built by the MCP SDK and FastAPI itself, which
    bypass those handler-level wrappers. Only JSON responses are buffered;
    SSE and other streaming media continue directly to the client.
    """

    _PREFIX_LIMIT = 4096
    _MISSING = object()

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _skip_json_whitespace(text: str, position: int) -> int:
        while position < len(text) and text[position] in " \t\r\n":
            position += 1
        return position

    @classmethod
    def _top_level_error_code(cls, text: str, position: int, decoder: json.JSONDecoder):
        position = cls._skip_json_whitespace(text, position)
        if position >= len(text) or text[position] != "{":
            return None
        position += 1

        while True:
            position = cls._skip_json_whitespace(text, position)
            if position >= len(text) or text[position] == "}":
                return None
            try:
                key, position = decoder.raw_decode(text, position)
            except json.JSONDecodeError:
                return None
            if not isinstance(key, str):
                return None

            position = cls._skip_json_whitespace(text, position)
            if position >= len(text) or text[position] != ":":
                return None
            value_position = cls._skip_json_whitespace(text, position + 1)
            try:
                value, end = decoder.raw_decode(text, value_position)
            except json.JSONDecodeError:
                return None
            if key == "code":
                return value if type(value) is int and value < 0 else None

            position = cls._skip_json_whitespace(text, end)
            if position >= len(text):
                return None
            if text[position] == "}":
                return None
            if text[position] != ",":
                return None
            position += 1

    @classmethod
    def _top_level_jsonrpc_metadata(cls, prefix: bytes):
        text = prefix.decode("utf-8", errors="ignore")
        decoder = json.JSONDecoder()
        position = cls._skip_json_whitespace(text, 0)
        if position >= len(text) or text[position] != "{":
            return None
        position += 1

        version = cls._MISSING
        request_id = cls._MISSING
        error_code = None
        while True:
            position = cls._skip_json_whitespace(text, position)
            if position >= len(text) or text[position] == "}":
                break
            try:
                key, position = decoder.raw_decode(text, position)
            except json.JSONDecodeError:
                break
            if not isinstance(key, str):
                break

            position = cls._skip_json_whitespace(text, position)
            if position >= len(text) or text[position] != ":":
                break
            value_position = cls._skip_json_whitespace(text, position + 1)

            if key == "error":
                error_code = cls._top_level_error_code(text, value_position, decoder)

            try:
                value, end = decoder.raw_decode(text, value_position)
            except json.JSONDecodeError:
                # The large value may continue beyond the retained prefix. If
                # protocol version and ID were already read, they still came
                # from top-level fields and are sufficient to identify this
                # as a JSON-RPC response.
                break

            if key == "jsonrpc":
                version = value
            elif key == "id":
                request_id = value

            position = cls._skip_json_whitespace(text, end)
            if position >= len(text) or text[position] == "}":
                break
            if text[position] != ",":
                break
            position += 1

        if version != "2.0" or request_id is cls._MISSING or not _valid_jsonrpc_id(request_id):
            return None
        return request_id, error_code

    @classmethod
    def _oversized_body(cls, prefix: bytes) -> bytes:
        metadata = cls._top_level_jsonrpc_metadata(prefix)
        if metadata is not None:
            request_id, error_code = metadata
            replacement = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": error_code if error_code is not None else -32603,
                    "message": "Serialized response exceeded the maximum size.",
                },
            }
        else:
            replacement = {
                "detail": "Serialized response exceeded the maximum size."
            }
        return json.dumps(replacement, separators=(",", ":")).encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_start = None
        chunks = []
        total_bytes = 0
        prefix = bytearray()
        collecting = False
        oversized = False

        async def bounded_send(message):
            nonlocal response_start, chunks, total_bytes, prefix, collecting, oversized
            message_type = message.get("type")

            if message_type == "http.response.start":
                headers = dict(message.get("headers", []))
                content_type = headers.get(b"content-type", b"").split(b";", 1)[0].lower()
                collecting = (
                    content_type == b"application/json"
                    or content_type.endswith(b"+json")
                )
                if collecting:
                    response_start = message
                    chunks = []
                    total_bytes = 0
                    prefix = bytearray()
                    oversized = False
                    return
                await send(message)
                return

            if collecting and message_type == "http.response.body":
                body = message.get("body", b"")
                if len(prefix) < self._PREFIX_LIMIT:
                    prefix.extend(body[: self._PREFIX_LIMIT - len(prefix)])
                if not oversized:
                    if total_bytes + len(body) <= MAX_TOOL_RESPONSE_BYTES:
                        chunks.append(body)
                        total_bytes += len(body)
                    else:
                        oversized = True
                        chunks.clear()

                if message.get("more_body", False):
                    return

                if oversized:
                    body = self._oversized_body(bytes(prefix))
                    headers = [
                        (key, value)
                        for key, value in response_start.get("headers", [])
                        if key.lower() not in {b"content-length", b"content-encoding"}
                    ]
                    headers.append((b"content-length", str(len(body)).encode("ascii")))
                    response_start = {**response_start, "headers": headers}
                else:
                    body = b"".join(chunks)

                await send(response_start)
                await send({"type": "http.response.body", "body": body, "more_body": False})
                response_start = None
                chunks = []
                total_bytes = 0
                prefix = bytearray()
                collecting = False
                oversized = False
                return

            await send(message)

        await self.app(scope, receive, bounded_send)


def _combine_lifespans(outer_lifespan, mcp_app):
    """Chain the FastAPI app's own lifespan with the mounted MCP sub-app's."""

    @asynccontextmanager
    async def combined(app):
        async with outer_lifespan(app):
            async with mcp_app.router.lifespan_context(mcp_app):
                yield

    return combined


@asynccontextmanager
async def _gateway_lifespan(app):
    """Start an early-discoverable local control endpoint for embedded DBs."""
    owner_server = None
    owner_task = None
    previous_endpoint = None
    endpoint_registered = False
    try:
        server = await get_server_for_gateway_startup()
        lease = getattr(getattr(server, "db_manager", None), "_ownership_lease", None)
        owner_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        owner_app.include_router(local_router, prefix="/api/v1/local")
        owner_server = uvicorn.Server(
            uvicorn.Config(
                owner_app,
                host="127.0.0.1",
                port=0,
                lifespan="off",
                log_config=None,
                access_log=False,
            )
        )
        owner_task = asyncio.create_task(owner_server.serve())
        while not owner_server.started:
            if owner_task.done():
                await owner_task
            await asyncio.sleep(0)

        sockets = owner_server.servers or []
        if not sockets or not sockets[0].sockets:
            raise RuntimeError("The local database owner endpoint failed to bind")
        port = int(sockets[0].sockets[0].getsockname()[1])
        endpoint = f"http://127.0.0.1:{port}"
        previous_endpoint = set_process_gateway_endpoint(endpoint, port=port)
        endpoint_registered = True
        if isinstance(lease, DatabaseOwnershipLease) and lease.is_acquired:
            lease.register_gateway(f"http://127.0.0.1:{port}", port=port)
        yield
    finally:
        try:
            if owner_server is not None and owner_task is not None:
                owner_server.should_exit = True
                await owner_task
        finally:
            try:
                if endpoint_registered:
                    restore_process_gateway_endpoint(previous_endpoint)
            finally:
                shutdown_server()


def create_app() -> FastAPI:
    # Log whether API-key auth is active. Emits a prominent security warning
    # when the gateway is running unauthenticated (CGC_API_KEY unset).
    log_auth_status()

    app = FastAPI(
        title=f"{get_instance_name()} Gateway",
        description=f"HTTP API gateway for {get_instance_name()} MCP server. Enables integration with ChatGPT Actions, Claude, and web frontends.",
        version="0.1.0",
        lifespan=_gateway_lifespan,
    )

    # Enable CORS for the website/frontend
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], # In production, restrict this
        # Credentials must stay disabled while origins is a wildcard; the
        # combination is rejected by browsers and would leak cookie-authed
        # responses to any site.
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(_JsonResponseByteLimitMiddleware)

    app.include_router(router, prefix="/api/v1")

    @app.get("/health")
    async def health():
        """Liveness probe for load balancers and k8s."""
        return {"status": "ok"}

    # MCP endpoint (Streamable HTTP, spec 2026-07-28). Replaces the deprecated
    # two-endpoint HTTP+SSE transport as the primary transport. The SDK's
    # session manager auto-detects legacy Streamable HTTP clients (2025-03-26
    # through 2025-11-25, via the MCP-Protocol-Version header) and the modern
    # per-request era, and routes each to the right internal handler — one
    # mount serves both.
    #
    # This dispatches to the same tools as the REST router
    # (execute_cypher_query, add_code_to_graph, delete_repository), so it
    # needs the same API-key check the router applies — without it, setting
    # CGC_API_KEY would leave the MCP transport as an unauthenticated path to
    # every tool. streamable_http_app() returns a plain Starlette app (no
    # FastAPI dependency injection), so the check is applied as ASGI
    # middleware instead of a FastAPI dependency. DNS-rebinding protection is
    # handled by our own _TransportSecurityMiddleware (Origin default-on,
    # Host opt-in — see get_mcp_transport_security_config), not the SDK's
    # built-in check, which couples both under one flag.
    allowed_hosts, allowed_origins = get_mcp_transport_security_config()
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/api/v1/mcp",
        # Disable the SDK's own Host/Origin check (coupled under one flag) —
        # _TransportSecurityMiddleware below replaces it with independently
        # configurable checks.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    # add_route (not mount): a Mount redirects a request without the trailing
    # slash to "/api/v1/mcp/" with a 307, and a redirect across origins can
    # drop the Authorization header. add_route with a plain ASGI callable
    # registers the exact path with no redirect and no method restriction.
    app.add_route(
        "/api/v1/mcp",
        _TransportSecurityMiddleware(
            _ApiKeyMiddleware(_JsonRpcRequestIdLimitMiddleware(mcp_app)),
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
    )

    # streamable_http_app()'s session manager needs its own lifespan entered
    # (it starts the task group backing every request) — a plain app.mount()
    # doesn't propagate that automatically, so it's driven from the parent's.
    app.router.lifespan_context = _combine_lifespans(app.router.lifespan_context, mcp_app)

    # Deprecated HTTP+SSE transport (2024-11-05). Kept alongside the mount
    # above so clients still configured with the old /api/v1/mcp/sse URL
    # (docs/INTEGRATION_GUIDE.md) keep working during the migration to
    # Streamable HTTP. Same API-key and DNS-rebinding checks as the new
    # endpoint — SseServerTransport takes no security_settings of its own, so
    # without these dependencies a browser page could open this stream
    # directly, key or no key.
    app.add_api_route(
        "/api/v1/mcp/sse",
        handle_sse,
        methods=["GET"],
        dependencies=[Depends(require_mcp_transport_security), Depends(require_api_key)],
    )
    app.add_api_route(
        "/api/v1/mcp/messages",
        handle_messages,
        methods=["POST"],
        dependencies=[Depends(require_mcp_transport_security), Depends(require_api_key)],
    )

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return """
        <!DOCTYPE html>
        <html>
            <head>
                <title>CGC Gateway</title>
                <style>
                    body { font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; background: #0f172a; color: white; margin: 0; }
                    .card { background: #1e293b; padding: 2rem; border-radius: 1rem; box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1); text-align: center; max-width: 400px; border: 1px solid #334155; }
                    h1 { color: #38bdf8; margin-top: 0; }
                    p { color: #94a3b8; line-height: 1.6; }
                    .btn { display: inline-block; background: #38bdf8; color: #0f172a; padding: 0.75rem 1.5rem; border-radius: 0.5rem; text-decoration: none; font-weight: bold; margin-top: 1rem; transition: background 0.2s; }
                    .btn:hover { background: #7dd3fc; }
                    .links { margin-top: 1.5rem; font-size: 0.9rem; }
                    .links a { color: #38bdf8; text-decoration: none; margin: 0 0.5rem; }
                </style>
            </head>
            <body>
                <div class="card">
                    <h1>CGC Gateway</h1>
                    <p>CodeGraphContext HTTP API is running. This gateway allows ChatGPT and Claude to interact with your code graph.</p>
                    <a href="/docs" class="btn">View API Docs</a>
                    <div class="links">
                        <a href="/openapi.json">OpenAPI Spec</a>
                        <a href="https://github.com/CodeGraphContext/CodeGraphContext" target="_blank">GitHub</a>
                    </div>
                </div>
            </body>
        </html>
        """

    return app

app = create_app()
