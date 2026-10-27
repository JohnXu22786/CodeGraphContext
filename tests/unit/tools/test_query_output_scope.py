"""H0 regressions for repository-scoped, bounded MCP query output."""
import asyncio
import json
import re
import threading
from contextlib import contextmanager
import pytest
from pathlib import Path, PureWindowsPath

from codegraphcontext.server import (
    MAX_JSONRPC_ID_BYTES,
    MAX_TOOL_RESPONSE_BYTES,
    MAX_TOOL_RESPONSE_LINES,
    MCPServer,
    _apply_final_output_limits,
    _apply_response_token_limit,
    _initial_active_repo_path,
    _pagination_retry_metadata,
)
from codegraphcontext.tool_definitions import TOOLS
from codegraphcontext.tools.code_finder import CodeFinder
from codegraphcontext.tools import report_generator
from codegraphcontext.tools.handlers.analysis_handlers import find_code
from codegraphcontext.tools.handlers import analysis_handlers
from codegraphcontext.tools.handlers.query_handlers import (
    _has_stable_unique_final_order_by,
    _scope_cypher_query,
    execute_cypher_query,
)
from codegraphcontext.tools.handlers import query_handlers


@pytest.fixture(autouse=True)
def _skip_index_readiness_for_h0_query_doubles(monkeypatch):
    """Keep H0 query-contract fakes independent of the job-state gate."""
    @contextmanager
    def _read_session(_self, driver, *args, **kwargs):
        with driver.session(*args, **kwargs) as session:
            yield session

    monkeypatch.setattr(CodeFinder, "_read_session", _read_session)


def _item(index=0, *, source=None, docstring=None, context=None, value=None):
    item = {
        "type": "function",
        "name": f"函数{index}",
        "path": f"/work/repo/src/{index}.py",
        "line_number": index + 1,
        "source": source if source is not None else f"def f{index}():\n    return {index}\n",
        "docstring": docstring if docstring is not None else f"说明 {index}",
        "is_dependency": False,
        "search_type": "function_name",
        "relevance_score": 0.9,
        "_sort_uid": f"uid-{index}",
        "_match_score": -1,
    }
    if context is not None:
        item["context"] = context
    if value is not None:
        item["value"] = value
    return item


class _FakeCodeFinder:
    def __init__(self, items):
        self.items = items
        self.last_kwargs = None

    def find_related_code(self, query, fuzzy_search, edit_distance, **kwargs):
        self.last_kwargs = kwargs
        items = self.items[: kwargs.get("candidate_limit", 20)]
        return {
            "query": query,
            "functions_by_name": list(items),
            "classes_by_name": list(items),
            "variables_by_name": list(items),
            "content_matches": list(items),
            "ranked_results": list(items),
            "total_matches": len(items),
        }


def test_query_tool_defaults_to_the_active_repository_and_preserves_explicit_scope(tmp_path):
    server = MCPServer.__new__(MCPServer)
    server.cwd = tmp_path
    server.disabled_tools = set()
    server._context_note_pending = False
    captured = []
    server.find_code_tool = lambda **args: captured.append(args) or {"success": True}
    server.execute_cypher_query_tool = lambda **args: captured.append(args) or {"success": True}
    server.find_datasource_nodes_tool = lambda **args: captured.append(args) or {"success": True}

    asyncio.run(server.handle_tool_call("find_code", {"query": "你好"}))
    asyncio.run(
        server.handle_tool_call(
            "find_code", {"query": "你好", "repo_path": "/data/selected-repo"}
        )
    )
    asyncio.run(
        server.handle_tool_call(
            "execute_cypher_query", {"cypher_query": "MATCH (n) RETURN n"}
        )
    )
    asyncio.run(
        server.handle_tool_call(
            "execute_cypher_query",
            {"cypher_query": "MATCH (n) RETURN n", "global_scope": True},
        )
    )
    asyncio.run(
        server.handle_tool_call(
            "execute_cypher_query",
            {"cypher_query": "MATCH (n) RETURN n", "global_scope": "false"},
        )
    )
    asyncio.run(server.handle_tool_call("find_datasource_nodes", {}))

    assert captured[0]["repo_path"] == str(tmp_path.resolve())
    assert captured[1]["repo_path"] == "/data/selected-repo"
    assert captured[2]["repo_path"] == str(tmp_path.resolve())
    assert "repo_path" not in captured[3]
    assert captured[4]["repo_path"] == str(tmp_path.resolve())
    assert captured[5]["repo_path"] == str(tmp_path.resolve())


@pytest.mark.parametrize(
    "tool_name",
    [
        "simulate_metrics",
        "simulate_architectural_change",
        "analyze_architectural_evolution",
    ],
)
def test_architecture_analysis_tools_use_active_repository_scope(tmp_path, tool_name):
    server = MCPServer.__new__(MCPServer)
    server.cwd = tmp_path / "startup-workspace"
    server.active_repo_path = tmp_path / "selected-repository"
    server.disabled_tools = set()
    server._context_note_pending = False
    captured = []
    setattr(
        server,
        f"{tool_name}_tool",
        lambda **args: captured.append(args) or {"status": "ok"},
    )

    asyncio.run(server.handle_tool_call(tool_name, {}))

    assert captured[0]["repo_path"] == str(server.active_repo_path)


def test_whitespace_explicit_repository_scope_uses_active_repository(tmp_path):
    server = MCPServer.__new__(MCPServer)
    server.cwd = tmp_path
    server.active_repo_path = tmp_path / "selected-repo"
    server.disabled_tools = set()
    server._context_note_pending = False
    captured = []
    server.find_code_tool = lambda **args: captured.append(args) or {"success": True}

    asyncio.run(
        server.handle_tool_call("find_code", {"query": "needle", "repo_path": "  \t"})
    )

    assert captured[0]["repo_path"] == str((tmp_path / "selected-repo").resolve())


def test_default_repository_scope_normalizes_windows_path_separators():
    server = MCPServer.__new__(MCPServer)
    server.cwd = PureWindowsPath(r"C:\Users\dev\repo")
    server.disabled_tools = set()
    server._context_note_pending = False
    captured = []
    server.find_code_tool = lambda **args: captured.append(args) or {"success": True}

    asyncio.run(server.handle_tool_call("find_code", {"query": "needle"}))

    assert captured[0]["repo_path"] == "C:/Users/dev/repo"


