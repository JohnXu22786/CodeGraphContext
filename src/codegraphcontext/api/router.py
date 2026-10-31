# src/codegraphcontext/api/router.py
import asyncio
import hmac
import ipaddress
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
import json
from fastapi.encoders import jsonable_encoder
from typing import Dict, Any, List
from pathlib import Path

from .schemas import (
    IndexRequest,
    QueryRequest,
    SearchRequest,
    ToolCallRequest,
    ApiResponse,
    LocalUpdateRequest,
)
from .auth import require_api_key
from codegraphcontext.server import MCPServer, _apply_final_output_limits
from codegraphcontext.core.jobs import JobStatus, wait_task_until_stopped
from codegraphcontext.core.database_ownership import (
    DatabaseOwnershipLease,
)
from codegraphcontext.utils.repo_path import any_repo_matches_path
from codegraphcontext.tools.indexing.discovery import root_file_symlink_exclusion_reason

import socket

# Optional API-key auth is enforced on every route below via require_api_key.
# It is a no-op (backward compatible) unless CGC_API_KEY is configured, in
# which case each request must supply the key. See codegraphcontext.api.auth.
router = APIRouter(dependencies=[Depends(require_api_key)])
# This separate router is protected by a per-owner random token and a loopback
# peer check. It must not inherit the public API-key dependency above.
local_router = APIRouter()

# Global server instance (initialized on startup)
_server_instance: MCPServer = None
_local_update_lock = asyncio.Lock()

async def get_server() -> MCPServer:
    """Build the server on the running event loop.

    A sync dependency runs in a worker thread, where MCPServer falls back to a
    fresh event loop nobody runs; jobs scheduled with run_coroutine_threadsafe
    then never start.
    """
    global _server_instance
    if _server_instance is None:
        _server_instance = MCPServer(loop=asyncio.get_running_loop(), cwd=Path.cwd())
    return _server_instance


async def get_server_for_gateway_startup() -> MCPServer:
    """Build the API server without requiring a remote DB during liveness startup.

    Embedded managers still open here so the gateway owns their writer lease
    before it starts accepting requests. Remote managers connect on their first
    database operation instead.
    """
    global _server_instance
    if _server_instance is None:
        _server_instance = MCPServer(
            loop=asyncio.get_running_loop(),
            cwd=Path.cwd(),
            initialize_database=False,
        )

    manager = getattr(_server_instance, "db_manager", None)
    if hasattr(manager, "_ownership_lease"):
        lease = getattr(manager, "_ownership_lease", None)
        if not isinstance(lease, DatabaseOwnershipLease) or not lease.is_acquired:
            manager.get_driver()
    return _server_instance


def shutdown_server() -> None:
    """Stop the API's MCP server and release its embedded database lease."""
    global _server_instance
    server = _server_instance
    _server_instance = None
    if server is not None:
        server.shutdown()


def _require_local_owner(request: Request, server: MCPServer) -> DatabaseOwnershipLease:
    peer = request.client.host if request.client is not None else None
    try:
        is_loopback = ipaddress.ip_address((peer or "").split("%", 1)[0]).is_loopback
    except ValueError:
        is_loopback = (peer or "").lower() == "localhost"
    if not is_loopback:
        raise HTTPException(status_code=403, detail="Local ownership endpoint is loopback-only")

    lease = getattr(server.db_manager, "_ownership_lease", None)
    expected_token = lease.token if isinstance(lease, DatabaseOwnershipLease) else ""
    provided_token = request.headers.get("X-CGC-Owner-Token", "")
    if not expected_token or not hmac.compare_digest(provided_token, expected_token):
        raise HTTPException(status_code=403, detail="Invalid local gateway owner token")
    if not lease.is_acquired:
        raise HTTPException(status_code=503, detail="Gateway no longer owns this embedded database")

    server_address = request.scope.get("server")
    try:
        actual_port = int(server_address[1])
    except (TypeError, ValueError, IndexError):
        raise HTTPException(status_code=503, detail="Gateway listening port is unavailable")
    registered_gateway = (lease.owner or {}).get("gateway")
    if (
        not isinstance(registered_gateway, dict)
        or registered_gateway.get("port") != actual_port
    ):
        raise HTTPException(status_code=503, detail="Gateway ownership registration is unavailable")
    return lease


