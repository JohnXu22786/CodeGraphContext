# HTTP API Reference

CodeGraphContext ships a **CGC Gateway** HTTP server for ChatGPT Actions, Claude connectors, and custom web frontends. It wraps the same MCP tool surface exposed by `cgc mcp start`.

---

## Starting the Server

```bash
cgc api start
cgc api start --host 127.0.0.1 --port 8080
cgc api start --reload   # development only
```

The server loads credentials from the same configuration chain as the CLI (`CGC_HOME/.env`, default `~/.codegraphcontext/.env`, context resolution, etc.).

---

## Endpoints

### Health & Status

| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | Liveness probe. Returns `{"status": "ok"}`. Suitable for load balancers and Kubernetes. |
| `GET` | `/` | Simple HTML landing page with links to OpenAPI docs. |
| `GET` | `/api/v1/status` | Database connectivity and active backend name. |

### MCP (Streamable HTTP)

| Method | Path | Description |
| :--- | :--- | :--- |
| `POST` | `/api/v1/mcp` | MCP endpoint (Streamable HTTP, spec 2026-07-28). Single endpoint for all MCP clients; each request is answered with a JSON response or a request-scoped SSE stream. |

### MCP-over-SSE (deprecated)

| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/api/v1/mcp/sse` | Server-Sent Events stream for MCP clients using the deprecated HTTP+SSE transport (protocol version 2024-11-05). Kept for existing client configurations; new integrations should use `/api/v1/mcp`. |
| `POST` | `/api/v1/mcp/messages` | MCP message ingress for the deprecated SSE transport. |

### REST Tool Bridge

| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/api/v1/tools` | Lists registered MCP tools and schemas. |
| `POST` | `/api/v1/tools/call` | Invokes a tool by name with JSON arguments. |
| `POST` | `/api/v1/index` | Triggers repository indexing (background job). |
| `POST` | `/api/v1/query` | Executes a read-only Cypher query. |
| `GET` | `/api/v1/repositories` | Lists indexed repositories in the active context. |

Interactive OpenAPI documentation is available at `/docs` while the server is running.

`POST /api/v1/index` accepts an optional positive `timeout_seconds` in its JSON body. If the deadline expires during an active database call, the job stops at the next safe boundary and removes the incomplete repository graph; if cleanup fails, it reports `failed` and the partial repository stays out of indexed-repository lists. While any repository is building, graph search and read-only Cypher tools report that indexing is in progress instead of returning partial graph results. The final ready-marker write is the commit point: once it starts, a successful write completes the job even if the deadline passes during that call. Database drivers do not expose a portable way to interrupt a call already in progress.

---

## Example: Health Check

```bash
curl -s http://localhost:8000/health
# {"status":"ok"}
```

```bash
curl -s http://localhost:8000/api/v1/status
```

---

## Relationship to MCP

| Interface | Transport | Typical use |
| :--- | :--- | :--- |
| `cgc mcp start` | stdio | Cursor, Claude Desktop, VS Code |
| `cgc api start` | HTTP / SSE | Web apps, ChatGPT Actions, remote agents |

Both paths share the same graph database and tool implementations.
