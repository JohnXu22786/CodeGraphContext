"""Behavior-level MCP coverage for lifecycle, bundle, registry, and simulator tools."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from codegraphcontext.cli import config_manager
from codegraphcontext.core.bundle_registry import BundleRegistry, RegistryUnavailableError
from codegraphcontext.core.cgc_bundle import CGCBundle
from codegraphcontext.core.database_kuzu import KuzuDBManager
from codegraphcontext.core.jobs import JobManager
from codegraphcontext.core.simulator import CodeGraphTwin
from codegraphcontext.server import MCPServer
from codegraphcontext.tools.graph_builder import GraphBuilder


kuzu = pytest.importorskip("kuzu")


@pytest.fixture
def mcp_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build the real MCP dispatcher over a Kùzu database inside this test's workspace."""
    workspace = tmp_path / "workspace"
    repo = workspace / "sample-repo"
    repo.mkdir(parents=True)

    cgc_home = tmp_path / "cgc-home"
    cgc_home.mkdir()
    repo_context = repo / ".codegraphcontext"
    (repo_context / "db").mkdir(parents=True)
    (repo / ".cgcignore").write_text(".codegraphcontext/\n", encoding="utf-8")
    (repo_context / "config.yaml").write_text(
        json.dumps({"database": "kuzudb"}), encoding="utf-8"
    )

    monkeypatch.setenv("CGC_HOME", str(cgc_home))
    monkeypatch.setenv("CGC_REPO_CONTEXT_DIR", repo_context.name)
    monkeypatch.setenv("CGC_ALLOWED_ROOTS", str(workspace))
    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "kuzudb")
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    monkeypatch.setenv("CGC_EMBEDDED_BUFFER_POOL_MB", "128")
    for name in ("CGC_RUNTIME_DB_PATH", "CGC_ALLOW_ALL_PATHS", "KUZUDB_PATH"):
        monkeypatch.delenv(name, raising=False)

    # The config module captures CGC_HOME at import time; redirect its file paths
    # as well so server initialization cannot inspect or write user configuration.
    monkeypatch.setattr(config_manager, "CONFIG_DIR", cgc_home)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", cgc_home / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", cgc_home / "config.yaml")
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", cgc_home / "cgc_config.yaml"
    )
    monkeypatch.setattr(
        config_manager, "_LEGACY_FALKORDB_PATH", cgc_home / "global" / "falkordb.db"
    )
    config_manager.CONTEXT_CONFIG_FILE.write_text(
        json.dumps({"version": 1, "mode": "per-repo", "contexts": {}}),
        encoding="utf-8",
    )

    server = MCPServer(cwd=repo)
    assert Path(server.resolved_context.db_path).is_relative_to(workspace)
    try:
        yield server, workspace, repo
    finally:
        server.code_watcher.stop()
        server.db_manager.close_driver()
        if not server.loop.is_closed():
            server.loop.close()


def _call(server: MCPServer, tool: str, arguments: dict):
    """Exercise the server's actual MCP dispatcher path."""
    return asyncio.run(server.handle_tool_call(tool, arguments))


def _write_repo_source(repo: Path) -> Path:
    source_dir = repo / "src"
    source_dir.mkdir(exist_ok=True)
    source = source_dir / "service.py"
    source.write_text(
        "def leaf(value):\n"
        "    return value + 1\n\n"
        "def caller(value):\n"
        "    return leaf(value)\n",
        encoding="utf-8",
    )
    return source


def _index_repo(server: MCPServer, repo: Path) -> None:
    if not (repo / "src" / "service.py").exists():
        _write_repo_source(repo)
    server.loop.run_until_complete(
        server.graph_builder.build_graph_from_path_async(repo, is_dependency=False)
    )