def test_default_repository_scope_tracks_context_switch(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch
    from codegraphcontext.cli.config_manager import ResolvedContext

    old_repo = tmp_path / "old-repo"
    new_repo = tmp_path / "new-repo"
    old_repo.mkdir()
    new_repo.mkdir()
    cgc_dir = new_repo / ".codegraphcontext"
    cgc_dir.mkdir()

    server = MCPServer.__new__(MCPServer)
    server._context_transition_lock = threading.RLock()
    server.cwd = old_repo
    server.resolved_context = ResolvedContext(
        mode="per-repo",
        context_name="",
        database="kuzudb",
        db_path=str(old_repo / ".codegraphcontext" / "db" / "kuzudb"),
        cgcignore_path=str(old_repo / ".codegraphcontext" / ".cgcignore"),
        is_local=True,
    )
    server.job_manager = SimpleNamespace(list_active_jobs=lambda: [])
    server.db_manager = MagicMock()
    server.code_watcher = MagicMock()
    server.code_watcher.observer.is_alive.return_value = False
    server.loop = None
    server.disabled_tools = set()
    server._context_note_pending = False
    captured = []
    server.find_code_tool = lambda **args: captured.append(args) or {"success": True}

    new_manager = MagicMock()
    with patch("codegraphcontext.utils.path_sandbox.is_path_allowed", return_value=True), \
         patch("codegraphcontext.server.get_repo_context_path", return_value=cgc_dir), \
         patch("codegraphcontext.server.get_database_manager", return_value=new_manager), \
         patch("codegraphcontext.server._teardown_db_manager"), \
         patch("codegraphcontext.server.GraphBuilder"), \
         patch("codegraphcontext.server.CodeFinder"), \
         patch("codegraphcontext.server.CodeWatcher"):
        switched = server.switch_context_tool(context_path=str(new_repo), save=False)

    assert switched["status"] == "ok", switched
    asyncio.run(server.handle_tool_call("find_code", {"query": "needle"}))
    assert captured[0]["repo_path"] == str(new_repo)


def test_switching_back_to_global_clears_selected_repository_scope(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch
    from codegraphcontext.cli.config_manager import ResolvedContext

    repo = tmp_path / "selected-repo"
    repo.mkdir()
    server = MCPServer.__new__(MCPServer)
    server._context_transition_lock = threading.RLock()
    server.cwd = repo
    server.active_repo_path = repo
    server.resolved_context = ResolvedContext(
        mode="per-repo",
        context_name="",
        database="kuzudb",
        db_path=str(repo / ".codegraphcontext" / "db" / "kuzudb"),
        cgcignore_path=str(repo / ".codegraphcontext" / ".cgcignore"),
        is_local=True,
    )
    server.job_manager = SimpleNamespace(list_active_jobs=lambda: [])
    server.db_manager = MagicMock()
    server.code_watcher = MagicMock()
    server.code_watcher.observer.is_alive.return_value = False
    server.loop = None
    server.disabled_tools = set()
    server.find_code_tool = MagicMock(return_value={"success": True})

    with patch("codegraphcontext.server.get_database_manager") as get_db, \
         patch("codegraphcontext.server._teardown_db_manager"), \
         patch("codegraphcontext.server.GraphBuilder"), \
         patch("codegraphcontext.server.CodeFinder"), \
         patch("codegraphcontext.server.CodeWatcher"), \
         patch("codegraphcontext.server._default_global_db_path", return_value=str(tmp_path / "global-db")), \
         patch("codegraphcontext.server.load_config", return_value={"DEFAULT_DATABASE": "kuzudb"}):
        get_db.return_value = MagicMock()
        switched = server.switch_context_tool(context_path="global")

    assert switched["status"] == "ok"
    assert server.resolved_context.mode == "global"
    assert server.active_repo_path is None
    result = asyncio.run(server.handle_tool_call("find_code", {"query": "needle"}))
    assert "error" in result
    assert "repo_path" in result["error"]
    server.find_code_tool.assert_not_called()


def test_multi_repository_workspace_requires_explicit_default_scope(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    workspace = tmp_path / "workspace"
    repo_a = workspace / "repo-a"
    repo_b = workspace / "repo-b"
    repo_a.mkdir(parents=True)
    repo_b.mkdir()
    context = SimpleNamespace(
        mode="global",
        context_name="",
        database="kuzudb",
        db_path=str(tmp_path / "global-db"),
        cgcignore_path="",
        is_local=False,
    )
    children = [
        SimpleNamespace(path=str(repo_a)),
        SimpleNamespace(path=str(repo_b)),
    ]

    with patch("codegraphcontext.server.resolve_context", return_value=context), \
         patch("codegraphcontext.server.discover_child_contexts", return_value=children), \
         patch("codegraphcontext.server.get_database_manager") as get_db, \
         patch("codegraphcontext.server.JobManager"), \
         patch("codegraphcontext.server.GraphBuilder"), \
         patch("codegraphcontext.server.CodeFinder"), \
         patch("codegraphcontext.server.CodeWatcher"):
        get_db.return_value = MagicMock()
        server = MCPServer(cwd=workspace)

    assert server.active_repo_path is None
    server.find_code_tool = MagicMock(return_value={"success": True})
    result = asyncio.run(server.handle_tool_call("find_code", {"query": "needle"}))
    assert "error" in result
    assert "repo_path" in result["error"]
    server.find_code_tool.assert_not_called()


def test_implicit_per_repo_context_does_not_scope_multi_repository_workspace(
    monkeypatch, tmp_path
):
    from codegraphcontext.cli import config_manager

    workspace = tmp_path / "workspace"
    (workspace / "projects" / "repo-a" / ".git").mkdir(parents=True)
    (workspace / "projects" / "repo-b" / ".git").mkdir(parents=True)
    local_context_dir = workspace / ".codegraphcontext"

    class _ContextConfig:
        mode = "per-repo"
        default_context = ""
        contexts = {}

    monkeypatch.setattr(
        config_manager,
        "load_context_config",
        lambda **_kwargs: _ContextConfig(),
    )
    monkeypatch.setattr(
        config_manager,
        "get_repo_context_path",
        lambda root: Path(root) / ".codegraphcontext",
    )
    monkeypatch.setattr(
        config_manager,
        "load_config",
        lambda **_kwargs: {"DEFAULT_DATABASE": "kuzudb"},
    )
    context = config_manager.resolve_context(
        cwd=workspace,
        create_local_context=False,
        read_only=True,
    )

    assert context.is_local
    assert not local_context_dir.exists()
    assert _initial_active_repo_path(workspace, context) is None


def test_nested_git_repositories_do_not_use_workspace_root_as_default_scope(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    workspace = tmp_path / "workspace"
    repo_a = workspace / "projects" / "team-a" / "repo-a"
    repo_b = workspace / "projects" / "team-b" / "repo-b"
    (repo_a / ".git").mkdir(parents=True)
    (repo_b / ".git").mkdir(parents=True)
    context = SimpleNamespace(
        mode="global",
        context_name="",
        database="kuzudb",
        db_path=str(tmp_path / "global-db"),
        cgcignore_path="",
        is_local=False,
    )

    with patch("codegraphcontext.server.resolve_context", return_value=context), \
         patch("codegraphcontext.server.discover_child_contexts", return_value=[]), \
         patch("codegraphcontext.server.get_database_manager") as get_db, \
         patch("codegraphcontext.server.JobManager"), \
         patch("codegraphcontext.server.GraphBuilder"), \
         patch("codegraphcontext.server.CodeFinder"), \
         patch("codegraphcontext.server.CodeWatcher"):
        get_db.return_value = MagicMock()
        server = MCPServer(cwd=workspace)

    assert server.active_repo_path is None


def test_outer_git_root_with_deep_nested_repository_is_ambiguous(tmp_path):
    from types import SimpleNamespace

    workspace = tmp_path / "outer-repo"
    (workspace / ".git").mkdir(parents=True)
    nested = workspace / "vendor" / "group" / "nested-repo"
    (nested / ".git").mkdir(parents=True)
    context = SimpleNamespace(is_local=False, cgcignore_path="")

    assert _initial_active_repo_path(workspace, context) is None


def test_global_server_from_repository_subdirectory_uses_git_ancestor(tmp_path):
    from types import SimpleNamespace

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    subdirectory = repo / "src" / "package"
    subdirectory.mkdir(parents=True)
    context = SimpleNamespace(is_local=False, cgcignore_path="")

    assert _initial_active_repo_path(subdirectory, context) == repo.resolve()


def test_named_server_scope_uses_registered_repository_outside_that_repository(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    unrelated_repo = tmp_path / "unrelated"
    (unrelated_repo / ".git").mkdir(parents=True)
    selected_repo = tmp_path / "selected" / "service"
    selected_repo.mkdir(parents=True)
    config = SimpleNamespace(
        contexts={"selected": SimpleNamespace(repos=[str(selected_repo)])}
    )
    monkeypatch.setattr(
        "codegraphcontext.server.load_context_config",
        lambda **_kwargs: config,
    )
    context = SimpleNamespace(
        mode="named", context_name="selected", is_local=False, cgcignore_path=""
    )

    assert _initial_active_repo_path(unrelated_repo, context) == selected_repo.resolve()


def test_mcp_server_startup_uses_registered_repository_from_named_context(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    unrelated_repo = tmp_path / "unrelated"
    (unrelated_repo / ".git").mkdir(parents=True)
    selected_repo = tmp_path / "selected" / "service"
    selected_repo.mkdir(parents=True)
    context = SimpleNamespace(
        mode="named",
        context_name="selected",
        database="kuzudb",
        db_path=str(tmp_path / "selected-db"),
        cgcignore_path="",
        is_local=False,
    )
    config = SimpleNamespace(
        contexts={"selected": SimpleNamespace(repos=[str(selected_repo)])}
    )
    monkeypatch.setattr("codegraphcontext.server.resolve_context", lambda **_kwargs: context)
    monkeypatch.setattr(
        "codegraphcontext.server.load_context_config", lambda **_kwargs: config
    )
    monkeypatch.setattr(
        "codegraphcontext.server.discover_child_contexts", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr("codegraphcontext.server.get_database_manager", lambda **_kwargs: MagicMock())
    monkeypatch.setattr("codegraphcontext.server.JobManager", lambda: MagicMock())
    monkeypatch.setattr(
        "codegraphcontext.server.GraphBuilder", lambda *_args, **_kwargs: MagicMock()
    )
    monkeypatch.setattr(
        "codegraphcontext.server.CodeFinder", lambda *_args, **_kwargs: MagicMock()
    )
    monkeypatch.setattr(
        "codegraphcontext.server.CodeWatcher", lambda *_args, **_kwargs: MagicMock()
    )

    server = MCPServer(cwd=unrelated_repo)

    assert server.active_repo_path == selected_repo.resolve()


def test_named_server_scope_fails_closed_for_ambiguous_registered_repositories(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    repo_a = tmp_path / "workspace" / "a"
    repo_b = tmp_path / "workspace" / "b"
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)
    config = SimpleNamespace(
        contexts={"selected": SimpleNamespace(repos=[str(repo_a), str(repo_b)])}
    )
    monkeypatch.setattr(
        "codegraphcontext.server.load_context_config",
        lambda **_kwargs: config,
    )
    context = SimpleNamespace(
        mode="named", context_name="selected", is_local=False, cgcignore_path=""
    )

    assert _initial_active_repo_path(tmp_path, context) is None


def test_code_finder_normalizes_windows_repository_filters_to_indexed_path_form():
    finder = CodeFinder(_CapturingSearchDB())

    assert finder._normalize_repo_path_prefix(r"C:\work\repo") == "C:/work/repo/"


def test_find_code_returns_concise_evidence_by_default_and_source_pages_on_request():
    source = 'def greet():\n    return "你好\\\"世界"\n' + "    pass\n" * 4
    docstring = "\n".join(f'第{i}行"路径\\' for i in range(100))
    finder = _FakeCodeFinder(
        [_item(source=source, docstring=docstring, context="上下文" * 500, value="值" * 500)]
    )

    concise = find_code(finder, query="你好", repo_path="/work/repo")
    concise_item = concise["results"]["ranked_results"][0]
    assert "source" not in concise_item
    assert "docstring" not in concise_item
    assert "context" not in concise_item
    assert "value" not in concise_item
    assert "_sort_uid" not in concise_item


    assert "_match_score" not in concise_item
    assert concise_item["source_excerpt"]
    assert len(concise_item["source_excerpt"]) <= 240
    assert len(concise_item["context_excerpt"]) <= 240
    assert len(concise_item["value_excerpt"]) <= 240
    assert concise_item["source_available"] is True

    large_finder = _FakeCodeFinder(
        [
            _item(i, source="源代码\\\" " + "x" * 5000, docstring="长文档\n" * 500)
            for i in range(20)
        ]
    )
    large_result = find_code(large_finder, query="源代码", repo_path="/work/repo")
    large_text = json.dumps(large_result, ensure_ascii=False, indent=2)
    assert len(large_text.splitlines()) <= MAX_TOOL_RESPONSE_LINES
    assert len(large_text.encode("utf-8")) < MAX_TOOL_RESPONSE_BYTES
    assert len(large_result["results"]["ranked_results"]) == 3
    assert len(large_result["results"]["functions_by_name"]) == 3
    assert large_result["results"]["category_next_offsets"]["functions_by_name"] == 3

    first_page = find_code(
        finder,
        query="你好",
        repo_path="/work/repo",
        include_source=True,
        include_docstring=True,
        source_offset=0,
        docstring_offset=0,
        source_char_limit=40,
        docstring_char_limit=24,
        source_line_limit=3,
    )["results"]["ranked_results"][0]
    assert first_page["source"] == source[:40]
    assert first_page["docstring"] == docstring[:24]
    assert first_page["source_next_offset"] == 40
    assert first_page["docstring_next_offset"] == 24
    assert len(first_page["source"].splitlines()) <= 3

    source_pages = [first_page["source"]]
    docstring_pages = [first_page["docstring"]]
    source_offset = first_page["source_next_offset"]
    docstring_offset = first_page["docstring_next_offset"]
    while source_offset is not None or docstring_offset is not None:
        page = find_code(
            finder,
            query="你好",
            repo_path="/work/repo",
            include_source=True,
            include_docstring=True,
            source_offset=source_offset if source_offset is not None else len(source),
            docstring_offset=docstring_offset if docstring_offset is not None else len(docstring),
            source_char_limit=40,
            docstring_char_limit=24,
            source_line_limit=3,
        )["results"]["ranked_results"][0]
        assert len(page["source"].splitlines()) <= 3
        assert len(page["docstring"].splitlines()) <= 3
        source_pages.append(page["source"])
        docstring_pages.append(page["docstring"])
        source_offset = page["source_next_offset"]
        docstring_offset = page["docstring_next_offset"]

    assert "".join(source_pages) == source
    assert "".join(docstring_pages) == docstring
    round_trip = json.loads(json.dumps(first_page, ensure_ascii=False))
    assert round_trip["source"] == first_page["source"]
    assert "你好" in round_trip["source"]


def test_java_report_sections_apply_the_selected_repository_scope():
    from codegraphcontext.tools.report_generator import generate_report

    calls = []

    class _Session:
        def run(self, query, **params):
            calls.append((query, params))
            return []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_driver(self):
            return _Driver()

    generate_report(_Manager(), include_java=True, repo_path="/work/repo")

    java_calls = [
        (query, params)
        for query, params in calls
        if "http_method" in query
        or "spring_stereotype" in query
        or "MavenModule" in query
    ]
    assert len(java_calls) == 3
    assert all(params.get("repo_path") == "/work/repo/" for _, params in java_calls)
    assert "fn.path STARTS WITH $repo_path" in java_calls[0][0]
    assert "c.path STARTS WITH $repo_path" in java_calls[1][0]
    assert "m.path STARTS WITH $repo_path" in java_calls[2][0]
    assert "dep.path STARTS WITH $repo_path" in java_calls[2][0]


def test_spring_endpoint_report_bounds_rows_and_signals_truncation():
    from codegraphcontext.tools.report_generator import _section_spring_endpoints

    calls = []
    all_rows = [
        {
            "method": "GET",
            "path": f"/api/{index}",
            "handler": f"handler_{index}",
            "file": f"/work/repo/src/{index}.java",
        }
        for index in range(101)
    ]

    class _Session:
        def run(self, query, **params):
            calls.append((query, params))
            return all_rows[:params["limit"]]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    section = _section_spring_endpoints(_Driver(), repo_path="/work/repo")

    query, params = calls[0]
    assert "LIMIT $limit" in query
    assert "ORDER BY" in query and "fn.uid" in query
    assert params["limit"] == 101
    assert "truncated" in section.lower()
    assert "handler_99" in section
    assert "handler_100" not in section


def test_report_relationship_queries_scope_callers_to_the_selected_repository():
    from codegraphcontext.tools.report_generator import (
        _section_dead_code,
        _section_god_nodes,
    )

    calls = []

    class _Session:
        def run(self, query, **params):
            calls.append((query, params))
            return []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    _section_god_nodes(_Driver(), repo_path="/work/repo")
    _section_dead_code(_Driver(), repo_path="/work/repo")

    god_nodes_query, god_nodes_params = next(
        (query, params) for query, params in calls if "in_degree" in query
    )
    dead_code_query, dead_code_params = next(
        (query, params)
        for query, params in calls
        if "MATCH (fn:Function)" in query
    )
    assert "caller.path STARTS WITH $repo_path" in god_nodes_query
    assert "caller.path STARTS WITH $repo_path" in dead_code_query
    assert god_nodes_params["repo_path"] == "/work/repo/"
    assert dead_code_params["repo_path"] == "/work/repo/"


def test_report_sections_normalize_trailing_repository_separator():
    from codegraphcontext.tools.report_generator import (
        _section_complexity,
        _section_cross_module_calls,
        _section_dead_code,
        _section_god_nodes,
    )

    calls = []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            return []

    class _Driver:
        def session(self):
            return _Session()

    _section_god_nodes(_Driver(), repo_path="/work/repo/")
    _section_complexity(_Driver(), repo_path="/work/repo/")
    _section_cross_module_calls(_Driver(), repo_path="/work/repo/")
    _section_dead_code(_Driver(), repo_path="/work/repo/")

    assert len(calls) == 4
    assert all(params["repo_path"] == "/work/repo/" for _, params in calls)


def test_generate_report_uses_active_and_explicit_repository_scopes(tmp_path):
    from unittest.mock import patch

    server = MCPServer.__new__(MCPServer)
    server.cwd = tmp_path
    server.active_repo_path = tmp_path / "selected-repo"
    server.db_manager = object()
    server.disabled_tools = set()
    server._context_note_pending = False

    with patch(
        "codegraphcontext.tools.report_generator.generate_report",
        return_value="# report",
    ) as generate_report, patch(
        "codegraphcontext.utils.path_sandbox.is_path_allowed", return_value=True
    ):
        default_result = asyncio.run(
            server.handle_tool_call(
                "generate_report", {"output_path": str(tmp_path / "default.md")}
            )
        )
        explicit_result = asyncio.run(
            server.handle_tool_call(
                "generate_report",
                {
                    "output_path": str(tmp_path / "explicit.md"),
                    "repo_path": "/data/explicit-repo",
                },
            )
        )

    assert default_result["status"] == "ok"
    assert explicit_result["status"] == "ok"
    assert generate_report.call_args_list[0].kwargs["repo_path"] == str(
        (tmp_path / "selected-repo").resolve()
    )
    assert (
        generate_report.call_args_list[1].kwargs["repo_path"]
        == "/data/explicit-repo"
    )
    assert "repo_path" in TOOLS["generate_report"]["inputSchema"]["properties"]


def test_find_dead_code_pages_results_and_returns_a_continuation(monkeypatch):
    rows = [
        {
            "function_name": f"unused_{index}",
            "path": f"/work/repo/{index}.py",
            "line_number": index + 1,
            "docstring": None,
            "context": None,
            "decorators": [],
            "modifiers": [],
            "file_name": f"{index}.py",
            "_sort_uid": f"unused-{index}",
        }
        for index in range(5)
    ]
    queries = []

    class _Rows:
        def __init__(self, query, params):
            self.query = query
            self.params = params

        def data(self):
            queries.append((self.query, dict(self.params)))
            if "AS total_count" in self.query:
                return [{
                    "total_count": 5,
                    "high_count": 5,
                    "medium_count": 0,
                    "low_count": 0,
                }]
            assert "SKIP $offset LIMIT $limit" in self.query
            start = self.params["offset"]
            return list(rows[start:start + self.params["limit"]])

    class _Session:
        def run(self, query, **params):
            return _Rows(query, params)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_driver(self, graph_name=None):
            return _Driver()

    monkeypatch.setattr(
        analysis_handlers, "get_tool_result_limit", lambda *_args, **_kwargs: 50
    )
    response = analysis_handlers.find_dead_code(
        CodeFinder(_Manager()), repo_path="/work/repo", offset=2, page_size=2
    )

    assert response["success"] is True
    assert [
        item["function_name"]
        for item in response["results"]["potentially_unused_functions"]
    ] == ["unused_2", "unused_3"]
    assert response["results"]["total_count"] == 5
    assert response["offset"] == 2
    assert response["page_size"] == 2
    assert response["truncated"] is True
    assert response["next_offset"] == 4
    assert response["continuation_limit_reached"] is False
    assert response["results"]["offset"] == 2
    assert response["results"]["page_size"] == 2
    assert response["results"]["next_offset"] == 4
    page_query = next(query for query, _ in queries if "ORDER BY func.path" in query)
    assert "ORDER BY func.path, func.line_number, func.uid" in page_query
    assert any("AS total_count" in query for query, _ in queries)
    properties = TOOLS["find_dead_code"]["inputSchema"]["properties"]
    assert "offset" in properties and "page_size" in properties


@pytest.mark.parametrize("include_low_confidence", [False, True])
def test_find_dead_code_uses_database_pages_and_separate_counts(
    include_low_confidence,
):
    all_rows = [
        {
            "function_name": f"unused_{index}",
            "path": f"/work/repo/{index:03}.py",
            "line_number": index + 1,
            "decorators": [],
            "modifiers": [],
            "file_name": f"{index:03}.py",
            "_sort_uid": f"unused-{index}",
        }
        for index in range(110 if include_low_confidence else 100)
    ]
    queries = []
    fetched_rows = []

    class _Rows:
        def __init__(self, query, params):
            self.query = query
            self.params = params

        def data(self):
            queries.append((self.query, dict(self.params)))
            if "AS total_count" in self.query:
                return [{
                    "total_count": len(all_rows),
                    "high_count": 80,
                    "medium_count": 20,
                    "low_count": 10 if include_low_confidence else 0,
                }]

            assert "SKIP $offset LIMIT $limit" in self.query
            assert self.params["offset"] == 10
            assert self.params["limit"] == 3
            page = all_rows[10:13]
            fetched_rows.extend(page)
            return [dict(row) for row in page]

    class _Session:
        def run(self, query, **params):
            return _Rows(query, params)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_driver(self, graph_name=None):
            return _Driver()

    response = CodeFinder(_Manager()).find_dead_code(
        repo_path="/work/repo",
        limit=3,
        offset=10,
        include_low_confidence=include_low_confidence,
    )

    assert len(queries) == 2
    assert any("AS total_count" in query for query, _ in queries)
    assert len(fetched_rows) == 3
    assert response["total_count"] == len(all_rows)
    assert response["confidence_counts"] == {
        "high": 80,
        "medium": 20,
        "low": 10 if include_low_confidence else 0,
    }
    assert response["offset"] == 10
    assert response["page_size"] == 3
    assert response["truncated"] is True
    assert response["next_offset"] == 13
    assert response["continuation_limit_reached"] is False
    assert [row["function_name"] for row in response["potentially_unused_functions"]] == [
        "unused_10",
        "unused_11",
        "unused_12",
    ]


def test_dead_code_pages_omit_unbounded_text_evidence_by_default(monkeypatch):
    queries = []
    candidate = {
        "function_name": "orphan",
        "path": "/work/repo/orphan.py",
        "line_number": 4,
        "decorators": [],
        "modifiers": [],
        "file_name": "orphan.py",
        "_sort_uid": "orphan-uid",
    }
    class _Rows:
        def __init__(self, query, params, rows):
            self.query = query
            self.params = params
            self.rows = rows

        def data(self):
            queries.append(self.query)
            if "AS total_count" in self.query:
                return [{
                    "total_count": 1,
                    "high_count": 1,
                    "medium_count": 0,
                    "low_count": 0,
                }]
            return [dict(row) for row in self.rows]

    class _Session:
        def run(self, query, **params):
            row = dict(candidate)
            if "func.docstring as docstring" in query.lower():
                row["docstring"] = "d" * 180_000
            if "func.context as context" in query.lower():
                row["context"] = "c" * 180_000
            return _Rows(query, params, [row])

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self, **_kwargs):
            return _Session()

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, *_args):
            return _Driver()

    response = analysis_handlers.find_dead_code(
        CodeFinder(_Manager()), repo_path="/work/repo", offset=0, page_size=1
    )
    item = response["results"]["potentially_unused_functions"][0]

    assert "docstring" not in item
    assert "context" not in item
    assert all("func.docstring" not in query for query in queries)
    assert all("func.context" not in query for query in queries)
    assert len(queries) == 2
    page_query = next(query for query in queries if "ORDER BY func.path" in query)
    assert "func.decorators as decorators" not in page_query.lower()
    assert "func.modifiers as modifiers" not in page_query.lower()
    assert "func.decorators[0..16]" in page_query
    assert "func.modifiers[0..16]" in page_query
    assert "as decorators_truncated" in page_query.lower()
    assert "as modifiers_truncated" in page_query.lower()

    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(response, ensure_ascii=False)}]},
    }
    assert len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")) <= MAX_TOOL_RESPONSE_BYTES


@pytest.mark.parametrize("query_type", ["find_all_callers", "find_all_callees"])
def test_transitive_relationship_analyzer_preserves_terminal_continuation_limit(
    query_type,
):
    rows = [
        {
            "caller_name": f"caller_{index}",
            "caller_path": f"/work/repo/{index}.py",
            "callee_name": "target",
            "callee_path": "/work/repo/target.py",
            "line": index,
            "_caller_uid": f"caller-{index}",
            "_callee_uid": "target",
        }
        for index in range(2)
    ]

    class _Rows:
        def data(self):
            return list(rows)

    class _Session:
        def run(self, _query, **_params):
            return _Rows()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_driver(self, graph_name=None):
            return _Driver()

    response = CodeFinder(_Manager()).analyze_code_relationships(
        query_type,
        "target",
        repo_path="/work/repo",
        offset=99_999,
        page_size=1,
    )

    assert response["truncated"] is True
    assert response["next_offset"] is None
    assert response["continuation_limit_reached"] is True


def test_find_code_source_and_docstring_pages_bound_lone_carriage_return_lines():
    source = "one\rtwo\rthree\rfour"
    docstring = "甲\r乙\r丙"
    finder = _FakeCodeFinder([_item(source=source, docstring=docstring)])

    item = find_code(
        finder,
        query="one",
        include_source=True,
        include_docstring=True,
        source_char_limit=100,
        docstring_char_limit=100,
        source_line_limit=2,
    )["results"]["ranked_results"][0]

    assert item["source"] == "one\rtwo\r"
    assert item["docstring"] == "甲\r乙\r"
    assert len(item["source"].splitlines()) <= 2
    assert len(item["docstring"].splitlines()) <= 2
    assert item["source_next_offset"] == len(item["source"])
    assert item["docstring_next_offset"] == len(item["docstring"])


def test_unicode_source_pages_fit_the_final_wrapped_byte_limit():
    finder = _FakeCodeFinder([_item(source="😀" * 5000, docstring="😀" * 5000)])
    result = find_code(
        finder,
        query="😀",
        page_size=1,
        include_source=True,
        include_docstring=True,
        source_char_limit=4096,
        docstring_char_limit=4096,
    )
    response_text = json.dumps(result, indent=2)
    wrapped_bytes = lambda text: json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}
    ).encode("utf-8")

    assert result["results"]["ranked_results"][0]["source"] == "😀" * 1536
    assert _apply_final_output_limits("find_code", response_text, wrapped_bytes) == response_text
    assert len(wrapped_bytes(response_text)) <= MAX_TOOL_RESPONSE_BYTES


