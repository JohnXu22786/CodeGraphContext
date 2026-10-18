"""Behavior-level MCP coverage for indexing and management tools in F3E."""

import asyncio
import time
from pathlib import Path

import pytest

from codegraphcontext import server as server_module
from codegraphcontext.cli import config_manager


def _isolated_workspace(tmp_path, monkeypatch):
    """Set every runtime/config/database path inside this test's temp tree."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cgc_home = tmp_path / "cgc-home"
    cgc_home.mkdir()

    monkeypatch.setenv("CGC_HOME", str(cgc_home))
    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "kuzudb")
    monkeypatch.setenv("CGC_RUNTIME_DB_PATH", str(cgc_home / "global-db"))
    monkeypatch.setenv("CGC_REPO_CONTEXT_DIR", ".codegraphcontext")
    monkeypatch.setenv("CGC_ALLOWED_ROOTS", str(workspace))
    monkeypatch.setenv("CGC_IGNORE_PROJECT_ENV", "1")
    monkeypatch.setenv("CGC_LOAD_PROJECT_ENV", "0")

    # config_manager captures these paths when imported, before pytest sets the
    # per-test environment. Point its config reads/writes at this task-owned
    # tree so constructing or switching a server cannot touch the user's home.
    monkeypatch.setattr(config_manager, "CONFIG_DIR", cgc_home)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", cgc_home / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", cgc_home / "config.yaml")
    monkeypatch.setattr(config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", cgc_home / "cgc_config.yaml")
    monkeypatch.setattr(config_manager, "_LEGACY_FALKORDB_PATH", cgc_home / "global" / "falkordb.db")
    monkeypatch.setattr(server_module, "CONFIG_DIR", cgc_home)
    monkeypatch.setattr(
        config_manager,
        "load_context_config",
        lambda: config_manager.ContextConfig(mode="per-repo"),
    )

    server_root = workspace / "server-root"
    server_root.mkdir()
    _write_repo_context(server_root)
    server = server_module.MCPServer(cwd=server_root)
    assert server.resolved_context.is_local
    assert Path(server.resolved_context.db_path).is_relative_to(workspace)
    return workspace, server


def _write_repo_context(repo_root):
    context = repo_root / ".codegraphcontext"
    context.mkdir(parents=True, exist_ok=True)
    (context / "config.yaml").write_text("database: kuzudb\n", encoding="utf-8")


def _write_python_project(project_root, function_name="greet"):
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "app.py").write_text(
        f"def {function_name}(name: str = 'world') -> str:\n"
        "    return f'hello {name}'\n",
        encoding="utf-8",
    )


async def _wait_for_job(server, job_id):
    """Synchronize with the real background job without asserting a duration."""
    deadline = time.monotonic() + 60
    last_result = None
    while time.monotonic() < deadline:
        last_result = await server.handle_tool_call("check_job_status", {"job_id": job_id})
        job = last_result.get("job") or {}
        if job.get("status") in {"completed", "failed", "cancelled"}:
            return last_result
        await asyncio.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not finish; last MCP result: {last_result!r}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_indexing_and_job_tools_use_real_mcp_behavior(tmp_path, monkeypatch):
    """The MCP dispatcher must safely index code/packages and expose their jobs and records."""
    workspace, server = _isolated_workspace(tmp_path, monkeypatch)
    project = workspace / "projects" / "primary"
    _write_python_project(project)
    try:
        missing_path = await server.handle_tool_call("add_code_to_graph", {})
        assert "error" in missing_path and "required" in missing_path["error"]

        indexed = await server.handle_tool_call("add_code_to_graph", {"repo_path": str(project)})
        assert indexed["success"] is True, indexed
        code_job = await _wait_for_job(server, indexed["job_id"])
        assert code_job["success"] is True, code_job
        assert code_job["job"]["status"] == "completed", code_job

        package_name = "f3e_fixture_package"
        package_root = workspace / "packages"
        package_dir = package_root / package_name
        package_dir.mkdir(parents=True)
        (package_dir / "__init__.py").write_text(
            "def package_entry(value: int = 1) -> int:\n    return value + 1\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(package_root))

        missing_language = await server.handle_tool_call(
            "add_package_to_graph", {"package_name": package_name}
        )
        assert "error" in missing_language and "language" in missing_language["error"]

        package = await server.handle_tool_call(
            "add_package_to_graph",
            {"package_name": package_name, "language": "python"},
        )
        assert package["success"] is True, package
        assert Path(package["discovered_path"]).resolve() == package_dir.resolve()
        package_job = await _wait_for_job(server, package["job_id"])
        assert package_job["job"]["status"] == "completed", package_job
        assert package_job["job"]["is_dependency"] is True

        missing_job_id = await server.handle_tool_call("check_job_status", {})
        assert "error" in missing_job_id and "required" in missing_job_id["error"]

        jobs = await server.handle_tool_call("list_jobs", {})
        assert jobs["success"] is True
        assert {code_job["job"]["job_id"], package_job["job"]["job_id"]} <= {
            job["job_id"] for job in jobs["jobs"]
        }

        repositories = await server.handle_tool_call("list_indexed_repositories", {})
        assert repositories["success"] is True, repositories
        repo_paths = {repo["path"] for repo in repositories["repositories"]}
        assert str(project.resolve()) in repo_paths
        assert str(package_dir.resolve()) in repo_paths

        stats = await server.handle_tool_call(
            "get_repository_stats", {"repo_path": str(project)}
        )
        assert stats["success"] is True, stats
        assert stats["stats"]["files"] >= 1

        absent_stats = await server.handle_tool_call(
            "get_repository_stats", {"repo_path": str(workspace / "not-indexed")}
        )
        assert absent_stats["success"] is False
        assert "Repository not found" in absent_stats["error"]

        # These tools have no invalid-argument branch. Inject a backend failure
        # while still exercising the real MCP dispatcher and handler wrappers.
        with monkeypatch.context() as injected:
            injected.setattr(
                server.job_manager,
                "list_jobs",
                lambda: (_ for _ in ()).throw(RuntimeError("isolated job-store failure")),
            )
            failed_jobs = await server.handle_tool_call("list_jobs", {})
        assert "error" in failed_jobs and "Failed to list jobs" in failed_jobs["error"]

        with monkeypatch.context() as injected:
            injected.setattr(
                server.code_finder,
                "list_indexed_repositories",
                lambda **kwargs: (_ for _ in ()).throw(RuntimeError("isolated graph read failure")),
            )
            failed_repositories = await server.handle_tool_call(
                "list_indexed_repositories", {}
            )
        assert "error" in failed_repositories
        assert "Failed to list indexed repositories" in failed_repositories["error"]
    finally:
        server.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_and_graph_listing_tools_use_real_mcp_behavior(tmp_path, monkeypatch):
    """Repository deletion must affect only a test-indexed path and graph listing must report backend outcomes."""
    workspace, server = _isolated_workspace(tmp_path, monkeypatch)
    project = workspace / "projects" / "to-delete"
    _write_python_project(project, function_name="disposable")
    try:
        with monkeypatch.context() as enabled:
            enabled.setattr(
                config_manager,
                "get_config_value",
                lambda key: "true" if key == "ALLOW_DB_DELETION" else None,
            )
            missing_path = await server.handle_tool_call("delete_repository", {})
        assert "error" in missing_path and "required" in missing_path["error"]

        indexed = await server.handle_tool_call("add_code_to_graph", {"repo_path": str(project)})
        assert indexed["success"] is True, indexed
        finished = await _wait_for_job(server, indexed["job_id"])
        assert finished["job"]["status"] == "completed", finished

        graphs = await server.handle_tool_call("list_graphs", {})
        assert graphs["success"] is True, graphs
        assert graphs["graphs"] == []
        assert "not supported" in graphs.get("message", "")

        denied = await server.handle_tool_call(
            "delete_repository", {"repo_path": str(project)}
        )
        assert "error" in denied and "deletion is disabled" in denied["error"]
        still_indexed = await server.handle_tool_call("list_indexed_repositories", {})
        assert any(repo["path"] == str(project.resolve()) for repo in still_indexed["repositories"])

        # Deletion is enabled only for this test call; the handler still runs
        # its normal permission check and deletes a repository created above.
        with monkeypatch.context() as enabled:
            enabled.setattr(
                config_manager,
                "get_config_value",
                lambda key: "true" if key == "ALLOW_DB_DELETION" else None,
            )
            deleted = await server.handle_tool_call(
                "delete_repository", {"repo_path": str(project)}
            )
        assert deleted["success"] is True, deleted

        after_delete = await server.handle_tool_call("list_indexed_repositories", {})
        assert all(repo["path"] != str(project.resolve()) for repo in after_delete["repositories"])

        # Kùzu has no list_graphs API; this injected backend failure covers the
        # handler's supported-backend exception response through handle_tool_call.
        with monkeypatch.context() as injected:
            injected.setattr(
                server.db_manager,
                "list_graphs",
                lambda: (_ for _ in ()).throw(RuntimeError("isolated graph catalog failure")),
                raising=False,
            )
            failed_graphs = await server.handle_tool_call("list_graphs", {})
        assert "error" in failed_graphs
        assert "Failed to list graphs" in failed_graphs["error"]
    finally:
        server.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_context_discovery_and_switch_tools_use_real_mcp_behavior(tmp_path, monkeypatch):
    """Context discovery and switching must stay within allowed roots and use discovered local databases."""
    workspace, server = _isolated_workspace(tmp_path, monkeypatch)
    target = workspace / "target-repo"
    target.mkdir()
    _write_repo_context(target)
    original_db_path = server.resolved_context.db_path
    try:
        discovered = await server.handle_tool_call(
            "discover_codegraph_contexts", {"path": str(workspace)}
        )
        assert discovered["status"] == "ok", discovered
        contexts = {context["repo_name"]: context for context in discovered["contexts"]}
        assert "target-repo" in contexts
        assert contexts["target-repo"]["database"] == "kuzudb"
        assert Path(contexts["target-repo"]["db_path"]).is_relative_to(workspace)

        outside_discovery = await server.handle_tool_call(
            "discover_codegraph_contexts", {"path": "/etc"}
        )
        assert "error" in outside_discovery
        assert "outside allowed roots" in outside_discovery["error"]

        missing_context = await server.handle_tool_call("switch_context", {})
        assert "error" in missing_context and "required" in missing_context["error"]

        outside_switch = await server.handle_tool_call(
            "switch_context", {"context_path": "/etc", "save": False}
        )
        assert "error" in outside_switch
        assert "outside allowed roots" in outside_switch["error"]

        switched = await server.handle_tool_call(
            "switch_context", {"context_path": str(target), "save": False}
        )
        assert switched["status"] == "ok", switched
        assert switched["database"] == "kuzudb"
        assert Path(switched["db_path"]).is_relative_to(target / ".codegraphcontext")
        assert switched["db_path"] != original_db_path
        assert server.resolved_context.db_path == switched["db_path"]
    finally:
        server.shutdown()