@local_router.get("/ownership", include_in_schema=False)
async def get_local_ownership(
    request: Request,
    server: MCPServer = Depends(get_server),
):
    lease = _require_local_owner(request, server)
    owner = lease.owner or {}
    return {
        "database_path": lease.database_path,
        "backend": lease.backend,
        "pid": owner.get("pid"),
        "hostname": owner.get("hostname"),
        "instance_name": owner.get("instance_name"),
        "lease_id": owner.get("lease_id"),
        "port": owner.get("gateway", {}).get("port"),
    }


def _prepare_local_update(server: MCPServer, path: Path, cgcignore_path: str):
    """Run synchronous database and filesystem work outside the ASGI loop."""
    indexed_repositories = server.code_finder.list_indexed_repositories()
    if any_repo_matches_path(indexed_repositories, path):
        server.graph_builder.delete_repository_from_graph(str(path))

    estimate = server.graph_builder.estimate_processing_time(
        path, cgcignore_path=cgcignore_path
    )
    return estimate or (0, 0.0)


def _prepare_local_update_and_create_job(
    server: MCPServer,
    path: Path,
    cgcignore_path: str,
    timeout_seconds: float | None,
):
    """Prepare an update and register its job before a context switch can run."""
    with server._context_transition_lock:
        total_files, estimated_duration = _prepare_local_update(
            server, path, cgcignore_path
        )
        job_id = server.job_manager.create_job(
            str(path), is_dependency=False, timeout_seconds=timeout_seconds
        )
        server.job_manager.update_job(
            job_id,
            total_files=total_files,
            estimated_duration=estimated_duration,
        )
    return total_files, estimated_duration, job_id