def test_find_code_result_pages_expose_a_continuation_offset():
    finder = _FakeCodeFinder([_item(i) for i in range(30)])

    first = find_code(finder, query="f", page_size=1, offset=0)
    second = find_code(finder, query="f", page_size=1, offset=first["next_offset"])

    assert [row["name"] for row in first["results"]["ranked_results"]] == ["函数0"]
    assert first["truncated"] is True
    assert first["has_more"] is True
    assert first["next_offset"] == 1
    assert [row["name"] for row in second["results"]["ranked_results"]] == ["函数1"]
    assert second["offset"] == 1
    assert second["total_matches_exact"] is False
    assert finder.last_kwargs["candidate_limit"] == 3

    last = find_code(finder, query="f", page_size=1, offset=29)
    assert [row["name"] for row in last["results"]["ranked_results"]] == ["函数29"]
    assert last["total_matches_exact"] is True
    assert last["has_more"] is False


def test_find_code_does_not_advertise_an_unaccepted_boundary_offset(monkeypatch):
    monkeypatch.setattr(analysis_handlers, "_FIND_CODE_MAX_OFFSET", 4)
    finder = _FakeCodeFinder([_item(i) for i in range(5)])

    result = find_code(finder, query="f", page_size=5, offset=3)

    assert result["page_size"] == 1
    assert result["has_more"] is True
    assert result["continuation_limit_reached"] is True
    assert "next_offset" not in result
    assert result["results"]["category_next_offsets"]["functions_by_name"] is None


class _Record:
    def __init__(self, data):
        self._data = data

    def data(self):
        return self._data


class _FakeSession:
    def __init__(self, manager):
        self.manager = manager

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **params):
        self.manager.last_query = query
        self.manager.last_params = params
        return iter(_Record({"id": i}) for i in range(self.manager.row_count))


class _FakeDriver:
    def __init__(self, manager):
        self.manager = manager

    def session(self, **kwargs):
        self.manager.session_kwargs = kwargs
        return _FakeSession(self.manager)


class _FakeDBManager:
    def __init__(self, row_count=6):
        self.last_query = None
        self.last_params = None
        self.session_kwargs = None
        self.row_count = row_count

    def get_backend_type(self):
        return "kuzu"

    def get_driver(self, graph_name=None):
        return _FakeDriver(self)


class _SearchResult:
    def data(self):
        return []


class _CapturingSearchSession:
    def __init__(self, manager):
        self.manager = manager

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **params):
        self.manager.calls.append((query, params))
        return _SearchResult()


class _CapturingSearchDriver:
    def __init__(self, manager):
        self.manager = manager

    def session(self):
        return _CapturingSearchSession(self.manager)


class _CapturingSearchDB:
    def __init__(self):
        self.calls = []

    def get_backend_type(self):
        return "neo4j"

    def get_driver(self, graph_name=None):
        return _CapturingSearchDriver(self)


def test_find_code_passes_page_window_to_each_underlying_search_query():
    db = _CapturingSearchDB()
    code_finder = CodeFinder(db)

    code_finder.find_related_code(
        "widget",
        fuzzy_search=False,
        edit_distance=2,
        repo_path="/work/repo",
        candidate_limit=42,
    )

    assert len(db.calls) == 4
    assert all("LIMIT $limit" in query for query, _ in db.calls)
    assert all("ORDER BY" in query for query, _ in db.calls)
    assert all("as _sort_uid" in query for query, _ in db.calls)
    assert all(".uid" in query.split("ORDER BY", 1)[1] for query, _ in db.calls)
    assert all(params["limit"] == 42 for _, params in db.calls)


def test_find_code_final_ranking_uses_uid_for_equal_visible_keys():
    rows = [
        {
            "name": "widget",
            "path": "/work/repo/widget.py",
            "line_number": 8,
            "is_dependency": False,
            "_sort_uid": uid,
        }
        for uid in ("z-uid", "a-uid")
    ]

    class _Rows:
        def __init__(self, data):
            self._data = data

        def data(self):
            return list(self._data)

    class _TieSession(_CapturingSearchSession):
        def run(self, query, **params):
            self.manager.calls.append((query, params))
            if "MATCH (node:Function" in query:
                return _Rows(rows)
            return _Rows([])

    class _TieDriver(_CapturingSearchDriver):
        def session(self):
            return _TieSession(self.manager)

    class _TieDB(_CapturingSearchDB):
        def get_driver(self, graph_name=None):
            return _TieDriver(self)

    result = CodeFinder(_TieDB()).find_related_code(
        "widget",
        fuzzy_search=False,
        edit_distance=2,
        repo_path="/work/repo",
        candidate_limit=10,
    )

    assert [item["_sort_uid"] for item in result["ranked_results"]] == [
        "a-uid",
        "z-uid",
    ]


@pytest.mark.parametrize("kind", ["function", "line", "variable", "content"])
def test_find_code_candidate_windows_are_prefix_stable(kind):
    candidates = {
        "function": [
            {
                "name": "widget",
                "path": "/work/repo/a.py",
                "line_number": 1,
                "is_dependency": True,
                "_sort_uid": "function-dependency",
            },
            {
                "name": "widget",
                "path": "/work/repo/z.py",
                "line_number": 1,
                "is_dependency": False,
                "_sort_uid": "function-project",
            },
        ],
        "line": [
            {
                "name": "widget",
                "path": "/work/repo/a.py",
                "line_number": 2,
                "is_dependency": False,
                "_sort_uid": "line-two",
            },
            {
                "name": "widget",
                "path": "/work/repo/a.py",
                "line_number": 10,
                "is_dependency": False,
                "_sort_uid": "line-ten",
            },
        ],
        "variable": [
            {
                "name": "widget_a",
                "path": "/work/repo/z.py",
                "line_number": 1,
                "is_dependency": False,
                "_sort_uid": "variable-z-path",
            },
            {
                "name": "widget_z",
                "path": "/work/repo/a.py",
                "line_number": 1,
                "is_dependency": False,
                "_sort_uid": "variable-a-path",
            },
        ],
        "content": [
            {
                "type": "function",
                "name": "widget_z",
                "path": "/work/repo/z.py",
                "line_number": 1,
                "is_dependency": False,
                "_sort_uid": "content-high-score",
            },
            {
                "type": "function",
                "name": "widget_a",
                "path": "/work/repo/a.py",
                "line_number": 1,
                "is_dependency": False,
                "_sort_uid": "content-low-score",
            },
        ],
    }[kind]
    score_by_uid = {
        "content-high-score": 20,
        "content-low-score": 1,
    }

    class _Rows:
        def __init__(self, rows):
            self.rows = rows

        def data(self):
            return [dict(row) for row in self.rows]

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            rows = []
            if kind in {"function", "line"} and "MATCH (node:Function {name:" in query:
                rows = list(candidates)
                if "node.is_dependency ASC, node.path" in query:
                    rows.sort(
                        key=lambda row: (
                            row["is_dependency"], row["path"], row["line_number"],
                            row["name"], row["_sort_uid"],
                        )
                    )
                else:
                    rows.sort(
                        key=lambda row: (
                            row["path"], row["line_number"], row["name"], row["_sort_uid"]
                        )
                    )
            elif kind == "variable" and "MATCH (v:Variable)" in query:
                rows = list(candidates)
                if "v.is_dependency ASC, v.path" in query:
                    rows.sort(
                        key=lambda row: (
                            row["is_dependency"], row["path"], row["line_number"],
                            row["name"], row["_sort_uid"],
                        )
                    )
                else:
                    rows.sort(
                        key=lambda row: (
                            row["is_dependency"], row["name"], row["path"],
                            row["line_number"], row["_sort_uid"],
                        )
                    )
            elif kind == "content" and '"code_search_index"' in query:
                rows = list(candidates)
                if "score as _match_score" in query:
                    for row in rows:
                        row["_match_score"] = score_by_uid[row["_sort_uid"]]
                    rows.sort(
                        key=lambda row: (
                            row["is_dependency"], -row["_match_score"], row["path"],
                            row["line_number"], row["type"], row["name"], row["_sort_uid"],
                        )
                    )
                else:
                    rows.sort(
                        key=lambda row: (
                            row["is_dependency"], row["path"], row["line_number"],
                            row["type"], row["name"], row["_sort_uid"],
                        )
                    )
            return _Rows(rows[: params.get("limit", 20)])

    class _Driver:
        def session(self):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, graph_name=None):
            return _Driver()

    first_page = CodeFinder(_DB()).find_related_code(
        "widget", False, 2, repo_path="/work/repo", candidate_limit=1
    )["ranked_results"]
    expanded_window = CodeFinder(_DB()).find_related_code(
        "widget", False, 2, repo_path="/work/repo", candidate_limit=2
    )["ranked_results"]

    assert first_page[0]["_sort_uid"] == expanded_window[0]["_sort_uid"]
    if kind == "content":
        assert first_page[0]["_sort_uid"] == "content-high-score"


def test_portable_fuzzy_quality_survives_candidate_windows_and_merged_ranking():
    rows = [
        {
            "name": "widget",
            "path": "/work/repo/z.py",
            "line_number": 1,
            "is_dependency": False,
            "_sort_uid": "exact-name",
        },
        {
            "name": "widgex",
            "path": "/work/repo/a.py",
            "line_number": 1,
            "is_dependency": False,
            "_sort_uid": "near-name",
        },
    ]

    class _Rows:
        def __init__(self, rows):
            self.rows = rows

        def data(self):
            return [dict(row) for row in self.rows]

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            if "MATCH (node:Function)" in query and "node.source IS NOT NULL" not in query:
                return _Rows(rows)
            return _Rows([])

    class _Driver:
        def session(self):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "kuzu"

        def get_driver(self, graph_name=None):
            return _Driver()

    finder = CodeFinder(_DB())
    first_page = finder.find_related_code(
        "widget", True, 2, repo_path="/work/repo", candidate_limit=1
    )["ranked_results"]
    expanded_window = finder.find_related_code(
        "widget", True, 2, repo_path="/work/repo", candidate_limit=2
    )["ranked_results"]

    assert first_page[0]["_sort_uid"] == expanded_window[0]["_sort_uid"]
    assert first_page[0]["_sort_uid"] == "exact-name"


def test_native_fuzzy_score_survives_candidate_windows_and_merged_ranking():
    rows = [
        {
            "name": "widget",
            "path": "/work/repo/z.py",
            "line_number": 1,
            "is_dependency": False,
            "_sort_uid": "high-score",
            "_match_score": 10.0,
        },
        {
            "name": "widgit",
            "path": "/work/repo/a.py",
            "line_number": 1,
            "is_dependency": False,
            "_sort_uid": "low-score",
            "_match_score": 0.1,
        },
    ]

    class _Rows:
        def __init__(self, data):
            self.data_rows = data

        def data(self):
            return [dict(row) for row in self.data_rows]

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            if "CALL db.index.fulltext.queryNodes" not in query:
                return _Rows([])
            if "MATCH (node)<-[:CONTAINS]-(f:File)" in query or "WHERE node:Class" in query:
                return _Rows([])
            result_rows = list(rows)
            if "score as _match_score" in query:
                result_rows.sort(
                    key=lambda row: (
                        row["is_dependency"], -row["_match_score"], row["path"],
                        row["line_number"], row["name"], row["_sort_uid"],
                    )
                )
            else:
                result_rows.sort(key=lambda row: (row["path"], row["line_number"], row["_sort_uid"]))
            return _Rows(result_rows[: params.get("limit", 20)])

    class _Driver:
        def session(self):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, graph_name=None):
            return _Driver()

    finder = CodeFinder(_DB())
    first_page = finder.find_related_code(
        "widget", True, 2, repo_path="/work/repo", candidate_limit=1
    )["ranked_results"]
    expanded_window = finder.find_related_code(
        "widget", True, 2, repo_path="/work/repo", candidate_limit=2
    )["ranked_results"]

    assert first_page[0]["_sort_uid"] == expanded_window[0]["_sort_uid"]
    assert first_page[0]["_sort_uid"] == "high-score"


def test_portable_fuzzy_repo_scan_is_bounded_and_reports_terminal_truncation(monkeypatch):
    from codegraphcontext.tools import code_finder as code_finder_module

    monkeypatch.setattr(code_finder_module, "_PORTABLE_FUZZY_SCAN_LIMIT", 3)
    rows = [
        {
            "name": f"target{i}",
            "path": f"/work/repo/{i}.py",
            "line_number": 1,
            "is_dependency": False,
            "_sort_uid": f"uid-{i}",
            "source": "source",
            "docstring": "docstring",
        }
        for i in range(3)
    ] + [
        {
            "name": "target",
            "path": "/work/repo/z.py",
            "line_number": 1,
            "is_dependency": False,
            "_sort_uid": "exact-target",
            "source": "source",
            "docstring": "docstring",
        }
    ]
    calls = []

    class _Rows:
        def __init__(self, data):
            self.data_rows = data

        def data(self):
            return [dict(row) for row in self.data_rows]

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            if "MATCH (node:Function)" in query and "node.source IS NOT NULL" not in query:
                return _Rows(rows)
            return _Rows([])

    class _Driver:
        def session(self):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "kuzu"

        def get_driver(self, graph_name=None):
            return _Driver()

    result = find_code(
        CodeFinder(_DB()),
        query="target",
        fuzzy_search=True,
        edit_distance=0,
        repo_path="/work/repo",
        page_size=1,
    )

    function_scan = next(call for call in calls if "MATCH (node:Function)" in call[0])
    assert "LIMIT $scan_limit" in function_scan[0]
    assert function_scan[1]["scan_limit"] == 4
    assert result["results"]["ranked_results"] == []
    assert result["total_matches_exact"] is False
    assert result["continuation_limit_reached"] is True
    assert "next_offset" not in result


