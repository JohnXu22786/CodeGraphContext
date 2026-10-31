# src/codegraphcontext/server.py
import urllib.parse
import asyncio
import json
import stdlibs
import sys
import traceback
import os
import re
import threading
from datetime import datetime
from pathlib import Path, PureWindowsPath
from dataclasses import asdict, replace

from typing import Any, Dict, Coroutine, Optional, List, Set

from .prompts import LLM_SYSTEM_PROMPT, build_system_prompt
from .core import get_database_manager
from .core.jobs import (
    JobManager,
    JobStatus,
    wait_task_until_stopped,
)
from .core.watcher import CodeWatcher
from .tools.graph_builder import GraphBuilder
from .tools.code_finder import CodeFinder
from .tools.package_resolver import get_local_package_path
from .utils.debug_log import debug_log, info_logger, error_logger, warning_logger, debug_logger
from .cli.config_manager import (
    resolve_context,
    discover_child_contexts,
    save_workspace_mapping,
    get_workspace_mapping,
    load_context_config,
    _default_global_db_path,
    _global_default_database,
    _has_explicit_database_default,
    _existing_context_fallback_storage,
    _infer_legacy_context_database,
    _context_database_and_path_for_path,
    _context_database_setting,
    _global_context_environment,
    ResolvedContext,
    CONFIG_DIR,
    load_config,
)
from .runtime_paths import (
    get_instance_mcp_config,
    get_instance_name,
    get_repo_context_dir_name,
    get_repo_context_path,
)
from .mcp_service import MCP_SERVER_VERSION, dispatch_tool_call
from .utils.gcf_encoder import encode_response

# Import Tool Definitions and Handlers
from .tool_definitions import TOOLS
from .core.database_ownership import DatabaseOwnershipLease
from .tools.handlers import (
    analysis_handlers,
    indexing_handlers,
    management_handlers,
    query_handlers,
    watcher_handlers
)

DEFAULT_EDIT_DISTANCE = 2
DEFAULT_FUZZY_SEARCH = False


def _resolve_global_context_for_restore() -> ResolvedContext:
    """Resolve the global backend independently of a selected repository context."""
    previous_ignore_project_env = os.environ.get("CGC_IGNORE_PROJECT_ENV")
    previous_default_database = os.environ.get("DEFAULT_DATABASE")
    context_selected_default = os.environ.get(
        "CGC_DB_SELECTION_SOURCE", ""
    ).startswith("context (") and not os.environ.get("CGC_RUNTIME_DB_TYPE")
    with _global_context_environment():
        os.environ["CGC_IGNORE_PROJECT_ENV"] = "1"
        if context_selected_default:
            os.environ.pop("DEFAULT_DATABASE", None)
        try:
            config = load_config(read_only=True)
            if (
                (config.get("FALKORDB_HOST") or os.environ.get("FALKORDB_HOST"))
                and not _has_explicit_database_default(read_only=True)
            ):
                database = "falkordb-remote"
            else:
                database = _global_default_database(
                    config.get("DEFAULT_DATABASE", "ladybugdb"), read_only=True
                )
            db_path = os.environ.get("CGC_RUNTIME_DB_PATH") or _default_global_db_path(
                database, read_only=True
            )
        finally:
            if previous_ignore_project_env is None:
                os.environ.pop("CGC_IGNORE_PROJECT_ENV", None)
            else:
                os.environ["CGC_IGNORE_PROJECT_ENV"] = previous_ignore_project_env
            if context_selected_default:
                if previous_default_database is None:
                    os.environ.pop("DEFAULT_DATABASE", None)
                else:
                    os.environ["DEFAULT_DATABASE"] = previous_default_database

    return ResolvedContext(
        mode="global",
        context_name="",
        database=database,
        db_path=db_path,
        cgcignore_path=str(CONFIG_DIR / "global" / ".cgcignore"),
    )


def _cancel_stdio_tool_request(request_id, active_requests, cancelling_requests):
    """Cancel an active stdio request once without relying on newer Task APIs."""
    try:
        tool_task = active_requests.get(request_id)
    except TypeError:
        return
    if (
        tool_task is not None
        and not tool_task.done()
        and request_id not in cancelling_requests
    ):
        cancelling_requests.add(request_id)
        tool_task.cancel()

WORKSPACE_PREFIX = "/workspace/"

def _teardown_db_manager(db_manager) -> None:
    """Release DB resources; stop FalkorDB Lite worker when switching contexts."""
    if getattr(db_manager, "get_backend_type", lambda: "")() == "falkordb":
        db_manager.close_driver(teardown=True)
    else:
        db_manager.close_driver()


def _set_runtime_db_type(database: Optional[str]) -> None:
    """Set or clear the process runtime backend while preserving an unset value."""
    if database is None:
        os.environ.pop("CGC_RUNTIME_DB_TYPE", None)
    else:
        os.environ["CGC_RUNTIME_DB_TYPE"] = database


def _is_path_key(key: str) -> bool:
    """Check if a dict key represents a file path field.

    Matches keys like 'path', 'clone_path', 'caller_file_path', and also
    Cypher-aliased keys like 'f.path', 'n.caller_file_path'.
    """
    # Strip Cypher alias prefix (e.g. "f.path" -> "path")
    bare = key.rsplit(".", 1)[-1] if "." in key else key
    return bare == "path" or bare.endswith("_path")


def _strip_path_value(value):
    """Strip /workspace/ prefix from a single string value."""
    if isinstance(value, str) and value.startswith(WORKSPACE_PREFIX):
        return value[len(WORKSPACE_PREFIX):]
    return value