async def _prepare_local_update_and_create_job_async(
    server: MCPServer,
    path: Path,
    cgcignore_path: str,
    timeout_seconds: float | None,
):
    """Keep sync preparation off-loop and cancel a job created during request stop."""
    task = asyncio.create_task(
        asyncio.to_thread(
            _prepare_local_update_and_create_job,
            server,
            path,
            cgcignore_path,
            timeout_seconds,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await wait_task_until_stopped(task)
        try:
            job_id = task.result()[2]
        except BaseException:
            job_id = None
        if job_id:
            cancel_task = asyncio.create_task(
                server.job_manager.cancel_job_async(job_id)
            )
            await wait_task_until_stopped(cancel_task)
        raise


@local_router.post("/update", include_in_schema=False)
async def update_through_local_gateway(
    request: LocalUpdateRequest,
    http_request: Request,
    server: MCPServer = Depends(get_server),
):
    _require_local_owner(http_request, server)
    requested_path = Path(request.path)
    symlink_reason = root_file_symlink_exclusion_reason(requested_path)
    if symlink_reason:
        raise HTTPException(status_code=400, detail=symlink_reason)
    path = requested_path.resolve()
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"Path does not exist: {path}")

    # This token-authenticated loopback route mirrors the local CLI's update
    # path behavior. Public API/tool routes continue to enforce allowed roots.
    cgcignore_path = request.cgcignore_path or server.resolved_context.cgcignore_path
    async with _local_update_lock:
        try:
            total_files, estimated_duration, job_id = (
                await _prepare_local_update_and_create_job_async(
                    server, path, cgcignore_path, request.timeout_seconds
                )
            )
            await server.graph_builder.build_graph_from_path_async(
                path,
                is_dependency=False,
                job_id=job_id,
                cgcignore_path=cgcignore_path,
            )
            job = server.job_manager.get_job(job_id)
            if job is None:
                raise RuntimeError("Gateway lost the update job record")
            if job.status != JobStatus.COMPLETED:
                detail = job.errors[0] if job.errors else f"Update ended with status {job.status.value}"
                raise RuntimeError(detail)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Gateway update failed: {exc}") from exc

    return {
        "status": "ok",
        "data": {"path": str(path), "job_id": job_id},
    }


def _bounded_api_tool_data(tool_name: str, result: Any) -> Any:
    """Limit tool data against the serialized REST response envelope."""
    response_text = json.dumps(
        jsonable_encoder(result), ensure_ascii=False, separators=(",", ":")
    )

    def serialize_wrapped(text: str) -> bytes:
        wrapped = ApiResponse(status="ok", data=json.loads(text))
        return json.dumps(
            jsonable_encoder(wrapped),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    bounded_text = _apply_final_output_limits(
        tool_name, response_text, serialize_wrapped
    )
    return json.loads(bounded_text)


def _bounded_api_error(tool_name: str, error: Any) -> ApiResponse:
    """Limit error details against the same serialized REST envelope cap."""
    error_text = json.dumps(error, ensure_ascii=False, separators=(",", ":"))

    def serialize_wrapped(text: str) -> bytes:
        payload = json.loads(text)
        if isinstance(payload, dict) and payload.get("truncated") is True:
            response = ApiResponse(
                status="error",
                error=payload.get("notice", "Error response truncated."),
                data=payload,
            )
        else:
            response = ApiResponse(status="error", error=str(payload))
        return json.dumps(
            jsonable_encoder(response),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    bounded_text = _apply_final_output_limits(
        tool_name, error_text, serialize_wrapped
    )
    payload = json.loads(bounded_text)
    if isinstance(payload, dict) and payload.get("truncated") is True:
        return ApiResponse(
            status="error",
            error=payload.get("notice", "Error response truncated."),
            data=payload,
        )
    return ApiResponse(status="error", error=str(payload))


def raise_service_unavailable(exc: Exception):
    raise HTTPException(
        status_code=503,
        detail="Database service unavailable",
    ) from exc

@router.get("/status", response_model=ApiResponse)
async def get_status(server: MCPServer = Depends(get_server)):
    try:
        status = server.db_manager.is_connected()
    except (OSError, socket.error) as exc:
        raise_service_unavailable(exc)

    return ApiResponse(
        status="ok",
        message="Connected" if status else "Disconnected",
        data={"database": server.resolved_context.database}
    )

@router.get("/tools", response_model=ApiResponse)
async def list_tools(server: MCPServer = Depends(get_server)):
    return ApiResponse(
        status="ok",
        data={"tools": list(server.tools.values())}
    )

@router.post("/tools/call", response_model=ApiResponse)
async def call_tool(
    request: ToolCallRequest,
    server: MCPServer = Depends(get_server)
):
    try:
        result = await server.handle_tool_call(
            request.name,
            request.arguments
        )

        if "error" in result:
            return _bounded_api_error(request.name, result["error"])

        return ApiResponse(
            status="ok",
            data=_bounded_api_tool_data(request.name, result),
        )

    except (OSError, socket.error) as exc:
        raise_service_unavailable(exc)

    except Exception as e:
        return _bounded_api_error(request.name, str(e))
    

@router.post("/index", response_model=ApiResponse)
async def index_repository(
    request: IndexRequest,
    background_tasks: BackgroundTasks,
    server: MCPServer = Depends(get_server)
):
    # The add_code_to_graph handler only understands "path" (and is_dependency);
    # repo_name/branch/force from the request model are not supported by it.
    args = {"path": request.path}
    if request.timeout_seconds is not None:
        args["timeout_seconds"] = request.timeout_seconds

    try:
        result = await server.handle_tool_call(
            "add_code_to_graph",
            args
        )
    except (OSError, socket.error) as exc:
        raise_service_unavailable(exc)

    if "error" in result:
        raise HTTPException(
            status_code=400,
            detail=result["error"]
        )

    return ApiResponse(
        status="ok",
        message="Indexing job started",
        data=result
    )

@router.post("/query", response_model=ApiResponse)
async def execute_query(
    request: QueryRequest,
    server: MCPServer = Depends(get_server)
):
    arguments = {
        "cypher_query": request.query,
        "params": request.params,
    }
    if request.repo_path is not None:
        arguments["repo_path"] = request.repo_path
    if request.global_scope:
        arguments["global_scope"] = True
    if request.offset:
        arguments["offset"] = request.offset
    if request.page_size is not None:
        arguments["page_size"] = request.page_size
    try:
        result = await server.handle_tool_call(
            "execute_cypher_query",
            arguments,
        )
    except (OSError, socket.error) as exc:
        raise_service_unavailable(exc)

    if "error" in result:
        return _bounded_api_error("execute_cypher_query", result["error"])

    return ApiResponse(
        status="ok",
        data=_bounded_api_tool_data("execute_cypher_query", result),
    )

@router.get("/repositories", response_model=ApiResponse)
async def list_repositories(
    server: MCPServer = Depends(get_server)
):
    try:
        result = await server.handle_tool_call(
            "list_indexed_repositories",
            {}
        )
    except (OSError, socket.error) as exc:
        raise_service_unavailable(exc)

    if "error" in result:
        return _bounded_api_error("list_indexed_repositories", result["error"])

    return ApiResponse(
        status="ok",
        data=_bounded_api_tool_data("list_indexed_repositories", result),
    )