def test_portable_fuzzy_scan_hydrates_only_the_requested_result_page():
    source = "def target():\n" + "    return 1\n" * 80
    docstring = "目标函数说明\n" * 80
    candidates = [
        {
            "name": "target",
            "path": f"/work/repo/{index}.py",
            "line_number": index + 1,
            "is_dependency": False,
            "_sort_uid": f"uid-{index}",
        }
        for index in range(4)
    ]
    calls = []

    class _Rows:
        def __init__(self, rows):
            self.rows = rows

        def data(self):
            return [dict(row) for row in self.rows]

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            if "WHERE node.uid IN $uids" in query:
                return _Rows(
                    [
                        {
                            "_sort_uid": uid,
                            "_source_preview": source[:params["preview_limit"]],
                            "_source_total_chars": len(source),
                            "_docstring_preview": docstring[:params["preview_limit"]],
                            "_docstring_total_chars": len(docstring),
                            "_source_page": source[params["source_offset"]:params["source_offset"] + params["source_char_limit"]],
                            "_docstring_page": docstring[params["docstring_offset"]:params["docstring_offset"] + params["docstring_char_limit"]],
                        }
                        for uid in params["uids"]
                    ]
                )
            if "LIMIT $scan_limit" in query and "MATCH (node:Function)" in query:
                return _Rows(candidates)
            return _Rows([])

    class _Driver:
        def session(self):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "kuzu"

        def get_driver(self, graph_name=None):
            return _Driver()

    response = find_code(
        CodeFinder(_DB()),
        query="target",
        fuzzy_search=True,
        edit_distance=0,
        repo_path="/work/repo",
        offset=1,
        page_size=2,
        include_source=True,
        include_docstring=True,
        source_offset=4,
        docstring_offset=6,
        source_char_limit=32,
        docstring_char_limit=24,
        source_line_limit=3,
    )

    scan_queries = [query for query, _ in calls if "LIMIT $scan_limit" in query]
    assert scan_queries
    assert all("node.source" not in query for query in scan_queries)
    assert all("node.docstring" not in query for query in scan_queries)
    hydration_call = next(
        (query, params)
        for query, params in calls
        if "WHERE node.uid IN $uids" in query
    )
    assert hydration_call[1]["uids"] == ["uid-1", "uid-2"]
    assert hydration_call[1]["source_start"] == 5
    assert hydration_call[1]["docstring_start"] == 7
    assert "substring(node.source, $source_start, $source_char_limit)" in hydration_call[0]
    page = response["results"]["ranked_results"]
    assert len(page) == 2
    assert all(item["source_available"] and item["docstring_available"] for item in page)
    assert all(len(item["source"]) <= 32 for item in page)
    assert all(item["source_total_chars"] == len(source) for item in page)
    assert all(item["docstring_total_chars"] == len(docstring) for item in page)


@pytest.mark.parametrize(
    "pattern",
    ["(f)-->(g)", "(g)<--(f)"],
)
def test_repository_scope_rejects_bracketless_nested_relationship_patterns(pattern):
    with pytest.raises(ValueError, match="nested graph patterns"):
        _scope_cypher_query(
            f"MATCH (f:Function) WHERE {pattern} RETURN f",
            "/work/repo",
        )


def test_name_and_content_candidates_omit_large_evidence_fields():
    db = _CapturingSearchDB()
    finder = CodeFinder(db)

    finder.find_by_function_name("target", fuzzy_search=False)
    finder.find_by_function_name("target", fuzzy_search=True)
    finder.find_by_class_name("target", fuzzy_search=False)
    finder.find_by_variable_name("target")
    finder.find_by_content("target")

    assert len(db.calls) == 5
    for query, _ in db.calls:
        projection = query.split("RETURN", 1)[1].split("ORDER BY", 1)[0]
        assert "node.source" not in projection
        assert "node.docstring" not in projection
        assert "v.value" not in projection
        assert "v.context" not in projection


def test_variable_search_can_fetch_bounded_context_and_value_on_demand():
    db = _CapturingSearchDB()
    finder = CodeFinder(db)

    finder.find_by_variable_name("target", include_context=True, include_value=True)

    query, params = db.calls[-1]
    projection = query.split("RETURN", 1)[1].split("ORDER BY", 1)[0]
    assert "substring(v.context, $evidence_start, $evidence_limit) as context" in projection
    assert "substring(v.value, $evidence_start, $evidence_limit) as value" in projection
    assert params["evidence_limit"] <= 1_024
    assert params["evidence_start"] == 0


def test_variable_search_omits_evidence_unless_requested():
    db = _CapturingSearchDB()
    finder = CodeFinder(db)

    finder.find_by_variable_name("target")

    projection = db.calls[-1][0].split("RETURN", 1)[1].split("ORDER BY", 1)[0]
    assert "v.context" not in projection
    assert "v.value" not in projection


def test_variable_search_uses_repository_prefix_without_matching_siblings():
    db = _CapturingSearchDB()

    CodeFinder(db).find_by_variable_name("target", repo_path="/work/repo")

    query, params = db.calls[-1]
    assert "v.path STARTS WITH $repo_path" in query
    assert params["repo_path"] == "/work/repo/"


def test_whitespace_repository_filter_fails_closed():
    finder = CodeFinder(_CapturingSearchDB())

    with pytest.raises(ValueError, match="repo_path"):
        finder.find_by_function_name("target", fuzzy_search=False, repo_path=" \t\n")


def test_cli_variable_content_and_type_searches_apply_database_offsets():
    db = _CapturingSearchDB()
    finder = CodeFinder(db)

    finder.find_by_variable_name(
        "target", limit=7, offset=13, include_context=True
    )
    variable_query, variable_params = db.calls[-1]
    assert "SKIP $offset" in variable_query
    assert variable_params["offset"] == 13
    assert variable_params["limit"] == 7

    finder.find_by_content("needle", limit=7, offset=13)
    content_query, content_params = db.calls[-1]
    assert "SKIP $offset" in content_query
    assert content_params["offset"] == 13
    assert content_params["limit"] == 7

    finder.find_by_type("function", limit=7, offset=13)
    type_query, type_params = db.calls[-1]
    assert "SKIP $offset" in type_query
    assert type_params["offset"] == 13
    assert type_params["limit"] == 7


def test_kuzu_variable_evidence_preview_uses_one_based_substring_index(tmp_path):
    pytest.importorskip("kuzu")
    from codegraphcontext.core.database_kuzu import KuzuDBManager

    manager = KuzuDBManager(str(tmp_path / "kuzu-variable-evidence.db"))
    try:
        with manager.get_driver().session() as session:
            session.run(
                "CREATE (:Variable {uid: $uid, name: $name, path: $path, "
                "line_number: 1, is_dependency: false, value: $value, context: $context})",
                uid="kuzu-variable-evidence",
                name="setting",
                path="/work/repo/settings.py",
                value="enabled",
                context="設定値 = enabled",
            )

        results = CodeFinder(manager).find_by_variable_name(
            "setting", include_context=True, include_value=True
        )
        assert results[0]["context"] == "設定値 = enabled"
        assert results[0]["value"] == "enabled"
    finally:
        manager.close_driver()


def test_kuzu_cli_search_offsets_return_the_requested_rows(tmp_path):
    pytest.importorskip("kuzu")
    from codegraphcontext.core.database_kuzu import KuzuDBManager

    repo = "/work/repo"
    manager = KuzuDBManager(str(tmp_path / "kuzu-cli-search-offset.db"))
    try:
        with manager.get_driver().session() as session:
            for index in range(3):
                session.run(
                    "CREATE (:Function {uid: $uid, name: $name, path: $path, "
                    "line_number: $line, is_dependency: false})",
                    uid=f"function-{index}",
                    name=f"function_{index}",
                    path=f"{repo}/function_{index}.py",
                    line=index + 1,
                )
                session.run(
                    "CREATE (:Variable {uid: $uid, name: $name, path: $path, "
                    "line_number: $line, is_dependency: false, context: $context})",
                    uid=f"variable-{index}",
                    name=f"setting_{index}",
                    path=f"{repo}/settings_{index}.py",
                    line=index + 1,
                    context=f"setting_{index} = true",
                )

        finder = CodeFinder(manager)
        function_page = finder.find_by_type(
            "function", limit=1, repo_path=repo, offset=1
        )
        variable_page = finder.find_by_variable_name(
            "setting_", repo_path=repo, limit=1, offset=1, include_context=True
        )

        assert function_page[0]["name"] == "function_1"
        assert variable_page[0]["name"] == "setting_1"
        assert variable_page[0]["context"] == "setting_1 = true"
    finally:
        manager.close_driver()


def test_variable_evidence_reports_when_a_bounded_preview_is_truncated():
    class _Rows:
        def data(self):
            return [{
                "name": "target",
                "path": "/work/repo/a.py",
                "line_number": 1,
                "is_dependency": False,
                "_sort_uid": "variable-uid",
                "context": "c" * 1_024,
                "_context_total_chars": 2_000,
                "value": "v" * 1_024,
                "_value_total_chars": 1_024,
            }]

    class _Session(_CapturingSearchSession):
        def run(self, query, **params):
            self.manager.calls.append((query, params))
            return _Rows()

    class _Driver(_CapturingSearchDriver):
        def session(self):
            return _Session(self.manager)

    class _DB(_CapturingSearchDB):
        def get_driver(self, graph_name=None):
            return _Driver(self)

    result = CodeFinder(_DB()).find_by_variable_name(
        "target", include_context=True, include_value=True
    )[0]

    assert result["context_truncated"] is True
    assert result["context_total_chars"] == 2_000
    assert result["value_truncated"] is False
    assert result["value_total_chars"] == 1_024


def test_falkordb_content_search_does_not_return_partial_results_on_category_error():
    class _Rows:
        def data(self):
            return [{
                "type": "function",
                "name": "function_match",
                "path": "/work/repo/a.py",
                "line_number": 1,
                "is_dependency": False,
                "_sort_uid": "function-uid",
            }]

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            if "MATCH (node:Class)" in query:
                raise RuntimeError("class content scan failed")
            return _Rows()

    class _Driver:
        def session(self):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "falkordb"

        def get_driver(self, graph_name=None):
            return _Driver()

    with pytest.raises(RuntimeError, match="class content scan failed"):
        CodeFinder(_DB()).find_by_content("needle")


def test_variable_page_hydration_restores_value_and_context_evidence():
    class _Rows:
        def data(self):
            return [{
                "_sort_uid": "variable-uid",
                "_source_preview": "let setting = true",
                "_source_total_chars": 18,
                "_docstring_preview": "variable docs",
                "_docstring_total_chars": 13,
                "_value_preview": "true",
                "_value_total_chars": 4,
                "_context_preview": "setting = true",
                "_context_total_chars": 14,
            }]

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            assert "substring(node.value, $preview_start, $preview_limit) as _value_preview" in query
            assert "substring(node.context, $preview_start, $preview_limit) as _context_preview" in query
            assert params["uids"] == ["variable-uid"]
            return _Rows()

    class _Driver:
        def session(self):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, graph_name=None):
            return _Driver()

    candidate = {
        "search_type": "variable_name",
        "type": "variable",
        "_sort_uid": "variable-uid",
    }
    CodeFinder(_DB()).hydrate_code_evidence([candidate])

    assert candidate["_value_preview"] == "true"
    assert candidate["_context_preview"] == "setting = true"


def test_evidence_hydration_returns_bounded_preview_and_requested_page_only():
    source = "line\n" * 20_000
    docstring = "说明\n" * 20_000
    calls = []

    class _Rows:
        def __init__(self, rows):
            self.rows = rows

        def data(self):
            return [dict(row) for row in self.rows]

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            assert "substring(node.source, $preview_start, $preview_limit)" in query
            assert "size(node.source) as _source_total_chars" in query
            assert "substring(node.source, $source_start, $source_char_limit)" in query
            assert "substring(node.docstring, $docstring_start, $docstring_char_limit)" in query
            return _Rows([{
                "_sort_uid": "large-function",
                "_source_preview": source[:params["preview_limit"]],
                "_source_total_chars": len(source),
                "_docstring_preview": docstring[:params["preview_limit"]],
                "_docstring_total_chars": len(docstring),
                "_source_page": source[params["source_offset"]:params["source_offset"] + params["source_char_limit"]],
                "_docstring_page": docstring[params["docstring_offset"]:params["docstring_offset"] + params["docstring_char_limit"]],
            }])

    class _Driver:
        def session(self):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, graph_name=None):
            return _Driver()

    candidate = {
        "search_type": "content",
        "type": "function",
        "_sort_uid": "large-function",
    }
    CodeFinder(_DB()).hydrate_code_evidence(
        [candidate],
        include_source=True,
        include_docstring=True,
        source_offset=100,
        docstring_offset=200,
        source_char_limit=20,
        docstring_char_limit=24,
    )

    assert len(calls) == 1
    query, params = calls[0]
    assert params["preview_limit"] <= 1024
    assert params["preview_start"] == 0
    assert params["source_offset"] == 100
    assert params["docstring_offset"] == 200
    assert candidate["_source_total_chars"] == len(source)
    assert candidate["_docstring_total_chars"] == len(docstring)
    assert len(candidate["_source_preview"]) <= 1024
    assert len(candidate["_docstring_preview"]) <= 1024
    assert candidate["_source_page"] == source[100:120]
    assert candidate["_docstring_page"] == docstring[200:224]
    assert " as source" not in query
    assert " as docstring" not in query


def test_kuzu_evidence_hydration_uses_its_one_based_substring_indices(tmp_path):
    pytest.importorskip("kuzu")
    from codegraphcontext.core.database_kuzu import KuzuDBManager

    manager = KuzuDBManager(str(tmp_path / "kuzu-evidence.db"))
    try:
        with manager.get_driver().session() as session:
            session.run(
                "CREATE (:Function {uid: $uid, name: $name, path: $path, "
                "line_number: 1, source: $source, docstring: $docstring})",
                uid="kuzu-evidence",
                name="selected",
                path="/work/repo/a.py",
                source="abcdef",
                docstring="你好世界",
            )
        candidate = {
            "search_type": "function_name",
            "type": "function",
            "_sort_uid": "kuzu-evidence",
        }
        CodeFinder(manager).hydrate_code_evidence(
            [candidate],
            include_source=True,
            source_offset=2,
            source_char_limit=3,
            include_docstring=True,
            docstring_offset=1,
            docstring_char_limit=2,
        )

        assert candidate["_source_page"] == "cde"
        assert candidate["_source_preview"] == "abcdef"
        assert candidate["_source_total_chars"] == 6
        assert candidate["_docstring_page"] == "好世"
        assert candidate["_docstring_preview"] == "你好世界"
        assert candidate["_docstring_total_chars"] == 4
    finally:
        manager.close_driver()


@pytest.mark.parametrize("requested_graph", ["requested-graph", None])
def test_find_code_hydration_uses_the_requested_graph_after_active_graph_changes(requested_graph):
    graph_calls = []

    class _Rows:
        def data(self):
            return [{
                "_sort_uid": "graph-specific",
                "_source_preview": "def selected(): pass",
                "_source_total_chars": 21,
                "_docstring_preview": "",
                "_docstring_total_chars": 0,
            }]

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            return _Rows()

    class _Driver:
        def session(self):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, graph_name=None):
            graph_calls.append(graph_name)
            return _Driver()

    finder = CodeFinder(_DB())

    def _ranked_rows(*args, **kwargs):
        finder._active_graph = "other-request-graph"
        item = {
            "name": "selected",
            "path": "/work/repo/a.py",
            "line_number": 1,
            "is_dependency": False,
            "search_type": "function_name",
            "type": "function",
            "_sort_uid": "graph-specific",
        }
        return {
            "functions_by_name": [item],
            "classes_by_name": [],
            "variables_by_name": [],
            "content_matches": [],
            "ranked_results": [item],
        }

    finder.find_related_code = _ranked_rows
    result = find_code(
        finder,
        query="selected",
        repo_path="/work/repo",
        graph_name=requested_graph,
        page_size=1,
    )

    assert result["success"] is True
    assert graph_calls == [requested_graph]