@pytest.mark.integration
def test_watcher_lifecycle_tools_dispatch_real_watcher_and_errors(mcp_server, monkeypatch):
    """Watcher tools must update real watcher state and return their validation/service errors."""
    server, _, repo = mcp_server
    monkeypatch.setattr(
        server, "list_indexed_repositories_tool", lambda **_: {"repositories": []}
    )
    monkeypatch.setattr(
        server, "add_code_to_graph_tool", lambda **_: {"job_id": "test-index-job"}
    )

    watched = _call(server, "watch_directory", {"path": str(repo)})
    assert watched["success"] is True
    assert str(repo) in watched["message"]
    server.code_watcher.start()
    assert server.code_watcher.observer.is_alive()
    assert _call(server, "list_watched_paths", {}) == {
        "success": True,
        "watched_paths": [str(repo.resolve())],
    }

    missing = _call(server, "watch_directory", {"path": str(repo / "missing")})
    assert missing["success"] is False
    assert missing["status"] == "path_not_found"

    stopped = _call(server, "unwatch_directory", {"path": str(repo)})
    assert "Stopped watching" in stopped["message"]
    assert _call(server, "list_watched_paths", {})["watched_paths"] == []
    assert _call(server, "unwatch_directory", {}) == {
        "error": "Path is a required argument."
    }

    def failed_list():
        raise RuntimeError("watcher state unavailable")

    monkeypatch.setattr(server.code_watcher, "list_watched_paths", failed_list)
    list_error = _call(server, "list_watched_paths", {})
    assert list_error["error"] == "Failed to list watched paths: watcher state unavailable"


@pytest.mark.integration
def test_load_bundle_imports_generated_bundle_and_rejects_unsafe_paths(mcp_server, tmp_path):
    """Bundle loading must import a generated graph snapshot only into task-owned storage."""
    server, workspace, repo = mcp_server
    source_manager = KuzuDBManager(db_path=str(tmp_path / "bundle-source-db"))
    source_loop = asyncio.new_event_loop()
    source_builder = GraphBuilder(source_manager, JobManager(), source_loop)
    bundle_path = workspace / "generated.cgc"
    target_db_path = server.resolved_context.db_path
    try:
        _write_repo_source(repo)
        source_loop.run_until_complete(
            source_builder.build_graph_from_path_async(repo, is_dependency=False)
        )
        exported, export_message = CGCBundle(source_manager).export_to_bundle(
            bundle_path, repo_path=repo
        )
        assert exported, export_message
    finally:
        source_manager.close_driver()
        source_loop.close()
        # Kùzu managers are a process singleton, so reopen the MCP server's
        # distinct task database after exporting from the source database.
        KuzuDBManager(db_path=target_db_path).get_driver()

    imported = _call(server, "load_bundle", {"bundle_name": str(bundle_path)})
    assert imported.get("success") is True, imported
    assert imported["stats"]["nodes"] > 0
    with server.db_manager.get_driver().session() as session:
        function_names = {
            row["name"] for row in session.run("MATCH (f:Function) RETURN f.name AS name").data()
        }
    assert {"leaf", "caller"} <= function_names

    assert _call(server, "load_bundle", {}) == {"error": "bundle_name is required"}
    outside = tmp_path / "outside.cgc"
    rejected = _call(server, "load_bundle", {"bundle_name": str(outside)})
    assert "outside allowed roots" in rejected["error"]


@pytest.mark.integration
def test_registry_search_dispatch_filters_read_only_mock_and_reports_unavailable(
    mcp_server, monkeypatch
):
    """Registry search must filter manifest-shaped records and surface unavailable service errors."""
    server, _, _ = mcp_server
    records = [
        {
            "name": "python-tools",
            "repo": "example/python-tools",
            "full_name": "python-tools-v2",
            "bundle_name": "python-tools-v2.cgc",
            "generated_at": "2026-09-01T00:00:00Z",
            "download_url": "https://huggingface.co/example/python-tools-v2.cgc",
            "source": "on-demand",
        },
        {
            "name": "rust-tools",
            "repo": "example/rust-tools",
            "full_name": "rust-tools-v1",
            "bundle_name": "rust-tools-v1.cgc",
            "generated_at": "2026-09-02T00:00:00Z",
            "download_url": "https://huggingface.co/example/rust-tools-v1.cgc",
            "source": "on-demand",
        },
    ]
    monkeypatch.setattr(BundleRegistry, "fetch_available_bundles", staticmethod(lambda: records))

    result = _call(
        server,
        "search_registry_bundles",
        {"query": "python", "unique_only": True},
    )
    assert result["success"] is True
    assert result["total"] == 1
    assert result["bundles"][0]["full_name"] == "python-tools-v2"
    assert result["query"] == "python"

    def registry_offline():
        raise RegistryUnavailableError("test network unavailable")

    monkeypatch.setattr(BundleRegistry, "fetch_available_bundles", staticmethod(registry_offline))
    unavailable = _call(server, "search_registry_bundles", {"query": "python"})
    assert unavailable["success"] is False
    assert "test network unavailable" in unavailable["error"]