def _strip_workspace_prefix(obj):
    """Recursively strip /workspace/ prefix from path values in results."""
    if isinstance(obj, dict):
        return {
            k: _strip_path_value(v) if _is_path_key(k) else _strip_workspace_prefix(v)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [_strip_workspace_prefix(item) for item in obj]
    return obj


# Approximate chars-per-token used by the legacy configurable response budget.
# The hard output cap below is measured in serialized UTF-8 bytes.
_CHARS_PER_TOKEN = 4
MAX_TOOL_RESPONSE_BYTES = 64 * 1024
MAX_TOOL_RESPONSE_LINES = 200
MAX_JSONRPC_ID_BYTES = 1024

_REPOSITORY_SCOPED_QUERY_TOOLS = frozenset({
    "find_code",
    "analyze_code_relationships",
    "find_dead_code",
    "calculate_cyclomatic_complexity",
    "find_most_complex_functions",
    "execute_cypher_query",
    "visualize_graph_query",
    "find_java_spring_endpoints",
    "find_java_spring_beans",
    "get_repository_stats",
    "find_datasource_nodes",
    "generate_report",
    "simulate_metrics",
    "simulate_architectural_change",
    "analyze_architectural_evolution",
})


def _initial_active_repo_path(cwd: Path, context, child_contexts=()) -> Optional[Path]:
    """Resolve one repository for default query scope, if the context is unambiguous."""
    cwd = Path(cwd).resolve()
    if getattr(context, "is_local", False):
        cgcignore_path = getattr(context, "cgcignore_path", "")
        if cgcignore_path:
            local_root = Path(cgcignore_path).resolve().parent.parent
            local_context_dir = Path(cgcignore_path).expanduser().parent
        else:
            local_root = cwd
            local_context_dir = None

        roots = set()
        walk_errors = []
        try:
            for current, directories, files in os.walk(
                local_root, onerror=walk_errors.append
            ):
                current_path = Path(current).resolve()
                if ".git" in directories or ".git" in files:
                    roots.add(current_path)
                    directories[:] = [name for name in directories if name != ".git"]
                if len(roots) > 1:
                    return None
        except OSError:
            return None
        if walk_errors:
            return None
        if roots:
            return next(iter(roots))
        if local_context_dir is not None and local_context_dir.is_dir():
            return local_root
        return None

    if getattr(context, "mode", None) == "named":
        context_name = getattr(context, "context_name", "")
        try:
            config = load_context_config(create_if_missing=False)
            selected = config.contexts.get(context_name)
            registered = {
                Path(repo).expanduser().resolve()
                for repo in (getattr(selected, "repos", ()) if selected else ())
                if repo
            }
        except Exception:
            return None
        if not registered:
            return None
        matches = [
            repo for repo in registered
            if cwd == repo or repo in cwd.parents
        ]
        if matches:
            return max(matches, key=lambda repo: len(repo.parts))
        return next(iter(registered)) if len(registered) == 1 else None

    roots = set()
    for child in child_contexts:
        child_path = getattr(child, "path", None)
        if child_path:
            roots.add(Path(child_path).resolve())

    # A workspace root can contain several Git repositories without CGC local
    # contexts. Walk for Git markers so a deeply nested repository does not get
    # hidden behind an outer Git root selected as the only visible root.
    repository_ancestor = next(
        (
            candidate
            for candidate in (cwd, *cwd.parents)
            if (candidate / ".git").exists()
        ),
        None,
    )
    scan_root = repository_ancestor or cwd
    try:
        walk_errors = []
        for current, directories, files in os.walk(
            scan_root, onerror=walk_errors.append
        ):
            current_path = Path(current).resolve()
            if ".git" in directories or ".git" in files:
                roots.add(current_path)
                directories[:] = [name for name in directories if name != ".git"]
            if len(roots) > 1:
                return None
    except OSError:
        return None
    if walk_errors:
        return None

    if len(roots) == 1:
        return next(iter(roots))
    if roots or len(child_contexts) > 1:
        return None
    # A global context with no discoverable repository root is ambiguous. The
    # current directory may be a workspace containing repositories nested at
    # arbitrary depth, so using it as a prefix could expose all of them.
    return None


def _effective_char_limit(max_tokens: int, max_chars_direct: int) -> int:
    """Return the effective character budget from both config knobs.

    Picks the stricter (smallest) non-zero value.  Returns 0 when both
    are zero, meaning no limit is active.
    """
    candidates = [c for c in (max_tokens * _CHARS_PER_TOKEN, max_chars_direct) if c > 0]
    return min(candidates) if candidates else 0


def _limit_source_label(max_tokens: int, max_chars_direct: int, effective: int) -> str:
    """Human-readable description of which config key produced *effective*."""
    token_chars = max_tokens * _CHARS_PER_TOKEN
    both_active = token_chars > 0 and max_chars_direct > 0
    if both_active:
        winner = "MAX_TOOL_RESPONSE_TOKENS" if token_chars <= max_chars_direct else "MAX_PROMPT_CHARS"
        return f"{winner} ({effective} chars)"
    if token_chars > 0:
        return f"MAX_TOOL_RESPONSE_TOKENS ({max_tokens} tokens → {effective} chars)"
    return f"MAX_PROMPT_CHARS ({effective} chars)"


def _pagination_retry_metadata(payload=None, fallback=None):
    """Build a smaller same-page retry hint from structured pagination data."""
    source = payload if isinstance(payload, dict) else None
    nested = source.get("continuation") if source else None
    if not isinstance(nested, dict) and isinstance(fallback, dict):
        nested = fallback.get("continuation")
    if isinstance(nested, dict):
        retry = {}
        for offset_key, page_size_key in (
            ("offset", "page_size"),
            ("table_offset", "table_page_size"),
            ("column_offset", "column_page_size"),
            ("key_pattern_offset", "key_pattern_page_size"),
        ):
            offset = nested.get(offset_key)
            if type(offset) is not int:
                continue
            retry[offset_key] = offset
            page_size = nested.get(page_size_key)
            # A prior truncation already reduced this page. Reduce it again if
            # the protocol wrapper still cannot fit the serialized response.
            if type(page_size) is int and page_size > 0:
                retry[page_size_key] = max(1, page_size // 2)
        if retry:
            return retry

    if source and isinstance(source.get("offset"), int):
        offset = source["offset"]
        page_size = source.get("page_size")
    elif isinstance(fallback, dict) and isinstance(fallback.get("offset"), int):
        offset = fallback["offset"]
        page_size = fallback.get("page_size")
    else:
        return None

    continuation = {"offset": offset}
    if isinstance(page_size, int) and page_size > 0:
        continuation["page_size"] = max(1, page_size // 2)
    return continuation


def _valid_jsonrpc_id(request_id) -> bool:
    """Keep reflected response IDs small enough for the hard envelope cap."""
    if request_id is None:
        return True
    if type(request_id) not in (str, int, float):
        return False
    try:
        serialized = json.dumps(
            request_id, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return False
    return len(serialized) <= MAX_JSONRPC_ID_BYTES


def _apply_response_token_limit(tool_name: str, text: str, *, pagination=None) -> str:
    """Truncate *text* to the configured budget and append a visible notice.

    Reads two independent config knobs at call time so live changes are
    respected without a server restart:

    * ``MAX_TOOL_RESPONSE_TOKENS`` — token-based cap (4 chars/token)
    * ``MAX_PROMPT_CHARS``         — direct character cap

    The stricter non-zero value wins.  When both are 0 the text is returned
    unchanged.  A structured warning is printed to stdout whenever truncation
    fires so operators can trace oversized payloads.
    """
    from .cli.config_manager import get_config_value

    raw_tokens = get_config_value("MAX_TOOL_RESPONSE_TOKENS") or "0"
    try:
        max_tokens = int(raw_tokens)
    except ValueError:
        max_tokens = 0

    raw_chars = get_config_value("MAX_PROMPT_CHARS") or "0"
    try:
        max_chars_direct = int(raw_chars)
    except ValueError:
        max_chars_direct = 0

    max_chars = _effective_char_limit(max_tokens, max_chars_direct)
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    label = _limit_source_label(max_tokens, max_chars_direct, max_chars)
    notice = (
        f"[CGC] Response truncated: output exceeded {label} "
        f"for tool '{tool_name}'. "
        "Increase the limit or narrow your query for full results."
    )

    print(
        f"[CGC WARNING] Truncation fired | tool={tool_name} | "
        f"original_chars={len(text)} | truncated_chars={max_chars} | "
        f"limit={label}",
        file=sys.stderr,
        flush=True,
    )

    budget = max(0, max_chars - 200)
    try:
        payload = json.loads(text)
        preview = json.dumps(payload, indent=2)
        if len(preview) <= budget:
            return preview
        truncated_payload = {"truncated": True, "preview": preview[:budget], "notice": notice}
        continuation = _pagination_retry_metadata(payload, pagination)
        if continuation is not None:
            truncated_payload["continuation"] = continuation
        return json.dumps(truncated_payload, indent=2)
    except json.JSONDecodeError:
        truncated_payload = {"truncated": True, "preview": text[:budget], "notice": notice}
        continuation = _pagination_retry_metadata(fallback=pagination)
        if continuation is not None:
            truncated_payload["continuation"] = continuation
        return json.dumps(truncated_payload, indent=2)


def _apply_final_output_limits(
    tool_name: str,
    response_text: str,
    serialize_wrapped,
    *,
    max_bytes: int = MAX_TOOL_RESPONSE_BYTES,
    max_lines: int = MAX_TOOL_RESPONSE_LINES,
    pagination=None,
) -> str:
    """Bound the final serialized protocol result, retaining retry metadata."""
    serialized = serialize_wrapped(response_text)
    if isinstance(serialized, str):
        serialized = serialized.encode("utf-8")
    line_count = len(response_text.splitlines())
    if len(serialized) <= max_bytes and line_count <= max_lines:
        return response_text

    try:
        payload = json.loads(response_text)
    except (TypeError, json.JSONDecodeError):
        payload = None

    continuation = _pagination_retry_metadata(payload, pagination)

    base = {
        "truncated": True,
        "notice": "Final serialized response limit exceeded.",
    }
    if continuation is not None:
        base["continuation"] = continuation

    def candidate_for(preview_length: int) -> str:
        candidate = dict(base)
        if preview_length:
            candidate["preview"] = response_text[:preview_length]
        return json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))

    max_preview_length = len(response_text)
    if line_count > max_lines:
        if max_lines <= 0:
            max_preview_length = 0
        else:
            max_preview_length = sum(
                len(line) for line in response_text.splitlines(keepends=True)[:max_lines]
            )

    low, high = 0, max_preview_length
    best = candidate_for(0)
    while low <= high:
        middle = (low + high) // 2
        candidate = candidate_for(middle)
        candidate_size = serialize_wrapped(candidate)
        if isinstance(candidate_size, str):
            candidate_size = candidate_size.encode("utf-8")
        if len(candidate_size) <= max_bytes:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1

    best_size = serialize_wrapped(best)
    if isinstance(best_size, str):
        best_size = best_size.encode("utf-8")
    if len(best_size) <= max_bytes:
        return best

    # At production limits the fixed protocol envelope is small. If a caller
    # chooses an impractically tiny test/config limit, keep the content valid.
    return json.dumps({"truncated": True, "tool": tool_name}, separators=(",", ":"))


def _serialize_bounded_jsonrpc_response(response: Dict[str, Any]) -> str:
    """Serialize a complete stdio response within the hard byte ceiling."""
    serialized = json.dumps(response)
    if len(serialized.encode("utf-8")) <= MAX_TOOL_RESPONSE_BYTES:
        return serialized

    error = response.get("error")
    error_code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(error_code, int):
        error_code = -32603
    bounded = {
        "jsonrpc": "2.0",
        "id": response.get("id"),
        "error": {
            "code": error_code,
            "message": "Serialized response exceeded the maximum size.",
        },
    }
    serialized = json.dumps(bounded, separators=(",", ":"))
    if len(serialized.encode("utf-8")) <= MAX_TOOL_RESPONSE_BYTES:
        return serialized

    # Request IDs are validated before dispatch. Keep a final minimal response
    # available in case a future caller bypasses that guard.
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32603,
                "message": "Serialized response exceeded the maximum size.",
            },
        },
        separators=(",", ":"),
    )