def test_find_datasource_nodes_scopes_rows_and_tables_to_repository_sources():
    calls = []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            return []

    class _Driver:
        def session(self):
            return _Session()

    class _Finder:
        driver = _Driver()

        @staticmethod
        def _normalize_repo_path_prefix(repo_path):
            return repo_path.rstrip("/\\") + "/"

    result = analysis_handlers.find_datasource_nodes(
        _Finder(), repo_path="/work/repo", include_columns=True
    )

    assert result["success"] is True
    assert len(calls) == 2
    for query, params in calls:
        assert "source.path STARTS WITH $repo_path" in query
        assert params["repo_path"] == "/work/repo/"
    assert "RedisKeyPattern" not in "\n".join(query for query, _ in calls)


def test_module_and_import_searches_can_filter_to_repository_sources():
    calls = []

    class _Result:
        def data(self):
            return []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            return _Result()

    class _Driver:
        def session(self):
            return _Session()

    class _DBManager:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, graph_name=None):
            return _Driver()

    finder = CodeFinder(_DBManager())
    finder.find_by_module_name("json", repo_path="/work/repo")
    finder.find_imports("json", repo_path="/work/repo")

    assert len(calls) == 2
    for query, params in calls:
        assert "f.path STARTS WITH $repo_path" in query
        assert params["repo_path"] == "/work/repo/"


def test_find_datasource_nodes_requires_explicit_global_scope_for_global_metadata():
    calls = []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            return []

    class _Driver:
        def session(self):
            return _Session()

    class _Finder:
        driver = _Driver()

    result = analysis_handlers.find_datasource_nodes(_Finder(), global_scope=True)

    assert result["success"] is True
    assert result["global_scope"] is True
    assert len(calls) == 2
    assert all("repo_path" not in params for _, params in calls)


def test_find_datasource_nodes_excludes_other_repository_tables_on_kuzu(tmp_path):
    pytest.importorskip("kuzu")
    from codegraphcontext.core.database_kuzu import KuzuDBManager

    manager = KuzuDBManager(str(tmp_path / "datasource-scope.db"))
    try:
        with manager.get_driver().session() as session:
            session.run(
                "CREATE (:Datasource {name:'local', kind:'mysql', host:'', env:''})"
            )
            session.run(
                "CREATE (:DbTable {fqn:'local.users', name:'users', "
                "datasource_name:'local', table_type:'', comment:''})"
            )
            session.run(
                "CREATE (:Function {uid:'local-function', name:'query', "
                "path:'/repo/local/a.py', line_number:1})"
            )
            session.run(
                "CREATE (:Datasource {name:'other', kind:'mysql', host:'', env:''})"
            )
            session.run(
                "CREATE (:DbTable {fqn:'other.users', name:'users', "
                "datasource_name:'other', table_type:'', comment:''})"
            )
            session.run(
                "CREATE (:Function {uid:'other-function', name:'query', "
                "path:'/repo/other/a.py', line_number:1})"
            )
            session.run(
                "MATCH (f:Function {uid:'local-function'}), "
                "(t:DbTable {fqn:'local.users'}), (d:Datasource {name:'local'}) "
                "CREATE (f)-[:READS]->(t) CREATE (t)-[:STORED_IN]->(d)"
            )
            session.run(
                "MATCH (f:Function {uid:'other-function'}), "
                "(t:DbTable {fqn:'other.users'}), (d:Datasource {name:'other'}) "
                "CREATE (f)-[:READS]->(t) CREATE (t)-[:STORED_IN]->(d)"
            )

        response = analysis_handlers.find_datasource_nodes(
            CodeFinder(manager), repo_path="/repo/local"
        )
    finally:
        manager.close_driver()

    assert response["repo_scope"] == "/repo/local/"
    assert [row["datasource"] for row in response["datasources"]] == ["local"]
    assert response["datasources"][0]["tables"] == ["local.users"]


@pytest.mark.parametrize(
    ("tool_name", "collection", "stable_key"),
    [
        ("find_java_spring_endpoints", "endpoints", "fn.uid"),
        ("find_java_spring_beans", "beans", "c.uid"),
    ],
)
def test_spring_tools_page_rows_with_stable_database_order(
    tool_name, collection, stable_key
):
    calls = []
    rows = [
        {"name": f"row-{index}", "line_number": index}
        for index in range(3)
    ]

    class _Result:
        def __iter__(self):
            return iter(rows)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            return _Result()

    class _Finder:
        driver = type("_Driver", (), {"session": lambda self: _Session()})()

        @staticmethod
        def _normalize_repo_path_prefix(path):
            return path.rstrip("/\\") + "/"

    response = getattr(analysis_handlers, tool_name)(
        _Finder(), repo_path="/work/repo", offset=2, page_size=2
    )

    query, params = calls[0]
    assert "SKIP $spring_offset LIMIT $spring_candidate_limit" in query
    assert "ORDER BY" in query and stable_key in query
    assert params["spring_offset"] == 2
    assert params["spring_candidate_limit"] == 3
    assert response["success"] is True
    assert response[collection] == rows[:2]
    assert response["count"] == 2
    assert response["offset"] == 2
    assert response["page_size"] == 2
    assert response["truncated"] is True
    assert response["next_offset"] == 4
    assert response["continuation_limit_reached"] is False


def test_find_datasource_nodes_pages_each_collection_and_exposes_retry_inputs():
    calls = []
    datasource_rows = [
        {"datasource": "page-db", "kind": "mysql", "host": "", "env": ""},
        {"datasource": "lookahead-db", "kind": "mysql", "host": "", "env": ""},
    ]
    table_rows = [
        {"datasource": "page-db", "table_fqn": f"db.table_{index}"}
        for index in range(3)
    ]
    column_rows = [
        {
            "datasource": "page-db",
            "table_fqn": "db.table_0",
            "col_name": f"column_{index}",
            "col_type": "TEXT",
            "nullable": True,
            "is_pk": False,
        }
        for index in range(3)
    ]
    key_pattern_rows = [
        {
            "datasource": "page-db",
            "pattern": f"key:{index}",
            "key_type": "string",
            "count": index,
        }
        for index in range(3)
    ]

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            if "AS datasource_count" in query:
                return [{"datasource_count": 3}]
            if "AS col_name" in query:
                return column_rows
            if "AS pattern" in query:
                return key_pattern_rows
            if "AS table_fqn" in query:
                return table_rows
            return datasource_rows

    class _Finder:
        driver = type("_Driver", (), {"session": lambda self: _Session()})()

    response = analysis_handlers.find_datasource_nodes(
        _Finder(),
        global_scope=True,
        include_columns=True,
        offset=1,
        page_size=1,
        table_offset=2,
        table_page_size=2,
        column_offset=3,
        column_page_size=2,
        key_pattern_offset=4,
        key_pattern_page_size=2,
    )

    assert len(calls) == 5
    expected_windows = {
        "datasource_offset": (1, 2),
        "table_offset": (2, 3),
        "column_offset": (3, 3),
        "key_pattern_offset": (4, 3),
    }
    checked_windows = set()
    for query, params in calls:
        for offset_name, (offset, candidate_limit) in expected_windows.items():
            if f"${offset_name}" in query:
                checked_windows.add(offset_name)
                assert "ORDER BY" in query
                assert params[offset_name] == offset
                assert params[f"{offset_name.removesuffix('_offset')}_candidate_limit"] == candidate_limit
                assert "LIMIT" in query
    assert checked_windows == set(expected_windows)

    assert response["count"] == 3
    assert response["page_count"] == 1
    assert response["datasources"][0]["datasource"] == "page-db"
    assert response["datasources"][0]["tables"] == ["db.table_0", "db.table_1"]
    assert response["datasources"][0]["columns"]["db.table_0"] == [
        {"name": "column_0", "type": "TEXT", "nullable": True, "is_primary_key": False},
        {"name": "column_1", "type": "TEXT", "nullable": True, "is_primary_key": False},
    ]
    assert response["datasources"][0]["key_patterns"] == [
        {"pattern": "key:0", "key_type": "string", "count": 0},
        {"pattern": "key:1", "key_type": "string", "count": 1},
    ]
    assert response["next_offset"] == 2
    assert response["table_next_offset"] == 4
    assert response["column_next_offset"] == 5
    assert response["key_pattern_next_offset"] == 6
    assert response["continuation"] == {
        "offset": 1,
        "page_size": 1,
        "table_offset": 2,
        "table_page_size": 2,
        "column_offset": 3,
        "column_page_size": 2,
        "key_pattern_offset": 4,
        "key_pattern_page_size": 2,
    }
    assert _pagination_retry_metadata(response) == {
        "offset": 1,
        "page_size": 1,
        "table_offset": 2,
        "table_page_size": 1,
        "column_offset": 3,
        "column_page_size": 1,
        "key_pattern_offset": 4,
        "key_pattern_page_size": 1,
    }


@pytest.mark.parametrize("query_type", ["find_callers", "find_callees"])
def test_direct_caller_and_callee_analysis_pages_with_a_stable_cursor(query_type):
    calls = []
    rows = [
        {
            "caller_function": f"caller-{index}",
            "called_function": f"callee-{index}",
            "caller_file_path": f"/work/repo/{index}.py",
            "called_file_path": f"/work/repo/{index}.py",
            "caller_line_number": index,
            "called_line_number": index,
            "call_line_number": index,
            "call_args": "[]",
            "full_call_name": f"call-{index}",
            "caller_is_dependency": False,
            "called_is_dependency": False,
            "target_file_path": "/work/repo/target.py",
            "_caller_uid": f"caller-{index}",
            "_target_uid": f"target-{index}",
            "_call_args_key": f"args-{index}",
            "_edge_type": "CALLS",
        }
        for index in range(3)
    ]
    if query_type == "find_callees":
        for row in rows:
            row.pop("target_file_path")

    class _Result:
        def data(self):
            return rows

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            return _Result()

    class _Manager:
        @staticmethod
        def get_backend_type():
            return "neo4j"

        @staticmethod
        def get_driver(_graph_name=None):
            return type("_Driver", (), {"session": lambda self: _Session()})()

    response = analysis_handlers.analyze_code_relationships(
        CodeFinder(_Manager()),
        query_type=query_type,
        target="target",
        repo_path="/work/repo",
        offset=2,
        page_size=2,
    )

    query, params = calls[0]
    assert "SKIP $offset LIMIT $limit" in query
    assert "ORDER BY" in query
    assert "_caller_uid" in query and "_target_uid" in query
    assert "_call_args_key" in query and "_edge_type" in query
    assert params["offset"] == 2
    assert params["limit"] == 3
    assert response["offset"] == 2
    assert response["page_size"] == 2
    assert response["next_offset"] == 4
    assert response["truncated"] is True
    assert len(response["results"]["results"]) == 2
    assert response["results"]["results"][0]["caller_file_path"] == rows[0]["caller_file_path"]
    assert all(not key.startswith("_") for key in response["results"]["results"][0])
    assert all("target_file_path" not in row for row in response["results"]["results"])


def test_direct_relationship_tool_schema_documents_page_inputs():
    props = TOOLS["analyze_code_relationships"]["inputSchema"]["properties"]
    assert props["offset"]["maximum"] == 99999
    assert props["page_size"]["minimum"] == 1
    assert "find_callers" in props["offset"]["description"]
    assert "find_callers" in props["page_size"]["description"]
    assert "find_callees" in props["page_size"]["description"]


def test_spring_and_datasource_schemas_expose_bounded_collection_offsets():
    for tool_name in ("find_java_spring_endpoints", "find_java_spring_beans"):
        props = TOOLS[tool_name]["inputSchema"]["properties"]
        assert props["offset"]["maximum"] == 99999
        assert props["page_size"]["maximum"] == 100

    props = TOOLS["find_datasource_nodes"]["inputSchema"]["properties"]
    for field in (
        "offset", "page_size", "table_offset", "table_page_size",
        "column_offset", "column_page_size",
        "key_pattern_offset", "key_pattern_page_size",
    ):
        assert field in props


def test_exact_name_search_hydrates_only_selected_page_and_keeps_large_bodies_off_candidates():
    source = "needle" + "x" * 100_000
    docstring = "说明" * 100_000
    candidates = [
        {
            "name": "target",
            "path": f"/work/repo/{index}.py",
            "line_number": index + 1,
            "is_dependency": False,
            "_sort_uid": f"uid-{index}",
        }
        for index in range(3)
    ]
    calls = []

    class _Rows:
        def __init__(self, rows):
            self.rows = rows

        def data(self):
            return [dict(row) for row in self.rows]

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            if "WHERE node.uid IN $uids" in query:
                return _Rows([
                    {
                        "_sort_uid": uid,
                        "_source_preview": source[:params["preview_limit"]],
                        "_source_total_chars": len(source),
                        "_docstring_preview": docstring[:params["preview_limit"]],
                        "_docstring_total_chars": len(docstring),
                    }
                    for uid in params["uids"]
                ])
            if "MATCH (node:Function {name:" in query:
                return _Rows(candidates[: params["limit"]])
            return _Rows([])

    class _Driver:
        def session(self):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, graph_name=None):
            return _Driver()

    response = find_code(
        CodeFinder(_DB()),
        query="target",
        repo_path="/work/repo",
        offset=1,
        page_size=1,
    )

    candidate_queries = [
        query for query, _ in calls if "WHERE node.uid IN $uids" not in query
    ]
    assert candidate_queries
    assert all("node.source" not in query.split("RETURN", 1)[1].split("ORDER BY", 1)[0]
               for query in candidate_queries)
    assert all("node.docstring" not in query.split("RETURN", 1)[1].split("ORDER BY", 1)[0]
               for query in candidate_queries)
    hydration_queries = [
        (query, params) for query, params in calls if "WHERE node.uid IN $uids" in query
    ]
    assert len(hydration_queries) == 1
    assert hydration_queries[0][1]["uids"] == ["uid-1"]
    assert "substring(node.source, $preview_start, $preview_limit)" in hydration_queries[0][0]
    assert hydration_queries[0][1]["preview_limit"] <= 1024
    page_item = response["results"]["ranked_results"][0]
    assert page_item["source_available"] is True
    assert len(page_item["source_excerpt"]) <= 240
    assert "source" not in page_item


@pytest.mark.parametrize(
    "invalid_args",
    [
        {"offset": 1.5},
        {"offset": True},
        {"page_size": 1.5},
        {"page_size": False},
        {"source_offset": 1.5},
        {"docstring_offset": True},
        {"source_char_limit": 5.5},
        {"docstring_char_limit": False},
        {"source_line_limit": 2.5},
    ],
)
def test_find_code_rejects_non_integer_pagination_and_evidence_limits(invalid_args):
    finder = _FakeCodeFinder([_item()])

    result = find_code(finder, query="target", **invalid_args)

    assert "error" in result
    assert "integer" in result["error"]
    assert finder.last_kwargs is None


@pytest.mark.parametrize(
    "invalid_args",
    [
        {"offset": 1.5},
        {"offset": True},
        {"page_size": 1.5},
        {"page_size": False},
    ],
)
def test_execute_cypher_query_rejects_non_integer_page_values_before_dispatch(invalid_args):
    db = _FakeDBManager()

    result = execute_cypher_query(
        db,
        cypher_query="MATCH (n:Function) RETURN n ORDER BY n.uid",
        **invalid_args,
    )

    assert "error" in result
    assert "integer" in result["error"]
    assert db.last_query is None


def test_scoping_keeps_repository_predicate_outside_trailing_line_comment():
    scoped, _ = _scope_cypher_query(
        "MATCH (f:Function) WHERE f.name = $name // caller note\nRETURN f",
        "/work/repo",
    )

    comment_tail = scoped.split("// caller note", 1)[1].split("\n", 1)
    assert len(comment_tail) == 2
    assert "f.path" not in comment_tail[0]
    assert "f.path STARTS WITH $_cgc_repo_prefix" in comment_tail[1]


def test_cypher_paging_ignores_clause_keyword_in_property_access():
    result = execute_cypher_query(
        _FakeDBManager(row_count=6),
        cypher_query="MATCH (n:Function) RETURN n.limit ORDER BY n.uid",
        repo_path="/work/repo",
        page_size=2,
    )

    assert result["success"] is True
    assert result["has_more"] is True
    assert result["next_offset"] == 2