@pytest.mark.integration
def test_simulate_metrics_dispatch_uses_indexed_graph_and_catches_database_error(
    mcp_server, monkeypatch
):
    """Metrics MCP dispatch must read the task-indexed graph and translate database failures."""
    server, _, repo = mcp_server
    _index_repo(server, repo)

    result = _call(server, "simulate_metrics", {"repo_path": str(repo)})
    assert result["status"] == "ok"
    assert result["metrics"]["nodes_count"] >= 2
    assert {
        "maintainability_score",
        "nodes_count",
        "edges_count",
        "coupling",
        "cohesion",
    } <= result["metrics"].keys()

    with monkeypatch.context() as patcher:
        def database_unavailable(*_args, **_kwargs):
            raise RuntimeError("test database unavailable")

        patcher.setattr(server.db_manager, "get_driver", database_unavailable)
        failed = _call(server, "simulate_metrics", {"repo_path": str(repo)})
    assert "test database unavailable" in failed["error"]


@pytest.mark.integration
def test_simulate_architectural_change_dispatch_reports_graph_delta_and_error(
    mcp_server
):
    """Architecture simulation MCP dispatch must apply mutations to a loaded test graph."""
    server, _, repo = mcp_server
    _index_repo(server, repo)
    twin = CodeGraphTwin(str(repo)).load_from_db(server.db_manager)
    function_id = next(
        node_id
        for node_id, node in twin.nodes.items()
        if node.get("label") == "Function"
    )

    result = _call(
        server,
        "simulate_architectural_change",
        {
            "repo_path": str(repo),
            "changes": [{"type": "remove_node", "node_id": function_id}],
        },
    )
    assert result["status"] == "ok"
    assert result["comparison"]["nodes_count"]["delta"] == -1
    assert result["simulated_metrics"]["nodes_count"] == result["baseline_metrics"]["nodes_count"] - 1

    malformed = _call(
        server,
        "simulate_architectural_change",
        {"repo_path": str(repo), "changes": [None]},
    )
    assert "Failed to simulate architectural change" in malformed["error"]


@pytest.mark.integration
def test_architectural_evolution_dispatch_combines_git_history_and_indexed_graph(
    mcp_server, monkeypatch
):
    """Evolution MCP dispatch must report churn-backed hotspots for task-owned graph data."""
    server, _, repo = mcp_server
    source = _write_repo_source(repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test Harness"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", "/dev/null"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "commit.gpgSign", "false"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "src/service.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\ndef newer(value):\n    return caller(value)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "src/service.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "add newer"], check=True)
    _index_repo(server, repo)

    result = _call(
        server,
        "analyze_architectural_evolution",
        {"repo_path": str(repo), "commits": 2},
    )
    assert result["status"] == "ok"
    assert result["hotspots"]
    assert result["hotspots"][0]["relative_path"] == "src/service.py"
    assert result["hotspots"][0]["churn"] == 2
    assert len(result["growth_trend"]) == 2

    with monkeypatch.context() as patcher:
        def database_unavailable(*_args, **_kwargs):
            raise RuntimeError("test evolution database unavailable")

        patcher.setattr(server.db_manager, "get_driver", database_unavailable)
        failed = _call(
            server,
            "analyze_architectural_evolution",
            {"repo_path": str(repo), "commits": 2},
        )
    assert "test evolution database unavailable" in failed["error"]