class MCPServer:
    """
    The main MCP Server class.
    
    This class orchestrates all the major components of the application, including:
    - Database connection management (`DatabaseManager` or `FalkorDBManager`)
    - Background job tracking (`JobManager`)
    - File system watching for live updates (`CodeWatcher`)
    - Tool handlers for graph building, code searching, etc.
    - The main JSON-RPC communication loop for interacting with an AI assistant.
    """

    def __init__(
        self,
        loop=None,
        cwd: Path | None = None,
        initialize_database: bool = True,
    ):
        """
        Initializes the MCP server and its components. 
        
        Args:
            loop: The asyncio event loop to use. If not provided, it gets the current
                  running loop or creates a new one.
            cwd: Working directory used for context resolution. Defaults to Path.cwd().
            initialize_database: Whether to connect to the selected database during
                construction. API lifespan startup defers remote connectivity so
                liveness can remain available during a remote outage.
        """
        self.cwd = (cwd or Path.cwd()).resolve()
        # Context switches temporarily replace the runtime backend. Retain the
        # value from process startup so returning to global restores the caller's
        # explicit override (or lets the configured global default take effect).
        # Project dotenv values may have been promoted to os.environ by the
        # CLI. Snapshot the global view so those values do not pin the backend
        # when this server later switches back from a repository context.
        with _global_context_environment():
            self._runtime_db_type_before_context_switch = os.environ.get(
                "CGC_RUNTIME_DB_TYPE"
            )
            self._runtime_db_path_before_context_switch = os.environ.get(
                "CGC_RUNTIME_DB_PATH"
            )
        self._clear_context_default_database_on_global_restore = (
            self._runtime_db_type_before_context_switch is None
            and os.environ.get("DEFAULT_DATABASE") is not None
            and os.environ.get("CGC_DB_SELECTION_SOURCE", "").startswith(
                "context ("
            )
        )
        self._startup_global_context: Optional[ResolvedContext] = None
        # `cwd` remains the server's workspace/config anchor across context
        # switches. Query defaults follow the repository currently selected by
        # switch_context, which may differ from that original directory.
        self.active_repo_path = None
        self.discovered_child_contexts: List[dict] = []
        self._context_note_pending = False
        self.disabled_tools: Set[str] = set()
        self._context_transition_lock = threading.RLock()

        try:
            ctx = resolve_context(cwd=self.cwd)
            self.resolved_context = ctx
            global_context_for_restore = (
                _resolve_global_context_for_restore()
                if ctx.mode != "global"
                else None
            )
            self.active_repo_path = _initial_active_repo_path(self.cwd, ctx)

            # Keep only an inherited LadybugDB default implicit so the factory
            # can auto-detect remote FalkorDB or choose an installed fallback.
            if (
                ctx.database
                and not os.environ.get('CGC_RUNTIME_DB_TYPE')
                and not (
                    ctx.database == "ladybugdb"
                    and not ctx.database_is_explicit
                    and not os.environ.get("DEFAULT_DATABASE")
                )
            ):
                os.environ['CGC_RUNTIME_DB_TYPE'] = ctx.database

            runtime_db_path = os.environ.get('CGC_RUNTIME_DB_PATH')
            self.db_manager = get_database_manager(db_path=runtime_db_path or ctx.db_path)
            selected_database = getattr(
                self.db_manager, "get_backend_type", lambda: ctx.database
            )()
            if isinstance(selected_database, str) and selected_database:
                ctx.database = selected_database
                os.environ["CGC_RUNTIME_DB_TYPE"] = selected_database
            selected_db_path = getattr(self.db_manager, "db_path", None)
            if (
                isinstance(selected_db_path, str)
                and selected_db_path
                and not runtime_db_path
            ):
                ctx.db_path = selected_db_path
            if ctx.mode == "global":
                self._startup_global_context = replace(
                    ctx, db_path=runtime_db_path or ctx.db_path
                )
            elif global_context_for_restore is not None:
                self._startup_global_context = global_context_for_restore
            if initialize_database:
                self.db_manager.get_driver()

            if not ctx.is_local:
                try:
                    children = discover_child_contexts(self.cwd, max_depth=1)
                    self.active_repo_path = _initial_active_repo_path(
                        self.cwd, ctx, children
                    )
                    if children:
                        self.discovered_child_contexts = [asdict(c) for c in children]
                        self._context_note_pending = True
                except Exception:
                    pass
        except ValueError as e:
            raise ValueError(f"Database configuration error: {e}")

        # Initialize managers for jobs and file watching.
        self.job_manager = JobManager()
        
        # Get the current event loop to pass to thread-sensitive components like the graph builder.
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        self.loop = loop

        # Keep the usual MCP process eager while allowing API lifespan startup
        # to defer remote connectivity until a database-backed route runs.
        self._graph_builder = None
        self.code_finder = CodeFinder(self.db_manager)
        self._code_watcher = None
        if initialize_database:
            self.graph_builder = GraphBuilder(self.db_manager, self.job_manager, loop)
            self.code_watcher = CodeWatcher(self.graph_builder, self.job_manager)
        
        # Define the tool manifest that will be exposed to the AI assistant.
        self._init_tools()

    @property
    def graph_builder(self):
        if self._graph_builder is None:
            self._graph_builder = GraphBuilder(
                self.db_manager, self.job_manager, self.loop
            )
        return self._graph_builder

    @graph_builder.setter
    def graph_builder(self, value):
        self._graph_builder = value

    @property
    def code_watcher(self):
        if self._code_watcher is None:
            self._code_watcher = CodeWatcher(self.graph_builder, self.job_manager)
        return self._code_watcher

    @code_watcher.setter
    def code_watcher(self, value):
        self._code_watcher = value

    def _init_tools(self):
        """
        Defines the complete tool manifest for the LLM.
        """
        self.disabled_tools = self._load_disabled_tools()
        self.tools = {
            name: definition
            for name, definition in TOOLS.items()
            if name not in self.disabled_tools
        }

    def _normalize_tool_name(self, name: Any) -> Optional[str]:
        """Normalize tool names from mcp.json to internal tool identifiers."""
        if not isinstance(name, str):
            return None

        normalized = name.strip()
        if not normalized:
            return None

        if normalized.startswith("codegraphcontext_"):
            normalized = normalized[len("codegraphcontext_"):]

        aliases = {
            "add_code_to_folder": "add_code_to_graph",
        }

        return aliases.get(normalized, normalized)

    def _load_disabled_tools(self) -> Set[str]:
        """Load disabled tool names from `<cwd>/mcp.json` config."""
        mcp_file = self.cwd / "mcp.json"
        if not mcp_file.exists():
            return set()

        try:
            with open(mcp_file, "r", encoding="utf-8") as f:
                mcp_config = json.load(f)
        except Exception as exc:
            warning_logger(f"Failed to read {mcp_file}: {exc}")
            return set()

        disabled_tools = get_instance_mcp_config(mcp_config).get("tools", {})
        disabled_tools = disabled_tools.get("disabledTools", []) if isinstance(disabled_tools, dict) else []

        if not isinstance(disabled_tools, list):
            warning_logger("mcp.json tools.disabledTools must be a list; ignoring invalid value.")
            return set()

        normalized_disabled: Set[str] = set()
        unknown_tools: List[str] = []

        for name in disabled_tools:
            normalized_name = self._normalize_tool_name(name)
            if not normalized_name:
                continue

            if normalized_name in TOOLS:
                normalized_disabled.add(normalized_name)
            else:
                unknown_tools.append(str(name))

        if unknown_tools:
            warning_logger(
                "Ignoring unknown tools in mcp.json tools.disabledTools: "
                + ", ".join(sorted(set(unknown_tools)))
            )

        return normalized_disabled

    def get_database_status(self) -> dict:
        """Returns the current connection status of the Neo4j database."""
        return {"connected": self.db_manager.is_connected()}
        

    # --- Tool Wrappers ---
    # These methods delegate to the functional handlers, injecting the necessary dependencies.

    def execute_cypher_query_tool(self, **args) -> Dict[str, Any]:
        return query_handlers.execute_cypher_query(self.db_manager, **args)
    
    def visualize_graph_query_tool(self, **args) -> Dict[str, Any]:
        return query_handlers.visualize_graph_query(self.db_manager, **args)

    def find_dead_code_tool(self, **args) -> Dict[str, Any]:
        return analysis_handlers.find_dead_code(self.code_finder, **args)

    def calculate_cyclomatic_complexity_tool(self, **args) -> Dict[str, Any]:
        return analysis_handlers.calculate_cyclomatic_complexity(self.code_finder, **args)

    def find_most_complex_functions_tool(self, **args) -> Dict[str, Any]:
        return analysis_handlers.find_most_complex_functions(self.code_finder, **args)

    def analyze_code_relationships_tool(self, **args) -> Dict[str, Any]:
        return analysis_handlers.analyze_code_relationships(self.code_finder, **args)

    def find_code_tool(self, **args) -> Dict[str, Any]:
        return analysis_handlers.find_code(self.code_finder, **args)

    def list_indexed_repositories_tool(self, **args) -> Dict[str, Any]:
        return management_handlers.list_indexed_repositories(self.code_finder, **args)

    def delete_repository_tool(self, **args) -> Dict[str, Any]:
        return management_handlers.delete_repository(self.graph_builder, **args)

    def check_job_status_tool(self, **args) -> Dict[str, Any]:
        return management_handlers.check_job_status(self.job_manager, **args)

    def cancel_indexing_job_tool(self, **args) -> Dict[str, Any]:
        return management_handlers.cancel_indexing_job(self.job_manager, **args)
    
    def list_jobs_tool(self, **args: Any) -> Dict[str, Any]:
        return management_handlers.list_jobs(self.job_manager)

    def list_watched_paths_tool(self, **args) -> Dict[str, Any]:
        return watcher_handlers.list_watched_paths(self.code_watcher, **args)

    def unwatch_directory_tool(self, **args) -> Dict[str, Any]:
        return watcher_handlers.unwatch_directory(self.code_watcher, **args)

    def add_code_to_graph_tool(self, **args) -> Dict[str, Any]:
        # Register the pending job before a concurrent context switch can pass
        # its active-job guard and tear down the DB this handler is preparing to use.
        with self._context_transition_lock:
            return indexing_handlers.add_code_to_graph(
                self.graph_builder,
                self.job_manager,
                self.loop,
                self.list_indexed_repositories_tool,
                **args
            )
    
    def add_package_to_graph_tool(self, **args) -> Dict[str, Any]:
        with self._context_transition_lock:
            return indexing_handlers.add_package_to_graph(
                self.graph_builder,
                self.job_manager,
                self.loop,
                self.list_indexed_repositories_tool,
                **args
            )

    def watch_directory_tool(self, **args) -> Dict[str, Any]:
        # watch_directory needs to call metadata tools.
        return watcher_handlers.watch_directory(
            self.code_watcher,
            self.list_indexed_repositories_tool,
            self.add_code_to_graph_tool,
            **args
        )

    def load_bundle_tool(self, **args) -> Dict[str, Any]:
        return management_handlers.load_bundle(self.code_finder, **args)
    
    def search_registry_bundles_tool(self, **args) -> Dict[str, Any]:
        return management_handlers.search_registry_bundles(self.code_finder, **args)
    
    def get_repository_stats_tool(self, **args) -> Dict[str, Any]:
        return management_handlers.get_repository_stats(self.code_finder, **args)

    def list_graphs_tool(self, **args) -> Dict[str, Any]:
        return management_handlers.list_graphs(self.db_manager, **args)

    def generate_report_tool(self, **args) -> Dict[str, Any]:
        from .tools.report_generator import generate_report

        output_path_raw = args.get("output_path")
        output_path = Path(output_path_raw) if output_path_raw else self.cwd / "CGC_REPORT.md"

        from .utils.path_sandbox import is_path_allowed

        output_path = output_path.resolve()
        if not is_path_allowed(output_path):
            return {"error": "Invalid output_path: path is outside allowed roots"}

        try:
            report = generate_report(
                self.db_manager,
                output_path=output_path,
                include_java=bool(args.get("include_java", False)),
                repo_path=args.get("repo_path"),
                god_node_limit=int(args.get("god_node_limit", 15)),
                complexity_limit=int(args.get("complexity_limit", 15)),
                cross_module_limit=int(args.get("cross_module_limit", 20)),
                )
            return {"status": "ok", "output_path": str(output_path), "report": report}
        except Exception as exc:
            return {"error": str(exc)}

    def find_java_spring_endpoints_tool(self, **args) -> Dict[str, Any]:
        return analysis_handlers.find_java_spring_endpoints(self.code_finder, **args)

    def find_java_spring_beans_tool(self, **args) -> Dict[str, Any]:
        return analysis_handlers.find_java_spring_beans(self.code_finder, **args)

    def find_datasource_nodes_tool(self, **args) -> Dict[str, Any]:
        return analysis_handlers.find_datasource_nodes(self.code_finder, **args)

    def simulate_metrics_tool(self, **args) -> Dict[str, Any]:
        from .core.simulator import CodeGraphTwin
        repo_path = args.get("repo_path") or args.get("path") or str(self.cwd)
        try:
            twin = CodeGraphTwin(repo_path).load_from_db(self.db_manager)
            return {"status": "ok", "metrics": twin.get_metrics_summary()}
        except Exception as e:
            return {"error": f"Failed to calculate simulation metrics: {e}"}

    def simulate_architectural_change_tool(self, **args) -> Dict[str, Any]:
        from .core.simulator import CodeGraphTwin
        repo_path = args.get("repo_path") or args.get("path") or str(self.cwd)
        changes = args.get("changes", [])
        try:
            baseline = CodeGraphTwin(repo_path).load_from_db(self.db_manager)
            simulated = CodeGraphTwin(repo_path).load_from_db(self.db_manager)
            
            for step in changes:
                stype = step.get("type")
                if stype == "decompose":
                    simulated.decompose_service(step.get("mapping", {}))
                elif stype == "remove_dependency":
                    simulated.remove_dependency(step.get("source"), step.get("target"), step.get("rel_type"))
                elif stype == "add_dependency":
                    simulated.add_dependency(step.get("source"), step.get("target"), step.get("rel_type"))
                elif stype == "remove_node":
                    simulated.remove_node(step.get("node_id"))
                    
            diff = baseline.compare_scenarios(simulated)
            return {
                "status": "ok",
                "baseline_metrics": baseline.get_metrics_summary(),
                "simulated_metrics": simulated.get_metrics_summary(),
                "comparison": diff
            }
        except Exception as e:
            return {"error": f"Failed to simulate architectural change: {e}"}

    def analyze_architectural_evolution_tool(self, **args) -> Dict[str, Any]:
        from .core.simulator import EvolutionTimeline
        repo_path = args.get("repo_path") or args.get("path") or str(self.cwd)
        commits = int(args.get("commits", 50))
        try:
            hotspots = EvolutionTimeline.analyze_hotspots(self.db_manager, repo_path, num_commits=commits)
            growth = EvolutionTimeline.get_growth_trend(repo_path, num_commits=min(15, commits))
            formatted_hotspots = []
            for hs in hotspots:
                formatted_hotspots.append({
                    "relative_path": hs["relative_path"],
                    "churn": hs["churn"],
                    "complexity": hs["complexity"],
                    "hotspot_score": hs["hotspot_score"]
                })
            return {
                "status": "ok",
                "hotspots": formatted_hotspots,
                "growth_trend": growth
            }
        except Exception as e:
            return {"error": f"Failed to analyze architectural evolution: {e}"}

    def discover_codegraph_contexts_tool(self, **args) -> Dict[str, Any]:
        from .utils.path_sandbox import is_path_allowed, clamp_discovery_depth

        scan_path = Path(args.get("path") or args.get("repo_path") or str(self.cwd)).resolve()
        if not is_path_allowed(scan_path):
            return {
                "error": (
                    f"Path '{scan_path}' is outside allowed roots. "
                    "Set CGC_ALLOWED_ROOTS to scan additional directories."
                )
            }
        max_depth = clamp_discovery_depth(args.get("max_depth", 1))
        try:
            children = discover_child_contexts(scan_path, max_depth=max_depth)
            if not children:
                return {
                    "status": "no_contexts_found",
                    "message": f"No repository context folders found under {scan_path} (depth={max_depth}).",
                    "contexts": [],
                }
            return {
                "status": "ok",
                "message": f"Found {len(children)} context(s) under {scan_path}.",
                "contexts": [asdict(c) for c in children],
            }
        except Exception as e:
            return {"error": f"Discovery failed: {e}"}

    def _stop_current_watcher(self) -> bool:
        """Stop the active code watcher (if any). Returns whether it was running."""
        was_running = False
        try:
            watcher = getattr(self, "code_watcher", None)
            if watcher is not None:
                was_running = watcher.observer.is_alive()
                watcher.stop()
        except Exception as exc:
            warning_logger(f"Failed to stop old code watcher during context switch: {exc}")
        return was_running

    def _start_watcher_if(self, should_start: bool) -> None:
        """Start the (new) code watcher when the previous one was running."""
        if not should_start:
            return
        try:
            self.code_watcher.start()
        except Exception as exc:
            warning_logger(f"Failed to start new code watcher after context switch: {exc}")

    def _close_db_manager_for_context_switch(
        self, watcher_was_running: bool
    ) -> Optional[Dict[str, Any]]:
        """Close the current DB before replacement, retaining it if its lease survives."""
        manager = self.db_manager
        try:
            _teardown_db_manager(manager)
        except Exception as exc:
            warning_logger(f"Failed to tear down old DB manager during context switch: {exc}")

        lease = getattr(manager, "_ownership_lease", None)
        if isinstance(lease, DatabaseOwnershipLease) and lease.is_acquired:
            self._start_watcher_if(watcher_was_running)
            return {
                "error": (
                    "Cannot switch context because the current embedded database "
                    "could not be closed. Its ownership lease remains held; retry "
                    "after close succeeds or restart the process."
                )
            }
        return None

    def _active_jobs_block_context_switch(self) -> Optional[Dict[str, Any]]:
        """Refuse context switches while indexing jobs still hold the current DB (#1536)."""
        active = self.job_manager.list_active_jobs()
        if not active:
            return None
        job_ids = [j.job_id for j in active]
        details = [
            {"job_id": j.job_id, "status": j.status.value, "path": j.path}
            for j in active
        ]
        return {
            "error": (
                "Cannot switch context while indexing jobs are still active "
                f"({', '.join(job_ids)}). Wait for them to finish "
                "(check_job_status / list_jobs), then retry switch_context."
            ),
            "active_jobs": details,
        }

    def switch_context_tool(self, **args) -> Dict[str, Any]:
        with self._context_transition_lock:
            return self._switch_context_tool_locked(**args)

    def _switch_context_tool_locked(self, **args) -> Dict[str, Any]:
        raw_path = args.get("context_path", "")
        should_save = args.get("save", True)

        if not raw_path:
            return {"error": "context_path is required."}

        # --- Special case: switch back to the global context ---
        if raw_path == "global":
            blocked = self._active_jobs_block_context_switch()
            if blocked:
                return blocked
            runtime_db_type_before_switch = os.environ.get("CGC_RUNTIME_DB_TYPE")
            default_database_before_switch = os.environ.get("DEFAULT_DATABASE")
            clear_context_default_database = getattr(
                self, "_clear_context_default_database_on_global_restore", False
            )
            global_runtime_db_type = getattr(
                self,
                "_runtime_db_type_before_context_switch",
                runtime_db_type_before_switch,
            )
            try:
                watcher_was_running = self._stop_current_watcher()
                close_error = self._close_db_manager_for_context_switch(
                    watcher_was_running
                )
                if close_error:
                    return close_error

                # Resolve global DB path directly — do NOT use resolve_context()
                # because that checks CWD for a local repository context and may
                # return per-repo instead of global.
                _set_runtime_db_type(global_runtime_db_type)
                startup_global_context = getattr(self, "_startup_global_context", None)
                if startup_global_context is not None:
                    db = global_runtime_db_type or startup_global_context.database
                    global_db_path = getattr(
                        self, "_runtime_db_path_before_context_switch", None
                    ) or startup_global_context.db_path
                else:
                    resolved_global_context = _resolve_global_context_for_restore()
                    db = global_runtime_db_type or resolved_global_context.database
                    global_db_path = getattr(
                        self, "_runtime_db_path_before_context_switch", None
                    ) or resolved_global_context.db_path
                if clear_context_default_database:
                    # CLI credential loading can promote the selected repo
                    # backend to DEFAULT_DATABASE. The global snapshot already
                    # resolved the caller's global config without that value.
                    os.environ.pop("DEFAULT_DATABASE", None)
                with _global_context_environment():
                    # Leave the implicit LadybugDB default unset so the factory
                    # can use an installed embedded fallback or global remote
                    # configuration while project settings are masked.
                    if global_runtime_db_type or db != "ladybugdb":
                        _set_runtime_db_type(db)
                    new_manager = get_database_manager(db_path=global_db_path)
                    new_manager.get_driver()

                    selected_database = getattr(
                        new_manager, "get_backend_type", lambda: db
                    )()
                    if isinstance(selected_database, str) and selected_database:
                        db = selected_database
                    selected_db_path = getattr(new_manager, "db_path", None)
                    if isinstance(selected_db_path, str) and selected_db_path:
                        global_db_path = selected_db_path

                _set_runtime_db_type(db)

                self.db_manager = new_manager
                self.resolved_context = type(self.resolved_context)(
                    mode="global",
                    context_name="",
                    database=db,
                    db_path=global_db_path,
                    cgcignore_path=str(CONFIG_DIR / "global" / ".cgcignore"),
                    is_local=False,
                )
                # The global database has no selected repository. Reusing the
                # repository path from the previous local context would silently
                # constrain subsequent default queries to that stale repository.
                self.active_repo_path = None

                # Rebuild dependent components
                self.graph_builder = GraphBuilder(self.db_manager, self.job_manager, self.loop)
                self.code_finder = CodeFinder(self.db_manager)
                self.code_watcher = CodeWatcher(self.graph_builder, self.job_manager)
                self._start_watcher_if(watcher_was_running)
                self._context_note_pending = False

                return {
                    "status": "ok",
                    "message": f"Switched back to global context at {global_db_path}.",
                    "database": db,
                    "db_path": global_db_path,
                }
            except Exception as e:
                _set_runtime_db_type(runtime_db_type_before_switch)
                if clear_context_default_database:
                    if default_database_before_switch is None:
                        os.environ.pop("DEFAULT_DATABASE", None)
                    else:
                        os.environ["DEFAULT_DATABASE"] = default_database_before_switch
                return {"error": f"Failed to switch to global context: {e}"}

        # --- Normal path-based switch ---
        from .utils.path_sandbox import is_path_allowed

        target = Path(raw_path).resolve()
        if not is_path_allowed(target):
            return {"error": f"Context path '{target}' is outside allowed roots."}
        # Accept either the repo directory or its selected context directory.
        if target.name == get_repo_context_dir_name():
            cgc_dir = target
            repository_root = target.parent
        else:
            cgc_dir = get_repo_context_path(target)
            repository_root = target

        if not cgc_dir.exists() or not cgc_dir.is_dir():
            return {"error": f"No repository context directory found at {cgc_dir}."}

        blocked = self._active_jobs_block_context_switch()
        if blocked:
            return blocked

        runtime_db_type_before_switch = os.environ.get("CGC_RUNTIME_DB_TYPE")
        default_database_before_switch = os.environ.get("DEFAULT_DATABASE")
        clear_context_default_database = getattr(
            self, "_clear_context_default_database_on_global_restore", False
        )

        try:
            if clear_context_default_database:
                # A prior per-repo/named context may have promoted its backend
                # into DEFAULT_DATABASE. Resolve an unconfigured path context
                # from global config instead of inheriting that stale choice.
                os.environ.pop("DEFAULT_DATABASE", None)
            local_default_db = load_config(read_only=True).get("DEFAULT_DATABASE") or "ladybugdb"
            workspace_mapping = get_workspace_mapping(self.cwd)
            mapped_context_path = (
                workspace_mapping.get("context_path") if workspace_mapping else None
            )
            mapping_matches_context = bool(mapped_context_path) and (
                Path(mapped_context_path).resolve() == cgc_dir.resolve()
            )
            mapped_database = (
                workspace_mapping.get("database")
                if mapping_matches_context and workspace_mapping
                else None
            )
            context_database = _context_database_setting(cgc_dir)
            if mapped_database:
                mapping_database_is_explicit = bool(
                    workspace_mapping.get(
                        "database_is_explicit", mapped_database
                    )
                )
                if (
                    not mapping_database_is_explicit
                    and context_database is None
                    and str(mapped_database).lower() == "ladybugdb"
                ):
                    mapped_database = _infer_legacy_context_database(
                        cgc_dir, str(mapped_database)
                    )
                local_db, new_db_path = _existing_context_fallback_storage(
                    cgc_dir, str(mapped_database)
                )
            else:
                local_db, new_db_path = _context_database_and_path_for_path(
                    cgc_dir, local_default_db
                )
                mapping_database_is_explicit = False
            local_database_is_explicit = context_database is not None or (
                mapping_database_is_explicit
                if mapping_matches_context and mapped_database
                else _has_explicit_database_default(
                    read_only=True,
                    include_runtime=False,
                    include_project_env=False,
                )
            )

            watcher_was_running = self._stop_current_watcher()
            # Tear down old connection
            close_error = self._close_db_manager_for_context_switch(
                watcher_was_running
            )
            if close_error:
                if clear_context_default_database and default_database_before_switch:
                    os.environ["DEFAULT_DATABASE"] = default_database_before_switch
                return close_error

            startup_runtime_db_type = getattr(
                self,
                "_runtime_db_type_before_context_switch",
                runtime_db_type_before_switch,
            )
            if (
                str(local_db).lower() == "ladybugdb"
                and not local_database_is_explicit
                and startup_runtime_db_type is None
                and not os.getenv("DEFAULT_DATABASE")
            ):
                # An unconfigured path context inherits the default. Clear a
                # backend selected for the previous context so the factory can
                # apply its normal optional fallback.
                _set_runtime_db_type(None)
            else:
                os.environ['CGC_RUNTIME_DB_TYPE'] = local_db
            new_manager = get_database_manager(db_path=new_db_path)
            new_manager.get_driver()

            selected_database = getattr(new_manager, "get_backend_type", lambda: local_db)()
            if isinstance(selected_database, str) and selected_database:
                local_db = selected_database
            selected_db_path = getattr(new_manager, "db_path", None)
            if isinstance(selected_db_path, str) and selected_db_path:
                new_db_path = selected_db_path
            os.environ['CGC_RUNTIME_DB_TYPE'] = local_db

            self.db_manager = new_manager
            self.resolved_context = type(self.resolved_context)(
                mode="per-repo",
                context_name="",
                database=local_db,
                db_path=new_db_path,
                cgcignore_path=str(cgc_dir / ".cgcignore"),
                is_local=True,
                database_is_explicit=local_database_is_explicit,
            )

            # Rebuild dependent components with the new DB manager
            self.graph_builder = GraphBuilder(self.db_manager, self.job_manager, self.loop)
            self.code_finder = CodeFinder(self.db_manager)
            self.code_watcher = CodeWatcher(self.graph_builder, self.job_manager)
            self._start_watcher_if(watcher_was_running)

            if should_save:
                save_workspace_mapping(
                    self.cwd,
                    cgc_dir,
                    database=local_db,
                    database_is_explicit=local_database_is_explicit,
                )

            self.active_repo_path = repository_root
            self._context_note_pending = False

            return {
                "status": "ok",
                "message": f"Switched to context at {cgc_dir}.",
                "database": local_db,
                "db_path": new_db_path,
                "saved": should_save,
            }
        except Exception as e:
            _set_runtime_db_type(runtime_db_type_before_switch)
            if clear_context_default_database:
                if default_database_before_switch is None:
                    os.environ.pop("DEFAULT_DATABASE", None)
                else:
                    os.environ["DEFAULT_DATABASE"] = default_database_before_switch
            return {"error": f"Failed to switch context: {e}"}


    async def handle_tool_call(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Routes a tool call from the AI assistant to the appropriate handler function. 
        """
        if tool_name in self.disabled_tools:
            return {"error": f"Tool '{tool_name}' is disabled in mcp.json (disabledTools)."}

        # Normalize path/repo_path aliasing: tool schemas declare `repo_path`
        # while several handlers read `path` (and vice versa). Skip the
        # repo_path -> path direction for tools where the two keys carry
        # different meanings (path = file path, repo_path = repo filter).
        if isinstance(args, dict):
            args = dict(args)
            # path_means_file: for these tools `path` is a file disambiguator and
            # `repo_path` is a separate repo-prefix filter. Aliasing either way
            # collapses the two meanings (#1532: path → repo_path made
            # calculate_cyclomatic_complexity return null).
            path_means_file = tool_name in ("calculate_cyclomatic_complexity",)
            if "repo_path" in args and "path" not in args and not path_means_file:
                args["path"] = args["repo_path"]
            elif "path" in args and "repo_path" not in args and not path_means_file:
                args["repo_path"] = args["path"]

            repo_path = args.get("repo_path")
            if isinstance(repo_path, str) and not repo_path.strip():
                args.pop("repo_path")

            if (
                tool_name in _REPOSITORY_SCOPED_QUERY_TOOLS
                and not (
                    tool_name in {
                        "execute_cypher_query",
                        "find_datasource_nodes",
                        "visualize_graph_query",
                    }
                    and args.get("global_scope") is True
                )
                and not args.get("repo_path")
            ):
                active_repo_path = getattr(self, "active_repo_path", self.cwd)
                if active_repo_path is None:
                    return {
                        "error": (
                            "No single repository is selected for the default query scope. "
                            "Pass repo_path or switch to a repository context."
                        )
                    }
                args["repo_path"] = PureWindowsPath(
                    str(active_repo_path)
                ).as_posix()

        tool_map: Dict[str, Coroutine] = {
            "add_package_to_graph": self.add_package_to_graph_tool,
            "find_dead_code": self.find_dead_code_tool,
            "find_code": self.find_code_tool,
            "analyze_code_relationships": self.analyze_code_relationships_tool,
            "watch_directory": self.watch_directory_tool,
            "execute_cypher_query": self.execute_cypher_query_tool,
            "add_code_to_graph": self.add_code_to_graph_tool,
            "check_job_status": self.check_job_status_tool,
            "cancel_indexing_job": self.cancel_indexing_job_tool,
            "list_jobs": self.list_jobs_tool,
            "calculate_cyclomatic_complexity": self.calculate_cyclomatic_complexity_tool,
            "find_most_complex_functions": self.find_most_complex_functions_tool,
            "list_indexed_repositories": self.list_indexed_repositories_tool,
            "delete_repository": self.delete_repository_tool,
            "visualize_graph_query": self.visualize_graph_query_tool,
            "list_watched_paths": self.list_watched_paths_tool,
            "unwatch_directory": self.unwatch_directory_tool,
            "load_bundle": self.load_bundle_tool,
            "search_registry_bundles": self.search_registry_bundles_tool,
            "get_repository_stats": self.get_repository_stats_tool,
            "list_graphs": self.list_graphs_tool,
            "discover_codegraph_contexts": self.discover_codegraph_contexts_tool,
            "switch_context": self.switch_context_tool,
            "generate_report": self.generate_report_tool,
            "find_java_spring_endpoints": self.find_java_spring_endpoints_tool,
            "find_java_spring_beans": self.find_java_spring_beans_tool,
            "find_datasource_nodes": self.find_datasource_nodes_tool,
            "simulate_metrics": self.simulate_metrics_tool,
            "simulate_architectural_change": self.simulate_architectural_change_tool,
            "analyze_architectural_evolution": self.analyze_architectural_evolution_tool,
        }
        handler = tool_map.get(tool_name)
        if handler:
            if tool_name == "cancel_indexing_job":
                job_id = args.get("job_id")
                result = (
                    await self.job_manager.cancel_job_async(str(job_id))
                    if job_id
                    else {"error": "Job ID is a required argument."}
                )
            else:
                handler_task = asyncio.create_task(asyncio.to_thread(handler, **args))
                try:
                    result = await asyncio.shield(handler_task)
                except asyncio.CancelledError:
                    await wait_task_until_stopped(handler_task)
                    try:
                        result = handler_task.result()
                    except BaseException:
                        result = None
                    if tool_name in ("add_code_to_graph", "add_package_to_graph") and isinstance(result, dict):
                        job_id = result.get("job_id")
                        if job_id:
                            await self.job_manager.cancel_job_async(job_id)
                    raise

            if self._context_note_pending and tool_name not in (
                "discover_codegraph_contexts", "switch_context"
            ):
                names = [c["repo_name"] for c in self.discovered_child_contexts]
                note = (
                    f"NOTE: No {get_instance_name()} database was found at the current workspace root. "
                    f"However, the following child directories have indexed databases: {names}. "
                    "Use the `switch_context` tool to connect to one, or "
                    "`discover_codegraph_contexts` for a deeper scan."
                )
                if isinstance(result, dict):
                    result["_context_discovery_note"] = note
                self._context_note_pending = False

            return result
        else:
            return {"error": f"Unknown tool: {tool_name}"}

    async def run(self):
        """
        Runs the main server loop, listening for JSON-RPC requests from stdin.
        """
        # info_logger("MCP Server is running. Waiting for requests...")
        print("MCP Server is running. Waiting for requests...", file=sys.stderr, flush=True)
        self.code_watcher.start()
        
        loop = asyncio.get_event_loop()
        try:
            await self._run_loop(loop)
        finally:
            # Release watcher/DB resources even when stdin reaches EOF or the
            # loop exits unexpectedly.
            try:
                self.shutdown()
            except Exception as exc:
                warning_logger(f"Error during server shutdown: {exc}")

    async def _run_loop(self, loop):
        request_count = 0
        active_requests = {}
        cancelling_requests = set()
        pending_tool_requests = set()

        async def dispatch_stdio_tool_call(
            has_request_id, request_id, tool_name, args
        ):
            try:
                result = await dispatch_tool_call(self, tool_name, args)
                if not has_request_id:
                    return
                if result.error_kind == "tool":
                    error_text = json.dumps(result.payload, ensure_ascii=False)
                    error_text = _apply_final_output_limits(
                        tool_name,
                        error_text,
                        lambda text: json.dumps({
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": -32000,
                                "message": "Tool execution error",
                                "data": json.loads(text),
                            },
                        }).encode("utf-8"),
                        pagination=result.payload,
                    )
                    response = {
                        "jsonrpc": "2.0", "id": request_id,
                        "error": {
                            "code": -32000,
                            "message": "Tool execution error",
                            "data": json.loads(error_text),
                        },
                    }
                elif result.error_kind == "invalid_params":
                    response = {
                        "jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32602, "message": result.error_message}
                    }
                else:
                    response_text = encode_response(result.payload)
                    response_text = _apply_response_token_limit(
                        tool_name, response_text, pagination=result.payload
                    )
                    response_text = _apply_final_output_limits(
                        tool_name,
                        response_text,
                        lambda text: json.dumps({
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": {
                                "content": [{"type": "text", "text": text}]
                            },
                        }).encode("utf-8"),
                        pagination=result.payload,
                    )
                    response = {
                        "jsonrpc": "2.0", "id": request_id,
                        "result": {"content": [{"type": "text", "text": response_text}]}
                    }
                print(_serialize_bounded_jsonrpc_response(response), flush=True)
            except asyncio.CancelledError:
                if has_request_id:
                    print(
                        _serialize_bounded_jsonrpc_response({
                            "jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32800, "message": "Request cancelled"},
                        }),
                        flush=True,
                    )
            except Exception as e:
                error_logger(f"Error processing request: {e}\n{traceback.format_exc()}")
                if has_request_id:
                    print(
                        _serialize_bounded_jsonrpc_response({
                            "jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32603, "message": f"Internal error: {str(e)}"},
                        }),
                        flush=True,
                    )

        while True:
            # Reset per iteration: on a parse error `request` would otherwise
            # still hold the PREVIOUS request, and the error response would
            # reuse its id — a second response for an already-answered id,
            # which desyncs clients that match responses by id.
            request = None
            try:
                if request_count and request_count % 50 == 0:
                    self.job_manager.cleanup_old_jobs(max_age_hours=24)
                # Read a request from the standard input.
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    debug_logger("Client disconnected (EOF received). Shutting down.")
                    if pending_tool_requests:
                        await asyncio.gather(*pending_tool_requests, return_exceptions=True)
                    break
                
                request = json.loads(line.strip())
                method = request.get('method')
                params = request.get('params', {})
                request_id = request.get('id')
                has_request_id = "id" in request
                request_count += 1

                if has_request_id and not _valid_jsonrpc_id(request_id):
                    response = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32600,
                            "message": "Request id is invalid or exceeds the supported size.",
                        },
                    }
                    print(_serialize_bounded_jsonrpc_response(response), flush=True)
                    continue
                
                response = {}
                # Route the request based on the JSON-RPC method.
                if method == 'initialize':
                    # Build system prompt with custom prompts if any
                    system_prompt = build_system_prompt()
                    
                    response = {
                        "jsonrpc": "2.0", "id": request_id,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "serverInfo": {
                                "name": get_instance_name(),
                                "version": MCP_SERVER_VERSION,
                                "systemPrompt": system_prompt,
                                "instructionsAvailable": True,
                            },
                            "capabilities": {"tools": {"listTools": True}},
                            "instructions": LLM_SYSTEM_PROMPT,
                        }
                    }
                elif method == 'tools/list':
                    # Return the list of tools defined in _init_tools.
                    response = {
                        "jsonrpc": "2.0", "id": request_id,
                        "result": {"tools": list(self.tools.values())}
                    }
                elif method == 'tools/call':
                    # Dispatch independently so the loop can read a cancellation
                    # notification while a long-running tool is still active.
                    tool_name = params.get('name')
                    args = params.get('arguments', {})
                    tool_task = asyncio.create_task(
                        dispatch_stdio_tool_call(
                            has_request_id, request_id, tool_name, args
                        )
                    )
                    pending_tool_requests.add(tool_task)
                    if request_id is not None:
                        cancelling_requests.discard(request_id)
                        active_requests[request_id] = tool_task

                    def _forget_tool_request(task, req_id=request_id):
                        pending_tool_requests.discard(task)
                        if req_id is not None and active_requests.get(req_id) is task:
                            del active_requests[req_id]
                            cancelling_requests.discard(req_id)
                        # A task canceled before its coroutine's first step
                        # cannot run the cancellation response handler above.
                        if task.cancelled() and req_id is not None:
                            print(
                                json.dumps({
                                    "jsonrpc": "2.0", "id": req_id,
                                    "error": {"code": -32800, "message": "Request cancelled"},
                                }),
                                flush=True,
                            )

                    tool_task.add_done_callback(_forget_tool_request)
                elif method == 'notifications/cancelled':
                    target_request_id = params.get("requestId") if isinstance(params, dict) else None
                    _cancel_stdio_tool_request(
                        target_request_id, active_requests, cancelling_requests
                    )
                elif method == 'notifications/initialized':
                    # This is a notification, no response needed.
                    pass
                else:
                    # Handle unknown methods.
                    if has_request_id:
                        response = {
                            "jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32601, "message": f"Method not found: {method}"}
                        }
                
                # Send the response to standard output if it's not a notification.
                if has_request_id and response:
                    print(_serialize_bounded_jsonrpc_response(response), flush=True)

            except Exception as e:
                error_logger(f"Error processing request: {e}\n{traceback.format_exc()}")
                # JSON-RPC: parse errors respond with id null and -32700;
                # errors while handling a parsed request keep that request's id.
                request_id = request.get('id') if isinstance(request, dict) else None
                is_parse_error = not isinstance(request, dict)
                has_request_id = isinstance(request, dict) and "id" in request

                error_response = {
                    "jsonrpc": "2.0", "id": request_id,
                    "error": {
                        "code": -32700 if is_parse_error else -32603,
                        "message": (
                            f"Parse error: {str(e)}" if is_parse_error
                            else f"Internal error: {str(e)}"
                        ),
                    },
                }
                if is_parse_error or has_request_id:
                    print(_serialize_bounded_jsonrpc_response(error_response), flush=True)

    def shutdown(self):
        """Gracefully shuts down the server and its components."""
        debug_logger("Shutting down server...")
        if self._code_watcher is not None:
            self._code_watcher.stop()
        _teardown_db_manager(self.db_manager)