def test_execute_cypher_query_pages_rows_without_dropping_remaining_rows():
    db = _FakeDBManager()
    result = execute_cypher_query(
        db,
        cypher_query="MATCH (n:Function) RETURN n ORDER BY n.uid",
        repo_path="/work/repo",
        offset=2,
        page_size=2,
    )

    assert result["results"] == [{"id": 2}, {"id": 3}]
    assert result["offset"] == 2
    assert result["page_size"] == 2
    assert result["has_more"] is True
    assert result["next_offset"] == 4
    assert result["truncated"] is True
    assert "n.path STARTS WITH $_cgc_repo_prefix" in db.last_query
    assert db.last_params["_cgc_repo_prefix"] == "/work/repo/"

    malformed_boolean_db = _FakeDBManager()
    execute_cypher_query(
        malformed_boolean_db,
        cypher_query="MATCH (n:Function) RETURN n ORDER BY n.uid",
        repo_path="/work/repo",
        global_scope="false",
    )
    assert "n.path STARTS WITH $_cgc_repo_prefix" in malformed_boolean_db.last_query


def test_execute_cypher_query_requires_ordering_when_paging_spans_rows():
    db = _FakeDBManager()
    result = execute_cypher_query(
        db,
        cypher_query="MATCH (n:Function) RETURN n",
        repo_path="/work/repo",
        page_size=2,
    )

    assert "add a stable ORDER BY" in result["error"]
    assert "id" not in result

    later_page = execute_cypher_query(
        _FakeDBManager(),
        cypher_query="MATCH (n:Function) RETURN n",
        offset=2,
        page_size=2,
    )
    assert "ORDER BY" in later_page["error"]

    duplicate_key_order = execute_cypher_query(
        _FakeDBManager(),
        cypher_query="MATCH (n:Function) RETURN n ORDER BY n.name",
        offset=2,
        page_size=2,
    )
    assert "unique tie-breaker" in duplicate_key_order["error"]

    duplicate_key_first_page = execute_cypher_query(
        _FakeDBManager(row_count=4),
        cypher_query="MATCH (n:Function) RETURN n ORDER BY n.name",
        page_size=2,
    )
    assert "unique tie-breaker" in duplicate_key_first_page["error"]


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:Function)-[:CALLS]->(m:Function) "
        "RETURN n, m ORDER BY n.uid, m.uid",
        "MATCH (n:Function)-->(m:Function) "
        "RETURN n, m ORDER BY n.uid, m.uid",
        "MATCH (n:Function)<--(m:Function) "
        "RETURN n, m ORDER BY n.uid, m.uid",
    ],
)
def test_execute_cypher_query_rejects_relationship_rows_before_continuation(query):
    db = _FakeDBManager()

    result = execute_cypher_query(
        db,
        cypher_query=query,
        repo_path="/work/repo",
        offset=2,
        page_size=2,
    )

    assert "unique tie-breaker" in result["error"]
    assert db.last_query is None


def test_raw_cypher_paging_rejects_relationship_rows_without_edge_identity():
    query = (
        "MATCH (a:Function)-[r:CALLS]->(b:Function) "
        "RETURN a, b, r ORDER BY a.uid, b.uid"
    )
    assert not _has_stable_unique_final_order_by(query)

    db = _FakeDBManager(row_count=4)
    result = execute_cypher_query(
        db,
        cypher_query=query,
        repo_path="/work/repo",
        offset=2,
        page_size=2,
    )

    assert "unique tie-breaker" in result["error"]
    assert db.last_query is None


def test_raw_cypher_paging_rejects_relationship_rows_ordered_only_by_endpoints():
    query = (
        "MATCH (a:Function)-[r:CALLS]->(b:Function) "
        "RETURN r.uid ORDER BY a.uid, b.uid"
    )
    assert not _has_stable_unique_final_order_by(query)

    db = _FakeDBManager(row_count=4)
    result = execute_cypher_query(
        db,
        cypher_query=query,
        repo_path="/work/repo",
        offset=2,
        page_size=2,
    )

    assert "unique tie-breaker" in result["error"]
    assert db.last_query is None


def test_raw_cypher_paging_validator_rejects_unwind_row_multiplicity():
    query = (
        "MATCH (n:Function) UNWIND range(1, 3) AS copy "
        "RETURN n, copy ORDER BY n.uid"
    )
    assert not _has_stable_unique_final_order_by(query)


def test_raw_cypher_paging_rejects_with_alias_rebinding():
    query = (
        "MATCH (f:Function) WITH f AS old MATCH (f:Function) "
        "RETURN old, f ORDER BY f.uid"
    )
    assert not _has_stable_unique_final_order_by(query)

    db = _FakeDBManager(row_count=4)
    result = execute_cypher_query(
        db,
        cypher_query=query,
        offset=2,
        page_size=2,
    )
    assert "unique tie-breaker" in result["error"]
    assert db.last_query is None


def test_execute_cypher_query_does_not_emit_an_unaccepted_boundary_offset(monkeypatch):
    monkeypatch.setattr(query_handlers, "_EXECUTE_CYPHER_MAX_OFFSET", 4)
    db = _FakeDBManager(row_count=6)

    result = execute_cypher_query(
        db,
        cypher_query="MATCH (n:Function) RETURN n ORDER BY n.uid",
        offset=3,
        page_size=5,
    )

    assert result["page_size"] == 1
    assert result["has_more"] is True
    assert result["continuation_limit_reached"] is True
    assert result["next_offset"] is None
    assert "error" in execute_cypher_query(
        db,
        cypher_query="MATCH (n:Function) RETURN n ORDER BY n.uid",
        offset=4,
        page_size=1,
    )


def test_repository_scope_reuses_labels_across_match_clauses():
    query = (
        "MATCH (f:Function) OPTIONAL MATCH (f)-[:CALLS]->(g:Function) "
        "RETURN f.name, g.name"
    )

    scoped, params = _scope_cypher_query(query, "/work/repo")

    assert "OPTIONAL MATCH (f)-[:CALLS]->(g:Function)" in scoped
    assert "f.path STARTS WITH $_cgc_repo_prefix" in scoped
    assert "g.path STARTS WITH $_cgc_repo_prefix" in scoped
    assert params["_cgc_repo_root"] == "/work/repo"

    db = _FakeDBManager(row_count=1)
    result = execute_cypher_query(db, cypher_query=query, repo_path="/work/repo")
    assert result["success"] is True
    assert "f.path STARTS WITH $_cgc_repo_prefix" in db.last_query
    assert "g.path STARTS WITH $_cgc_repo_prefix" in db.last_query
    assert db.last_params["_cgc_repo_root"] == "/work/repo"


@pytest.mark.parametrize(
    "query",
    [
        (
            "MATCH (f:Function) WITH f OPTIONAL MATCH (f)-[:CALLS]->(g:Function) "
            "RETURN f.name, g.name"
        ),
        (
            "MATCH (f:Function) WITH f AS previous OPTIONAL MATCH "
            "(previous)-[:CALLS]->(g:Function) RETURN previous.name, g.name"
        ),
    ],
)
def test_repository_scope_tracks_node_labels_through_with_projections(query):
    scoped, _ = _scope_cypher_query(query, "/work/repo")

    assert "STARTS WITH $_cgc_repo_prefix" in scoped
    assert "g.path STARTS WITH $_cgc_repo_prefix" in scoped


def test_repository_scope_reuses_previously_scoped_pathless_module():
    scoped, _ = _scope_cypher_query(
        "MATCH (f:File)-[:IMPORTS]->(m:Module) "
        "OPTIONAL MATCH (m)-[:MODULE_DEPENDS_ON]->(dep:MavenModule) "
        "RETURN m.name, dep.name",
        "/work/repo",
    )

    assert "f.path STARTS WITH $_cgc_repo_prefix" in scoped
    assert "m.path IS NULL" in scoped
    assert "dep.path STARTS WITH $_cgc_repo_prefix" in scoped


def test_repository_scope_drops_node_labels_not_projected_by_with():
    with pytest.raises(ValueError, match="unlabeled node"):
        _scope_cypher_query(
            "MATCH (f:Function) WITH 1 AS f OPTIONAL MATCH (f)-[:CALLS]->(g:Function) "
            "RETURN f, g",
            "/work/repo",
        )


def test_repository_scope_does_not_reuse_labels_across_union_branches():
    with pytest.raises(ValueError, match="unlabeled node"):
        _scope_cypher_query(
            "MATCH (f:Function) RETURN f UNION MATCH (f) RETURN f",
            "/work/repo",
        )


def test_execute_cypher_query_adds_a_default_repository_predicate():
    scoped, params = _scope_cypher_query(
        "MATCH (f:Function)-[:CALLS]->(g:Function) "
        "WHERE f.name = $name RETURN g",
        "/work/repo",
    )

    assert "f.path" in scoped and "g.path" in scoped
    assert "STARTS WITH $_cgc_repo_prefix" in scoped
    assert scoped.index("AND ((f.path") > scoped.index("WHERE (f.name = $name)")
    assert scoped.index("RETURN g") > scoped.index("AND ((f.path")
    assert params["_cgc_repo_root"] == "/work/repo"
    assert params["_cgc_repo_prefix"] == "/work/repo/"
    assert not "/work/repo-other/file.py".startswith(params["_cgc_repo_prefix"])

    grouped, _ = _scope_cypher_query(
        "MATCH (f:Function) WHERE f.name = 'first' OR f.name = 'second' RETURN f",
        "/work/repo",
    )
    assert (
        "WHERE (f.name = 'first' OR f.name = 'second') AND ("
        in grouped
    )

    quoted, _ = _scope_cypher_query(
        "MATCH (`function-name`:Function) RETURN `function-name`", "/work/repo"
    )
    assert "`function-name`.path STARTS WITH $_cgc_repo_prefix" in quoted

    with pytest.raises(ValueError, match="no MATCH clause"):
        _scope_cypher_query("CALL db.labels()", "/work/repo")
    with pytest.raises(ValueError, match="nested MATCH clauses"):
        _scope_cypher_query(
            "MATCH (f:Function) WHERE EXISTS { MATCH (other:Function) RETURN other } RETURN f",
            "/work/repo",
        )
    with pytest.raises(ValueError, match="ExternalLibrary"):
        _scope_cypher_query(
            "MATCH (module:MavenModule)-[:USES_LIBRARY]->(library:ExternalLibrary) RETURN library",
            "/work/repo",
        )
    module_query, _ = _scope_cypher_query(
        "MATCH (f:File)-[:IMPORTS]->(m:Module) RETURN m.name",
        "/work/repo",
    )
    assert "f.path STARTS WITH $_cgc_repo_prefix" in module_query
    assert "m.path IS NULL" in module_query
    with pytest.raises(ValueError, match="Module.*connected.*File"):
        _scope_cypher_query(
            "MATCH (f:File), (m:Module) RETURN m.name",
            "/work/repo",
        )
    with pytest.raises(ValueError, match="named, path-bearing label"):
        _scope_cypher_query("MATCH ()-[r:CALLS]->(f:Function) RETURN f", "/work/repo")
    with pytest.raises(ValueError, match="nested graph patterns"):
        _scope_cypher_query(
            "MATCH (f:Function) RETURN [(f)-[:CALLS]->(g:Function) | g.path] AS paths",
            "/work/repo",
        )
    with pytest.raises(ValueError, match="nested graph patterns"):
        _scope_cypher_query(
            "MATCH (f:Function) RETURN [(f)<-[:CALLS]-(g:Function) | g.path] AS paths",
            "/work/repo",
        )
    with pytest.raises(ValueError, match="nested graph patterns"):
        _scope_cypher_query(
            "MATCH (f:Function) RETURN [(g:Function) | g.path] AS paths",
            "/work/repo",
        )
    with pytest.raises(ValueError, match="nested graph patterns"):
        _scope_cypher_query(
            "MATCH (f:Function) RETURN [(g) | g.path] AS paths",
            "/work/repo",
        )


@pytest.mark.parametrize(
    "relationship",
    ["-[:CALLS*1..3]->", "-[:CALLS*]->", "-[*1..2]-"],
)
def test_repository_scope_rejects_variable_length_paths(relationship):
    query = (
        f"MATCH p=(a:Function){relationship}(b:Function) RETURN p"
    )
    with pytest.raises(ValueError, match="variable-length"):
        _scope_cypher_query(query, "/work/repo")


def test_repository_scope_allows_multiplication_inside_list_index():
    scoped, _ = _scope_cypher_query(
        "MATCH (f:Function) WHERE f.items[index * 2] > 0 RETURN f",
        "/work/repo",
    )

    assert "f.path STARTS WITH $_cgc_repo_prefix" in scoped


def test_scoped_query_rejects_global_procedure_calls_before_execution():
    query = "MATCH (f:Function) CALL db.labels() YIELD label RETURN label"
    with pytest.raises(ValueError, match="CALL.*global_scope=true"):
        _scope_cypher_query(query, "/work/repo")

    db = _FakeDBManager()
    result = execute_cypher_query(db, cypher_query=query, repo_path="/work/repo")
    assert "global_scope=true" in result["error"]
    assert db.last_query is None

    paged = execute_cypher_query(
        db,
        cypher_query=query,
        repo_path="/work/repo",
        offset=2,
        page_size=2,
    )
    assert "CALL" in paged["error"] and "global_scope=true" in paged["error"]
    assert db.last_query is None


def test_scoped_query_rejects_global_procedure_calls_inside_subqueries():
    query = (
        "MATCH (f:Function) WHERE EXISTS { CALL db.labels() YIELD label "
        "WHERE label = 'OtherRepoOnlyLabel' RETURN label } RETURN f"
    )

    with pytest.raises(ValueError, match="CALL.*global_scope=true"):
        _scope_cypher_query(query, "/work/repo")

    db = _FakeDBManager()
    result = execute_cypher_query(db, cypher_query=query, repo_path="/work/repo")
    assert "global_scope=true" in result["error"]
    assert db.last_query is None

    scoped_property_query, _ = _scope_cypher_query(
        "MATCH (f:Function) WHERE f.call = true RETURN f", "/work/repo"
    )
    assert "f.path STARTS WITH $_cgc_repo_prefix" in scoped_property_query


def test_execute_cypher_query_allows_unscoped_metadata_only_when_explicit():
    db = _FakeDBManager()
    result = execute_cypher_query(
        db,
        cypher_query="CALL db.labels()",
        repo_path="/work/repo",
        global_scope=True,
    )

    assert result["success"] is True
    assert db.last_query == "CALL db.labels()"
    assert "repo_path" not in result


def test_final_response_limits_count_the_protocol_wrapper_and_preserve_json():
    response_text = json.dumps(
        {
            "offset": 4,
            "page_size": 8,
            "results": ["中文", 'quoted " and backslash \\'],
        },
        ensure_ascii=False,
        indent=2,
    )

    def wrapped_bytes(text):
        response = {
            "jsonrpc": "2.0",
            "id": 17,
            "result": {"content": [{"type": "text", "text": text}]},
        }
        return json.dumps(response).encode("utf-8")

    exact_limit = len(wrapped_bytes(response_text))
    assert (
        _apply_final_output_limits(
            "find_code",
            response_text,
            wrapped_bytes,
            max_bytes=exact_limit,
            max_lines=200,
        )
        == response_text
    )

    limited = _apply_final_output_limits(
        "find_code",
        response_text,
        wrapped_bytes,
        max_bytes=exact_limit - 1,
        max_lines=200,
    )
    inner = json.loads(limited)
    assert inner["truncated"] is True
    assert inner["continuation"] == {"offset": 4, "page_size": 4}
    assert len(wrapped_bytes(limited)) <= exact_limit - 1


def test_final_response_line_cap_and_unicode_byte_cap_are_enforced():
    response_text = json.dumps({"evidence": "中文\\\"" + "行\n" * 6}, ensure_ascii=False, indent=2)
    wrapped = lambda text: json.dumps({"result": {"text": text}}).encode("utf-8")

    limited = _apply_final_output_limits(
        "find_code", response_text, wrapped, max_bytes=1000, max_lines=2
    )
    inner = json.loads(limited)
    assert inner["truncated"] is True
    assert len(wrapped(limited)) <= 1000
    assert len(limited.splitlines()) <= 2
    assert "\ufffd" not in limited


def test_final_response_line_cap_limits_decoded_preview_lines():
    response_text = "\n".join(f"short line {index}" for index in range(201))
    wrapped = lambda text: json.dumps({"result": {"text": text}}).encode("utf-8")

    limited = _apply_final_output_limits(
        "find_code", response_text, wrapped, max_bytes=8192, max_lines=200
    )

    payload = json.loads(limited)
    assert payload["truncated"] is True
    assert len(wrapped(limited)) <= 8192
    assert len(payload["preview"].splitlines()) <= 200


def test_final_limiter_preserves_nested_and_gcf_pagination_metadata(monkeypatch):
    monkeypatch.setattr(
        "codegraphcontext.cli.config_manager.get_config_value",
        lambda key: "300" if key == "MAX_PROMPT_CHARS" else "0",
    )
    token_limited = _apply_response_token_limit(
        "find_code",
        "GCF:" + "x" * 1000,
        pagination={"offset": 7, "page_size": 8},
    )
    token_payload = json.loads(token_limited)
    assert token_payload["continuation"] == {"offset": 7, "page_size": 4}

    wrap = lambda text: json.dumps({"result": {"text": text}}).encode("utf-8")
    constrained = _apply_final_output_limits(
        "find_code",
        token_limited,
        wrap,
        max_bytes=len(wrap(token_limited)) - 1,
        pagination={"offset": 7, "page_size": 8},
    )
    assert json.loads(constrained)["continuation"] == {"offset": 7, "page_size": 2}
    assert len(wrap(constrained)) <= len(wrap(token_limited)) - 1


def test_gcf_truncation_preserves_nested_datasource_retry_cursors(monkeypatch):
    monkeypatch.setattr(
        "codegraphcontext.cli.config_manager.get_config_value",
        lambda key: "300" if key == "MAX_PROMPT_CHARS" else "0",
    )
    pagination = {
        "continuation": {
            "offset": 1,
            "page_size": 8,
            "table_offset": 2,
            "table_page_size": 6,
            "column_offset": 3,
            "column_page_size": 4,
            "key_pattern_offset": 4,
            "key_pattern_page_size": 2,
        }
    }

    limited = _apply_response_token_limit(
        "find_datasource_nodes",
        "GCF:" + "x" * 1000,
        pagination=pagination,
    )

    assert json.loads(limited)["continuation"] == {
        "offset": 1,
        "page_size": 4,
        "table_offset": 2,
        "table_page_size": 3,
        "column_offset": 3,
        "column_page_size": 2,
        "key_pattern_offset": 4,
        "key_pattern_page_size": 1,
    }


def test_oversized_jsonrpc_id_is_rejected_before_tool_output(monkeypatch, capsys):
    request = {
        "jsonrpc": "2.0",
        "id": "x" * (MAX_JSONRPC_ID_BYTES + 1),
        "method": "tools/call",
        "params": {"name": "find_code", "arguments": {"query": "x"}},
    }

    class _Loop:
        def __init__(self):
            self.lines = iter([json.dumps(request) + "\n", ""])

        async def run_in_executor(self, executor, function):
            return next(self.lines)

    server = MCPServer.__new__(MCPServer)

    async def unexpected_tool_call(*args, **kwargs):
        raise AssertionError("oversized request id must be rejected before tool execution")

    server.handle_tool_call = unexpected_tool_call
    asyncio.run(server._run_loop(_Loop()))

    response = json.loads(capsys.readouterr().out)
    assert response["id"] is None
    assert response["error"]["code"] == -32600
    assert len(json.dumps(response).encode("utf-8")) < MAX_TOOL_RESPONSE_BYTES


def test_oversized_stdio_unknown_method_response_is_capped(capsys):
    request = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "unknown-" + "x" * (MAX_TOOL_RESPONSE_BYTES + 128),
    }

    class _Loop:
        def __init__(self):
            self.lines = iter([json.dumps(request) + "\n", ""])

        async def run_in_executor(self, executor, function):
            return next(self.lines)

    server = MCPServer.__new__(MCPServer)
    asyncio.run(server._run_loop(_Loop()))

    serialized = capsys.readouterr().out.strip()
    response = json.loads(serialized)
    assert response["id"] == 7
    assert response["error"]["code"] == -32601
    assert len(serialized.encode("utf-8")) <= MAX_TOOL_RESPONSE_BYTES


def test_content_fallback_merges_falkordb_categories_before_slicing():
    from codegraphcontext.tools.code_finder import CodeFinder

    class _Result:
        def __init__(self, rows):
            self.rows = rows

        def data(self):
            return list(self.rows)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            if "MATCH (node:Function)" in query:
                return _Result([
                    {"type": "function", "name": f"a{i:02}", "path": "/r/a.py", "line_number": i}
                    for i in range(params["limit"])
                ])
            return _Result([
                    {"type": "class", "name": "a015", "path": "/r/a.py", "line_number": 1}
            ])

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_backend_type(self):
            return "falkordb"

        def get_driver(self, graph_name=None):
            return _Driver()

    finder = CodeFinder(_Manager())
    first_window = finder._find_by_content_falkordb("needle", limit=2)
    later_window = finder._find_by_content_falkordb("needle", limit=4)
    offset_window = finder._find_by_content_falkordb("needle", limit=2, offset=2)

    assert [row["name"] for row in first_window] == ["a00", "a015"]
    assert [row["name"] for row in later_window] == ["a00", "a015", "a01", "a02"]
    assert [row["name"] for row in offset_window] == ["a01", "a02"]


@pytest.mark.asyncio
async def test_http_tool_output_limit_counts_the_actual_jsonrpc_id(monkeypatch):
    from types import SimpleNamespace
    from mcp.types import CallToolRequestParams, JSONRPCResponse
    from codegraphcontext.api import mcp_sse

    request_id = "r" * MAX_JSONRPC_ID_BYTES

    class _LargeServer:
        async def handle_tool_call(self, tool_name, args):
            return {"data": "x" * MAX_TOOL_RESPONSE_BYTES}

    async def fake_get_server():
        return _LargeServer()

    monkeypatch.setattr(mcp_sse, "get_server", fake_get_server)

    params = CallToolRequestParams(name="find_code", arguments={"query": "x"})
    result = await mcp_sse.handle_call_tool(
        SimpleNamespace(request_id=request_id), params
    )
    envelope = JSONRPCResponse(
        jsonrpc="2.0",
        id=request_id,
        result=result.model_dump(mode="json", by_alias=True, exclude_unset=True),
    ).model_dump_json(by_alias=True, exclude_unset=True).encode()
    assert len(envelope) <= MAX_TOOL_RESPONSE_BYTES
    assert json.loads(result.content[0].text)["truncated"] is True


def test_query_schemas_expose_scope_paging_and_on_demand_source_controls():
    find_props = TOOLS["find_code"]["inputSchema"]["properties"]
    cypher_props = TOOLS["execute_cypher_query"]["inputSchema"]["properties"]
    relationship_props = TOOLS["analyze_code_relationships"]["inputSchema"]["properties"]

    assert find_props["include_source"]["default"] is False
    assert find_props["offset"]["type"] == "integer"
    assert find_props["offset"]["maximum"] == 99999
    assert find_props["page_size"]["type"] == "integer"
    assert find_props["source_char_limit"]["maximum"] == 1536
    assert cypher_props["repo_path"]["type"] == "string"
    assert cypher_props["global_scope"]["default"] is False
    assert cypher_props["offset"]["type"] == "integer"
    assert cypher_props["offset"]["maximum"] == 99999
    assert cypher_props["page_size"]["type"] == "integer"
    assert "ORDER BY" in cypher_props["page_size"]["description"]
    assert "node-only query" in cypher_props["page_size"]["description"]
    assert "WITH" in cypher_props["page_size"]["description"]
    assert relationship_props["page_size"]["type"] == "integer"
    assert relationship_props["page_size"]["maximum"] == 500


def test_report_suggested_cypher_queries_work_with_default_repository_scope():
    section = report_generator._section_suggested_queries()
    queries = re.findall(r"```cypher\n(.*?)\n```", section, re.DOTALL)

    assert len(queries) == 5
    for query in queries:
        if "ExternalLibrary" in query:
            assert "global_scope=true" in section
            continue
        _scope_cypher_query(query, "/work/repo")


def test_maven_report_counts_dependencies_without_collecting_names():
    calls = []

    class _Session:
        def run(self, query, **params):
            calls.append((query, params))
            return iter([{
                "module": "root",
                "version": "1.0",
                "internal_dep_count": 123,
            }])

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    section = report_generator._section_maven_modules(_Driver(), "/work/repo")

    query = calls[0][0]
    assert "collect(" not in query.lower()
    assert "count(dep)" in query
    assert "123" in section


def test_suggested_spring_query_orders_by_a_grouping_key():
    section = report_generator._section_suggested_queries()
    queries = re.findall(r"```cypher\n(.*?)\n```", section, re.DOTALL)
    spring_query = next(query for query in queries if "INJECTS" in query)

    assert re.search(
        r"RETURN\s+bean\.name(?:\s+AS\s+bean_name)?\s*,\s*"
        r"count\(\*\)\s+AS\s+injection_count",
        spring_query,
        re.IGNORECASE,
    )
    assert re.search(
        r"ORDER BY\s+injection_count DESC,\s*(?:bean\.name|bean_name)\s+LIMIT 10",
        spring_query,
        re.IGNORECASE,
    )


def test_external_library_query_is_a_bounded_sample():
    section = report_generator._section_suggested_queries()
    examples = re.findall(r"### ([^\n]+)\n```cypher\n(.*?)\n```", section, re.DOTALL)
    title, query = next((title, query) for title, query in examples if "ExternalLibrary" in query)

    assert "sample" in title.lower()
    assert re.search(r"\bLIMIT\s+100\b", query, re.IGNORECASE)


def test_decorator_lookup_returns_a_bounded_page_with_retrieval_metadata():
    calls = []

    class _Rows:
        def data(self):
            return [{
                "function_name": "route",
                "path": "/work/repo/routes.py",
                "line_number": 7,
                "decorators": ["@decorator-preview"],
                "decorators_value_total_chars": [300],
                "decorators_total_count": 40,
                "decorators_truncated": True,
                "decorators_offset": 16,
                "decorators_page_size": 16,
                "decorators_next_offset": 32,
                "decorators_value_offset": 128,
                "decorators_value_page_size": 128,
                "decorators_value_next_offset": 256,
                "decorators_value_continuation_limit_reached": False,
                "_sort_uid": "function-uid",
            }]

    class _Session:
        def run(self, query, **params):
            calls.append((query, params))
            return _Rows()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, *_args):
            return _Driver()

    finder = CodeFinder(_Manager())
    rows = finder.find_functions_by_decorator(
        "route", repo_path="/work/repo", limit=2, offset=3,
        decorator_offset=16, decorator_page_size=16,
        decorator_value_offset=128, decorator_value_page_size=128,
    )

    query, params = calls[0]
    normalized = " ".join(query.lower().split())
    assert "f.decorators as decorators" not in normalized
    assert "f.decorators[16..32]" in normalized
    assert "substring(_cgc_decorator_preview, 128, 128)" in normalized
    assert "decorators_total_count" in normalized
    assert "decorators_truncated" in normalized
    assert "decorators_next_offset" in normalized
    assert "decorators_value_total_chars" in normalized
    assert "decorators_value_next_offset" in normalized
    assert params["offset"] == 3
    assert params["limit"] == 2
    assert rows[0]["decorators"] == ["@decorator-preview"]
    assert rows[0]["decorators_total_count"] == 40
    assert rows[0]["decorators_truncated"] is True
    assert rows[0]["decorators_offset"] == 16
    assert rows[0]["decorators_page_size"] == 16
    assert rows[0]["decorators_next_offset"] == 32
    assert rows[0]["decorators_value_total_chars"] == [300]
    assert rows[0]["decorators_value_offset"] == 128
    assert rows[0]["decorators_value_page_size"] == 128
    assert rows[0]["decorators_value_next_offset"] == 256


def test_decorator_page_arguments_are_exposed_and_forwarded():
    properties = TOOLS["analyze_code_relationships"]["inputSchema"]["properties"]
    assert properties["decorator_offset"]["maximum"] == 99999
    assert properties["decorator_page_size"]["maximum"] == 16
    assert properties["decorator_value_offset"]["maximum"] == 99999
    assert properties["decorator_value_page_size"]["maximum"] == 128

    received = {}

    class _Finder:
        def analyze_code_relationships(self, *args, **kwargs):
            received["args"] = args
            received["kwargs"] = kwargs
            return {"decorators_next_offset": 24}

    response = analysis_handlers.analyze_code_relationships(
        _Finder(), query_type="find_functions_by_decorator", target="route",
        decorator_offset=16, decorator_page_size=8,
        decorator_value_offset=128, decorator_value_page_size=64,
    )

    assert received["kwargs"]["decorator_offset"] == 16
    assert received["kwargs"]["decorator_page_size"] == 8
    assert received["kwargs"]["decorator_value_offset"] == 128
    assert received["kwargs"]["decorator_value_page_size"] == 64
    assert response["success"] is True


@pytest.mark.parametrize(
    "method_name, call_args",
    [
        ("who_imports_module", ("module",)),
        ("find_functions_by_argument", ("argument",)),
        ("find_functions_by_decorator", ("decorator",)),
        ("who_modifies_variable", ("variable",)),
        ("find_function_overrides", ("method",)),
        ("find_function_call_chain", ("start", "end")),
    ],
)
def test_relationship_query_methods_use_database_offset_windows(
    method_name, call_args
):
    calls = []

    class _Rows:
        def data(self):
            return []

    class _Session:
        def run(self, query, **params):
            calls.append((query, params))
            return _Rows()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, *_args):
            return _Driver()

    finder = CodeFinder(_Manager())
    getattr(finder, method_name)(*call_args, limit=3, offset=4)

    query, params = calls[0]
    assert "SKIP $offset" in query
    assert "LIMIT $limit" in query
    assert "ORDER BY" in query
    assert params["offset"] == 4
    assert params["limit"] == 3


def test_class_hierarchy_uses_bounded_stable_pages_for_each_collection(
    monkeypatch,
):
    from codegraphcontext.tools import code_finder as code_finder_module

    monkeypatch.setattr(code_finder_module, "get_tool_result_limit", lambda *_a, **_k: None)
    calls = []

    class _Rows:
        def __init__(self, query):
            if "parent.name as parent_class" in query:
                self.rows = [
                    {"parent_class": f"Parent{index}", "_sort_uid": f"p{index}"}
                    for index in range(3)
                ]
            elif "grandchild.name as child_class" in query:
                self.rows = [
                    {"child_class": f"Child{index}", "_sort_uid": f"c{index}"}
                    for index in range(3)
                ]
            else:
                self.rows = [
                    {"method_name": f"method{index}", "_sort_uid": f"m{index}"}
                    for index in range(3)
                ]

        def data(self):
            return self.rows

    class _Session:
        def run(self, query, **params):
            calls.append((query, params))
            return _Rows(query)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, *_args):
            return _Driver()

    finder = CodeFinder(_Manager())
    response = finder.analyze_code_relationships(
        "class_hierarchy", "Child", offset=4, page_size=2
    )

    assert len(calls) == 3
    for query, params in calls:
        assert "SKIP $offset" in query and "LIMIT $limit" in query
        assert "ORDER BY" in query and "_sort_uid" in query
        assert "$preview_char_limit" in query
        assert "parent.docstring as parent_docstring" not in query.lower()
        assert "grandchild.docstring as child_docstring" not in query.lower()
        assert "method.docstring as method_docstring" not in query.lower()
        assert params["offset"] == 4
        assert params["limit"] == 3
        if "method.name as method_name" in query.lower():
            assert "method.args as method_args" not in query.lower()
            assert "method.args[0..16]" in query.lower()
            assert "method_args_total_count" in query.lower()
            assert "method_args_truncated" in query.lower()
    results = response["results"]
    assert [row["parent_class"] for row in results["parent_classes"]] == [
        "Parent0", "Parent1"
    ]
    assert [row["child_class"] for row in results["child_classes"]] == [
        "Child0", "Child1"
    ]
    assert [row["method_name"] for row in results["methods"]] == [
        "method0", "method1"
    ]
    assert results["parent_classes_next_offset"] == 6
    assert results["child_classes_next_offset"] == 6
    assert results["methods_next_offset"] == 6
    assert response["truncated"] is True
    assert response["next_offset"] == 6


@pytest.mark.parametrize(
    "method_name, args, bounded_projection, raw_projection, path",
    [
        (
            "find_functions_by_argument",
            ("argument",),
            "substring(f.docstring, $preview_start, $preview_char_limit)",
            "f.docstring AS docstring",
            None,
        ),
        (
            "find_functions_by_decorator",
            ("decorator",),
            "substring(f.docstring, $preview_start, $preview_char_limit)",
            "f.docstring AS docstring",
            None,
        ),
        (
            "find_functions_by_decorator",
            ("decorator",),
            "substring(f.docstring, $preview_start, $preview_char_limit)",
            "f.docstring AS docstring",
            "/workspace/repo/decorated.py",
        ),
        (
            "who_modifies_variable",
            ("variable",),
            "substring(var.value, $preview_start, $preview_char_limit)",
            "var.value as variable_value",
            None,
        ),
        (
            "find_function_overrides",
            ("method",),
            "substring(func.docstring, $preview_start, $preview_char_limit)",
            "func.docstring as function_docstring",
            None,
        ),
    ],
)
def test_relationship_rows_project_bounded_evidence_previews(
    method_name, args, bounded_projection, raw_projection, path
):
    queries = []

    class _Rows:
        def data(self):
            return []

    class _Session:
        def run(self, query, **_params):
            queries.append(query)
            return _Rows()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, *_args):
            return _Driver()

    finder = CodeFinder(_Manager())
    kwargs = {"limit": 1, "offset": 0}
    if path:
        kwargs["path"] = path
    getattr(finder, method_name)(*args, **kwargs)

    query = queries[0]
    assert bounded_projection in query
    assert raw_projection.lower() not in query.lower()
    if method_name == "who_modifies_variable":
        assert "substring(var.context, $preview_start, $preview_char_limit)" in query
        assert "var.context as variable_context" not in query.lower()
    if method_name == "find_function_overrides":
        assert "func.args as function_args" not in query.lower()
        assert "func.args[0..16]" in query.lower()
        assert "function_args_total_count" in query.lower()
        assert "function_args_truncated" in query.lower()
    assert "$preview_char_limit" in query


def test_find_complexity_analyzer_bounds_numeric_limit_and_returns_page_metadata(
    monkeypatch,
):
    from codegraphcontext.tools import code_finder as code_finder_module

    monkeypatch.setattr(code_finder_module, "get_tool_result_limit", lambda *_a, **_k: None)
    calls = []
    rows = [{"function_name": f"complex_{index}"} for index in range(3)]

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

    finder = CodeFinder(_Manager())

    def fake_find_most_complex(limit, repo_path=None, graph_name=None, offset=0):
        calls.append((limit, repo_path, graph_name, offset))
        return rows

    monkeypatch.setattr(finder, "find_most_complex_functions", fake_find_most_complex)
    response = finder.analyze_code_relationships(
        "find_complexity", "1000000000", offset=4, page_size=2
    )

    assert calls == [(3, None, None, 4)]
    assert response["limit"] == 100_000
    assert response["results"] == rows[:2]
    assert response["truncated"] is True
    assert response["offset"] == 4
    assert response["page_size"] == 2
    assert response["next_offset"] == 6


def test_find_most_complex_functions_applies_database_offset_and_limit_window():
    calls = []

    class _Rows:
        def data(self):
            return []

    class _Session:
        def run(self, query, **params):
            calls.append((query, params))
            return _Rows()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, *_args):
            return _Driver()

    CodeFinder(_Manager()).find_most_complex_functions(
        limit=3, offset=4
    )

    query, params = calls[0]
    assert "SKIP $offset" in query
    assert "LIMIT $limit" in query
    assert "ORDER BY" in query and "f.uid" in query
    assert params["offset"] == 4
    assert params["limit"] == 3


def test_module_dependency_and_variable_scope_queries_bound_database_rows():
    calls = []

    class _Rows:
        def data(self):
            return []

    class _Session:
        def run(self, query, **params):
            calls.append((query, params))
            return _Rows()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, *_args):
            return _Driver()

    finder = CodeFinder(_Manager())
    finder.find_module_dependencies("target", offset=4, page_size=2)
    module_queries = list(calls)
    calls.clear()
    finder.find_variable_usage_scope("target", offset=4, page_size=2)
    variable_queries = list(calls)

    assert len(module_queries) == 2
    assert all("SKIP $offset" in query and "LIMIT $limit" in query for query, _ in module_queries)
    assert all(params["offset"] == 4 and params["limit"] == 3 for _, params in module_queries)
    assert len(variable_queries) == 2
    assert all("LIMIT $candidate_limit" in query for query, _ in variable_queries)
    assert all(params["candidate_limit"] == 7 for _, params in variable_queries)


@pytest.mark.parametrize("orphaned", [False, True])
def test_variable_scope_evidence_returns_bounded_value_and_context_pages(orphaned):
    value = "值" * 5_000
    context = "变量上下文" * 2_000
    calls = []

    class _Rows:
        def __init__(self, row):
            self.row = row

        def data(self):
            return [dict(self.row)] if self.row is not None else []

    class _Session:
        def run(self, query, **params):
            calls.append((query, params))
            is_orphan_query = "AND NOT ()-[:CONTAINS]->(var)" in query
            include_row = is_orphan_query is orphaned
            if not include_row:
                return _Rows(None)
            value_offset = params.get("value_offset", 0)
            context_offset = params.get("context_offset", 0)
            char_limit = params.get("evidence_char_limit", 1_024)
            return _Rows({
                "variable_name": "target",
                "variable_value": value[value_offset:value_offset + char_limit],
                "_variable_value_total_chars": len(value),
                "context": context[context_offset:context_offset + char_limit],
                "_context_total_chars": len(context),
                "line_number": 7,
                "path": "/repo/target.py",
                "scope_type": "module",
                "scope_name": "module_level",
                "is_dependency": False,
                "_sort_uid": "target-uid",
            })

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, *_args):
            return _Driver()

    result = CodeFinder(_Manager()).find_variable_usage_scope(
        "target",
        offset=0,
        page_size=1,
        value_offset=1_200,
        context_offset=400,
        evidence_char_limit=128,
    )

    instance = result["instances"][0]
    assert instance["variable_value"] == value[1_200:1_328]
    assert instance["variable_value_offset"] == 1_200
    assert instance["variable_value_next_offset"] == 1_328
    assert instance["variable_value_total_chars"] == len(value)
    assert instance["variable_value_truncated"] is True
    assert instance["context"] == context[400:528]
    assert instance["context_offset"] == 400
    assert instance["context_next_offset"] == 528
    assert instance["context_total_chars"] == len(context)
    assert instance["context_truncated"] is True
    assert len(calls) == 2
    assert all("substring(var.value" in query for query, _ in calls)
    assert all("substring(var.context" in query for query, _ in calls)
    assert all("var.value as variable_value" not in query for query, _ in calls)
    assert all("var.context as context" not in query for query, _ in calls)
    assert all(params["evidence_char_limit"] == 128 for _, params in calls)


@pytest.mark.parametrize("with_path", [False, True])
def test_callee_docstrings_are_bounded_and_pageable(with_path):
    docstring = "函数说明😀" * 2_000
    calls = []

    class _Rows:
        def data(self):
            query, params = calls[-1]
            offset = params.get("docstring_offset", 0)
            limit = params.get("docstring_char_limit", len(docstring))
            if "substring(called.docstring" in query:
                page = docstring[offset:offset + limit]
                total = len(docstring)
            else:
                page = docstring
                total = None
            return [{
                "_caller_uid": "caller-uid",
                "_target_uid": "callee-uid",
                "_edge_type": "CALLS",
                "_call_line": 12,
                "_call_full_name": "callee()",
                "_call_args_key": "[]",
                "called_function": "callee",
                "called_file_path": "/repo/callee.py",
                "called_line_number": 4,
                "called_docstring": page,
                "_called_docstring_total_chars": total,
                "called_is_dependency": False,
                "call_line_number": 12,
                "call_args": "[]",
                "full_call_name": "callee()",
            }]

    class _Session:
        def run(self, query, **params):
            calls.append((query, params))
            return _Rows()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, *_args):
            return _Driver()

    result = CodeFinder(_Manager()).what_does_function_call(
        "caller",
        path="/repo/caller.py" if with_path else None,
        docstring_offset=1_500,
        docstring_char_limit=128,
    )

    item = result[0]
    assert item["called_docstring"] == docstring[1_500:1_628]
    assert item["called_docstring_offset"] == 1_500
    assert item["called_docstring_next_offset"] == 1_628
    assert item["called_docstring_total_chars"] == len(docstring)
    assert item["called_docstring_truncated"] is True
    assert len(calls) == 1
    query, params = calls[0]
    assert "substring(called.docstring" in query
    assert "called.docstring as called_docstring" not in query
    assert params["docstring_char_limit"] == 128
    assert params["docstring_start"] == 1_500


def test_relationship_handler_forwards_evidence_page_inputs():
    captured = {}

    class _Finder:
        def analyze_code_relationships(self, *args, **kwargs):
            captured.update(kwargs)
            return {"results": [], "truncated": False}

    analysis_handlers.analyze_code_relationships(
        _Finder(),
        query_type="find_callees",
        target="caller",
        docstring_offset=100,
        docstring_char_limit=200,
        value_offset=300,
        context_offset=400,
        evidence_char_limit=500,
    )

    assert captured["docstring_offset"] == 100
    assert captured["docstring_char_limit"] == 200
    assert captured["value_offset"] == 300
    assert captured["context_offset"] == 400
    assert captured["evidence_char_limit"] == 500


@pytest.mark.parametrize(
    "query_type, method_name, target",
    [
        ("find_importers", "who_imports_module", "module"),
        ("find_functions_by_argument", "find_functions_by_argument", "argument"),
        ("find_functions_by_decorator", "find_functions_by_decorator", "decorator"),
        ("who_modifies", "who_modifies_variable", "variable"),
        ("overrides", "find_function_overrides", "method"),
        ("call_chain", "find_function_call_chain", "start->end"),
    ],
)
def test_relationship_analyzer_list_queries_return_continuable_pages(
    monkeypatch, query_type, method_name, target
):
    from codegraphcontext.tools import code_finder as code_finder_module

    monkeypatch.setattr(code_finder_module, "get_tool_result_limit", lambda *_a, **_k: None)
    rows = [{"row": index} for index in range(3)]
    calls = []

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

    finder = CodeFinder(_Manager())

    def page_query(*_args, **kwargs):
        calls.append(kwargs)
        return rows

    monkeypatch.setattr(finder, method_name, page_query)
    response = finder.analyze_code_relationships(
        query_type, target, offset=4, page_size=2
    )

    assert calls[0]["offset"] == 4
    assert calls[0]["limit"] == 3
    assert response["truncated"] is True
    assert response["offset"] == 4
    assert response["page_size"] == 2
    assert response["next_offset"] == 6
    assert response["results"] == rows[:2]


def test_dead_code_relationship_analyzer_uses_the_requested_bounded_page(monkeypatch):
    calls = []
    rows = [{"function_name": f"unused_{index}"} for index in range(3)]

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

    finder = CodeFinder(_Manager())

    def fake_find_dead_code(**kwargs):
        calls.append(kwargs)
        return {
            "potentially_unused_functions": rows,
            "total_count": 10,
            "confidence_counts": {"high": 10, "medium": 0, "low": 0},
        }

    monkeypatch.setattr(finder, "find_dead_code", fake_find_dead_code)
    response = finder.analyze_code_relationships(
        "dead_code", "unused", offset=4, page_size=2
    )

    assert calls[0]["offset"] == 4
    assert calls[0]["limit"] == 3
    assert response["truncated"] is True
    assert response["offset"] == 4
    assert response["next_offset"] == 6
    assert response["results"]["potentially_unused_functions"] == rows[:2]


@pytest.mark.parametrize(
    ("total_count", "raw_next_offset"),
    [(7, None), (10, 7)],
)
def test_dead_code_relationship_nested_paging_matches_the_trimmed_page(
    monkeypatch, total_count, raw_next_offset
):
    rows = [{"function_name": f"unused_{index}"} for index in range(3)]

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

    finder = CodeFinder(_Manager())

    def fake_find_dead_code(**kwargs):
        return {
            "potentially_unused_functions": rows,
            "total_count": total_count,
            "confidence_counts": {"high": total_count, "medium": 0, "low": 0},
            "offset": kwargs["offset"],
            "page_size": kwargs["limit"],
            "result_limit": kwargs["limit"],
            "truncated": raw_next_offset is not None,
            "next_offset": raw_next_offset,
            "continuation_limit_reached": False,
        }

    monkeypatch.setattr(finder, "find_dead_code", fake_find_dead_code)
    response = finder.analyze_code_relationships(
        "dead_code", "unused", offset=4, page_size=2
    )

    nested = response["results"]
    assert nested["potentially_unused_functions"] == rows[:2]
    assert nested["offset"] == 4
    assert nested["page_size"] == 2
    assert nested["result_limit"] == 2
    assert nested["truncated"] is True
    assert nested["next_offset"] == 6
    assert nested["continuation_limit_reached"] is False


@pytest.mark.parametrize(
    "query_type, method_name, collection_key, payload",
    [
        (
            "module_deps",
            "find_module_dependencies",
            "importers",
            {"module_name": "target", "importers": [{"row": i} for i in range(3)], "imports": []},
        ),
        (
            "variable_scope",
            "find_variable_usage_scope",
            "instances",
            {"variable_name": "target", "instances": [{"row": i} for i in range(3)]},
        ),
    ],
)
def test_module_and_variable_analyzer_branches_pass_pages_and_return_cursors(
    monkeypatch, query_type, method_name, collection_key, payload
):
    from codegraphcontext.tools import code_finder as code_finder_module

    monkeypatch.setattr(code_finder_module, "get_tool_result_limit", lambda *_a, **_k: None)
    calls = []

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

    finder = CodeFinder(_Manager())

    def fake_query(*_args, **kwargs):
        calls.append(kwargs)
        result = dict(payload)
        rows = result[collection_key]
        has_more = len(rows) > kwargs["page_size"]
        result[collection_key] = rows[:kwargs["page_size"]]
        result.update(
            offset=kwargs["offset"],
            page_size=kwargs["page_size"],
            result_limit=kwargs["page_size"],
            truncated=has_more,
            next_offset=(kwargs["offset"] + kwargs["page_size"] if has_more else None),
            continuation_limit_reached=False,
        )
        return result

    monkeypatch.setattr(finder, method_name, fake_query)
    response = finder.analyze_code_relationships(
        query_type, "target", offset=4, page_size=2
    )

    assert calls[0]["offset"] == 4
    assert calls[0]["page_size"] == 2
    assert response["truncated"] is True
    assert response["offset"] == 4
    assert response["page_size"] == 2
    assert response["next_offset"] == 6
    assert len(response["results"][collection_key]) == 2


def test_diagram_export_applies_database_paging_and_preserves_total_count():
    calls = []
    edge_rows = [
        {"src": f"caller_{index}", "dst": "target"}
        for index in range(3)
    ]

    class _Rows:
        def __init__(self, rows):
            self.rows = rows

        def data(self):
            return self.rows

    class _Session:
        def run(self, query, **params):
            calls.append((query, params))
            if "COUNT" in query.upper():
                return _Rows([{"total_count": 10}])
            return _Rows(edge_rows)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Driver:
        def session(self):
            return _Session()

    class _Manager:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self, *_args):
            return _Driver()

    result = CodeFinder(_Manager()).export_diagram_edges(
        level="call", repo_path="/work/repo", limit=2, offset=2
    )

    page_query, params = next((q, p) for q, p in calls if "COUNT" not in q.upper())
    assert "SKIP $offset LIMIT $candidate_limit" in page_query
    assert params["offset"] == 2
    assert params["candidate_limit"] == 3
    assert result["edges"] == [("caller_0", "target"), ("caller_1", "target")]
    assert result["total_count"] == 10
    assert result["truncated"] is True
    assert result["next_offset"] == 4
