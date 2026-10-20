"""Regression coverage for contexts whose database backend is not explicit."""

import os
import sys
from pathlib import Path

import pytest
import yaml

from codegraphcontext.cli import config_manager
from codegraphcontext.cli import cli_helpers
from codegraphcontext.cli.config_manager import ResolvedContext
import codegraphcontext.core as core
from codegraphcontext.server import MCPServer
import codegraphcontext.server as server_module


@pytest.fixture(autouse=True)
def _isolate_database_environment():
    names = (
        "DEFAULT_DATABASE",
        "DATABASE_TYPE",
        "CGC_RUNTIME_DB_TYPE",
        "CGC_RUNTIME_DB_PATH",
        "CGC_DB_SELECTION_SOURCE",
        "CGC_SELECTED_DATABASE",
        "FALKORDB_HOST",
        "FALKORDB_PATH",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NORNIC_URI",
        "NORNIC_USERNAME",
        "NORNIC_PASSWORD",
        "CGC_IGNORE_PROJECT_ENV",
        "CGC_LOAD_PROJECT_ENV",
    )
    previous = {name: os.environ[name] for name in names if name in os.environ}
    for name in names:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name in names:
            os.environ.pop(name, None)
        os.environ.update(previous)


def test_context_switch_uses_configured_default_and_records_selected_backend(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    context_path = tmp_path / ".codegraphcontext"
    context_path.mkdir()
    requested = []
    saved = {}

    class Manager:
        db_path = str(context_path / "db" / "ladybugdb")

        def get_driver(self):
            return None

        def get_backend_type(self):
            return "ladybugdb"

    manager = Manager()
    monkeypatch.setattr(server_module, "load_config", lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"})
    monkeypatch.setattr(server_module, "get_database_manager", lambda db_path: requested.append(db_path) or manager)
    monkeypatch.setattr(
        server_module,
        "save_workspace_mapping",
        lambda cwd, path, *, database=None, database_is_explicit=True: saved.update(
            cwd=cwd,
            context_path=path,
            database=database,
            database_is_explicit=database_is_explicit,
        ),
    )
    monkeypatch.setattr(server_module, "get_repo_context_dir_name", lambda: context_path.name)
    monkeypatch.setattr("codegraphcontext.utils.path_sandbox.is_path_allowed", lambda _path: True)
    monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())

    server = MCPServer.__new__(MCPServer)
    server.cwd = tmp_path
    server.resolved_context = ResolvedContext(
        mode="global",
        context_name="",
        database="ladybugdb",
        db_path="/old/database",
        cgcignore_path="",
    )
    server.db_manager = object()
    server.job_manager = object()
    server.loop = None
    server._active_jobs_block_context_switch = lambda: None
    server._stop_current_watcher = lambda: False
    server._close_db_manager_for_context_switch = lambda _watcher_was_running: None
    server._start_watcher_if = lambda _watcher_was_running: None

    result = server._switch_context_tool_locked(context_path=str(context_path))

    assert requested == [str(context_path / "db" / "ladybugdb")]
    assert result["database"] == "ladybugdb"
    assert result["db_path"] == manager.db_path
    assert server.resolved_context.database == "ladybugdb"
    assert server.resolved_context.db_path == manager.db_path
    assert saved["database"] == "ladybugdb"
    assert saved["database_is_explicit"] is False


def test_context_switch_preserves_legacy_falkordb_storage_without_config(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    context_path = tmp_path / ".codegraphcontext"
    legacy_db_path = context_path / "db" / "falkordb"
    legacy_db_path.mkdir(parents=True)
    requested = []
    saved = {}

    class Manager:
        db_path = str(legacy_db_path)

        def get_driver(self):
            return None

        def get_backend_type(self):
            return "falkordb"

    manager = Manager()
    monkeypatch.setattr(
        server_module, "load_config", lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"}
    )
    monkeypatch.setattr(
        server_module,
        "get_database_manager",
        lambda db_path: requested.append(db_path) or manager,
    )
    monkeypatch.setattr(
        server_module,
        "save_workspace_mapping",
        lambda cwd, path, *, database=None, database_is_explicit=True: saved.update(
            cwd=cwd,
            context_path=path,
            database=database,
            database_is_explicit=database_is_explicit,
        ),
    )
    monkeypatch.setattr(server_module, "get_repo_context_dir_name", lambda: context_path.name)
    monkeypatch.setattr("codegraphcontext.utils.path_sandbox.is_path_allowed", lambda _path: True)
    monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())

    server = MCPServer.__new__(MCPServer)
    server.cwd = tmp_path
    server.resolved_context = ResolvedContext(
        mode="global",
        context_name="",
        database="ladybugdb",
        db_path="/old/database",
        cgcignore_path="",
    )
    server.db_manager = object()
    server.job_manager = object()
    server.loop = None
    server._active_jobs_block_context_switch = lambda: None
    server._stop_current_watcher = lambda: False
    server._close_db_manager_for_context_switch = lambda _watcher_was_running: None
    server._start_watcher_if = lambda _watcher_was_running: None

    result = server._switch_context_tool_locked(context_path=str(context_path))

    assert requested == [str(legacy_db_path)]
    assert result["database"] == "falkordb"
    assert result["db_path"] == str(legacy_db_path)
    assert server.resolved_context.database == "falkordb"
    assert saved["database"] == "falkordb"


def test_context_switch_records_backend_selected_after_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    context_path = tmp_path / ".codegraphcontext"
    context_path.mkdir()
    (context_path / "config.yaml").write_text("database: falkordb\n", encoding="utf-8")
    requested = []
    saved = {}

    class Manager:
        db_path = str(context_path / "db" / "ladybugdb")

        def get_driver(self):
            return None

        def get_backend_type(self):
            return "ladybugdb"

    manager = Manager()
    monkeypatch.setattr(server_module, "load_config", lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"})
    monkeypatch.setattr(server_module, "get_database_manager", lambda db_path: requested.append(db_path) or manager)
    monkeypatch.setattr(
        server_module,
        "save_workspace_mapping",
        lambda cwd, path, *, database=None, database_is_explicit=True: saved.update(
            cwd=cwd,
            context_path=path,
            database=database,
            database_is_explicit=database_is_explicit,
        ),
    )
    monkeypatch.setattr(server_module, "get_repo_context_dir_name", lambda: context_path.name)
    monkeypatch.setattr("codegraphcontext.utils.path_sandbox.is_path_allowed", lambda _path: True)
    monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())

    server = MCPServer.__new__(MCPServer)
    server.cwd = tmp_path
    server.resolved_context = ResolvedContext(
        mode="global",
        context_name="",
        database="ladybugdb",
        db_path="/old/database",
        cgcignore_path="",
    )
    server.db_manager = object()
    server.job_manager = object()
    server.loop = None
    server._active_jobs_block_context_switch = lambda: None
    server._stop_current_watcher = lambda: False
    server._close_db_manager_for_context_switch = lambda _watcher_was_running: None
    server._start_watcher_if = lambda _watcher_was_running: None

    result = server._switch_context_tool_locked(context_path=str(context_path))

    assert requested == [str(context_path / "db" / "falkordb")]
    assert result["database"] == "ladybugdb"
    assert result["db_path"] == manager.db_path
    assert server.resolved_context.database == "ladybugdb"
    assert server.resolved_context.db_path == manager.db_path
    assert saved["database"] == "ladybugdb"
    assert saved["database_is_explicit"] is True


def test_save_workspace_mapping_defaults_to_configured_backend(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    context_path = tmp_path / "context"
    cwd.mkdir()
    context_path.mkdir()
    saved = {}

    monkeypatch.setattr(config_manager, "load_config", lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"})
    monkeypatch.setattr(config_manager, "_load_workspace_mappings", lambda: {})
    monkeypatch.setattr(config_manager, "_save_workspace_mappings", lambda mappings: saved.update(mappings))

    config_manager.save_workspace_mapping(cwd, context_path)

    assert saved[str(cwd.resolve())]["database"] == "ladybugdb"


def test_save_workspace_mapping_prefers_actual_backend_over_context_request(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    context_path = tmp_path / "context"
    cwd.mkdir()
    context_path.mkdir()
    (context_path / "config.yaml").write_text("database: falkordb\n", encoding="utf-8")
    saved = {}

    monkeypatch.setattr(config_manager, "load_config", lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"})
    monkeypatch.setattr(config_manager, "_load_workspace_mappings", lambda: {})
    monkeypatch.setattr(config_manager, "_save_workspace_mappings", lambda mappings: saved.update(mappings))

    config_manager.save_workspace_mapping(cwd, context_path, database="ladybugdb")

    assert saved[str(cwd.resolve())]["database"] == "ladybugdb"


def test_save_workspace_mapping_persists_backend_selection_provenance(
    tmp_path, monkeypatch
):
    cwd = tmp_path / "repo"
    context_path = tmp_path / "context"
    cwd.mkdir()
    context_path.mkdir()
    saved = {}

    monkeypatch.setattr(config_manager, "_load_workspace_mappings", lambda: {})
    monkeypatch.setattr(
        config_manager,
        "_save_workspace_mappings",
        lambda mappings: saved.update(mappings),
    )

    config_manager.save_workspace_mapping(
        cwd,
        context_path,
        database="ladybugdb",
        database_is_explicit=False,
    )

    assert saved[str(cwd.resolve())] == {
        "context_path": str(context_path.resolve()),
        "database": "ladybugdb",
        "database_is_explicit": False,
    }


def test_sparse_workspace_mapping_infers_existing_legacy_falkordb_storage(
    tmp_path, monkeypatch
):
    cwd = tmp_path / "repo"
    context_path = tmp_path / "context"
    cwd.mkdir()
    (context_path / "db" / "falkordb").mkdir(parents=True)
    saved = {}

    monkeypatch.setattr(
        config_manager, "load_config", lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"}
    )
    monkeypatch.setattr(config_manager, "_load_workspace_mappings", lambda: {})
    monkeypatch.setattr(
        config_manager, "_save_workspace_mappings", lambda mappings: saved.update(mappings)
    )

    config_manager.save_workspace_mapping(cwd, context_path)

    assert saved[str(cwd.resolve())]["database"] == "falkordb"


def test_discovered_context_without_backend_uses_configured_default(tmp_path, monkeypatch):
    start = tmp_path / "workspaces"
    context_path = start / "repo" / ".codegraphcontext"
    context_path.mkdir(parents=True)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr(config_manager, "_configured_default_database", lambda **_kwargs: "ladybugdb")
    monkeypatch.setattr(config_manager, "get_repo_context_path", lambda repo: repo / ".codegraphcontext")

    contexts = config_manager.discover_child_contexts(start)

    assert len(contexts) == 1
    assert contexts[0].database == "ladybugdb"
    assert contexts[0].db_path == str(context_path / "db" / "ladybugdb")


def test_discovered_legacy_context_uses_existing_falkordb_storage(tmp_path, monkeypatch):
    start = tmp_path / "workspaces"
    context_path = start / "repo" / ".codegraphcontext"
    (context_path / "db" / "falkordb").mkdir(parents=True)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr(
        config_manager, "_configured_default_database", lambda **_kwargs: "ladybugdb"
    )
    monkeypatch.setattr(
        config_manager, "get_repo_context_path", lambda repo: repo / ".codegraphcontext"
    )

    contexts = config_manager.discover_child_contexts(start)

    assert len(contexts) == 1
    assert contexts[0].database == "falkordb"
    assert contexts[0].db_path == str(context_path / "db" / "falkordb")


def test_per_repo_context_infers_existing_legacy_falkordb_storage(tmp_path, monkeypatch):
    context_config = tmp_path / "config.yaml"
    context_config.write_text("mode: per-repo\n", encoding="utf-8")
    repo = tmp_path / "repo"
    context_path = repo / ".codegraphcontext"
    (context_path / "db" / "falkordb").mkdir(parents=True)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager,
        "_LEGACY_CONTEXT_CONFIG_FILE",
        tmp_path / "legacy-config.yaml",
    )
    monkeypatch.setattr(
        config_manager, "get_repo_context_path", lambda root: root / ".codegraphcontext"
    )
    monkeypatch.setattr(
        config_manager, "load_config", lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"}
    )
    for name in ("CGC_RUNTIME_DB_TYPE", "DEFAULT_DATABASE"):
        monkeypatch.delenv(name, raising=False)

    resolved = config_manager.resolve_context(
        cwd=repo, create_local_context=False, read_only=True
    )

    assert resolved.database == "falkordb"
    assert resolved.db_path == str(context_path / "db" / "falkordb")


def test_per_repo_explicit_falkordb_config_wins_over_existing_kuzudb_storage(
    tmp_path, monkeypatch
):
    context_config = tmp_path / "config.yaml"
    context_config.write_text("mode: per-repo\n", encoding="utf-8")
    repo = tmp_path / "repo"
    context_path = repo / ".codegraphcontext"
    (context_path / "db" / "kuzudb").mkdir(parents=True)
    (context_path / "config.yaml").write_text(
        "database: falkordb\n", encoding="utf-8"
    )
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager,
        "_LEGACY_CONTEXT_CONFIG_FILE",
        tmp_path / "legacy-config.yaml",
    )
    monkeypatch.setattr(
        config_manager, "get_repo_context_path", lambda root: root / ".codegraphcontext"
    )

    resolved = config_manager.resolve_context(
        cwd=repo, create_local_context=False, read_only=True
    )

    assert resolved.database == "falkordb"
    assert resolved.db_path == str(context_path / "db" / "falkordb")
    assert resolved.database_is_explicit is True


def test_sparse_workspace_mapping_resolves_to_legacy_falkordb_storage(
    tmp_path, monkeypatch
):
    context_config = tmp_path / "config.yaml"
    context_config.write_text("mode: per-repo\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    mapped_context = tmp_path / "mapped-context"
    legacy_db_path = mapped_context / "db" / "falkordb"
    legacy_db_path.mkdir(parents=True)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager,
        "_LEGACY_CONTEXT_CONFIG_FILE",
        tmp_path / "legacy-config.yaml",
    )
    monkeypatch.setattr(config_manager, "find_local_cgc_dir", lambda _cwd: None)
    monkeypatch.setattr(
        config_manager,
        "get_repo_context_path",
        lambda root: root / ".codegraphcontext",
    )
    monkeypatch.setattr(
        config_manager,
        "get_workspace_mapping",
        lambda _cwd: {"context_path": str(mapped_context)},
    )
    monkeypatch.setattr(
        config_manager, "_configured_default_database", lambda **_kwargs: "ladybugdb"
    )

    resolved = config_manager.resolve_context(cwd=repo, read_only=True)

    assert resolved.database == "falkordb"
    assert resolved.db_path == str(legacy_db_path)


def test_legacy_named_context_without_backend_keeps_falkordb_storage(
    tmp_path, monkeypatch
):
    context_config = tmp_path / "config.yaml"
    context_config.write_text(
        "mode: named\ndefault_context: legacy\ncontexts:\n  legacy: {}\n",
        encoding="utf-8",
    )
    legacy_db_path = tmp_path / "contexts" / "legacy" / "db" / "falkordb"
    legacy_db_path.mkdir(parents=True)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", tmp_path / "cgc_config.yaml")

    config = config_manager.load_context_config(create_if_missing=False)
    resolved = config_manager.resolve_context("legacy", read_only=True)

    assert config.contexts["legacy"].database == "falkordb"
    assert config.contexts["legacy"].db_path == str(legacy_db_path)
    assert resolved.database == "falkordb"
    assert resolved.db_path == str(legacy_db_path)


@pytest.mark.parametrize("legacy_backend", ("falkordb", "kuzudb"))
def test_global_context_infers_existing_backend_when_default_is_unset(
    tmp_path, monkeypatch, legacy_backend
):
    profile = tmp_path / "profile"
    context_config = profile / "config.yaml"
    context_config.parent.mkdir(parents=True)
    context_config.write_text("mode: global\n", encoding="utf-8")
    legacy_db_path = profile / "global" / "db" / legacy_backend
    legacy_db_path.mkdir(parents=True)

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_env", lambda **_kwargs: None)
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)
    monkeypatch.delenv("CGC_RUNTIME_DB_PATH", raising=False)

    resolved = config_manager.resolve_context(read_only=True)

    assert resolved.database == legacy_backend
    assert resolved.db_path == str(legacy_db_path)


@pytest.mark.parametrize("startup", ("cli", "mcp"))
def test_legacy_global_falkordb_does_not_override_remote_host(
    startup, tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    context_config = profile / "config.yaml"
    context_config.parent.mkdir(parents=True)
    context_config.write_text("mode: global\n", encoding="utf-8")
    (profile / "global" / "db" / "falkordb").mkdir(parents=True)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_env", lambda **_kwargs: None)
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "CGC_RUNTIME_DB_PATH",
        "DEFAULT_DATABASE",
        "DATABASE_TYPE",
        "FALKORDB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FALKORDB_HOST", "graph.example")

    def resolve_global_context(*_args, **_kwargs):
        return config_manager.resolve_context(read_only=True, cwd=tmp_path)

    factory_calls = []

    def make_manager(db_path):
        backend = (
            os.environ.get("CGC_RUNTIME_DB_TYPE")
            or os.environ.get("DEFAULT_DATABASE")
            or ("falkordb-remote" if os.environ.get("FALKORDB_HOST") else "ladybugdb")
        )
        factory_calls.append((backend, db_path))
        return _SwitchTestManager(backend, db_path)

    if startup == "cli":
        monkeypatch.setattr(cli_helpers, "ensure_first_run_bootstrap", lambda: None)
        monkeypatch.setattr(cli_helpers, "resolve_context", resolve_global_context)
        monkeypatch.setattr(cli_helpers, "get_database_manager", make_manager)
        monkeypatch.setattr(cli_helpers, "JobManager", lambda: object())
        monkeypatch.setattr(cli_helpers, "GraphBuilder", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(cli_helpers, "CodeFinder", lambda *_args, **_kwargs: object())

        manager, _graph_builder, _code_finder, resolved = (
            cli_helpers._initialize_services(cwd=tmp_path)
        )
        selected_backend = manager.get_backend_type()
    else:
        monkeypatch.setattr(server_module, "resolve_context", resolve_global_context)
        monkeypatch.setattr(server_module, "get_database_manager", make_manager)
        monkeypatch.setattr(server_module, "discover_child_contexts", lambda *_a, **_k: [])
        monkeypatch.setattr(server_module, "JobManager", lambda: object())
        monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(MCPServer, "_init_tools", lambda _self: None)

        server = MCPServer(loop=object(), cwd=tmp_path, initialize_database=False)
        resolved = server.resolved_context
        selected_backend = server.db_manager.get_backend_type()

    assert factory_calls[0][0] == "falkordb-remote"
    assert selected_backend == "falkordb-remote"
    assert resolved.database == "falkordb-remote"


def test_explicit_global_falkordb_default_wins_over_remote_host(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    context_config = profile / "config.yaml"
    context_config.parent.mkdir(parents=True)
    context_config.write_text("mode: global\n", encoding="utf-8")
    config_file = profile / ".env"
    config_file.write_text("DEFAULT_DATABASE=falkordb\n", encoding="utf-8")
    (profile / "global" / "db" / "falkordb").mkdir(parents=True)

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_env", lambda **_kwargs: None)
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "CGC_RUNTIME_DB_PATH",
        "DEFAULT_DATABASE",
        "DATABASE_TYPE",
        "FALKORDB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FALKORDB_HOST", "graph.example")

    resolved = config_manager.resolve_context(read_only=True, cwd=tmp_path)

    assert resolved.database == "falkordb"


def test_global_context_infers_existing_in_profile_falkordb_path(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    context_config = profile / "config.yaml"
    context_config.parent.mkdir(parents=True)
    context_config.write_text("mode: global\n", encoding="utf-8")
    custom_db_path = profile / "custom" / "falkordb"
    custom_db_path.mkdir(parents=True)
    config_file = profile / ".env"
    config_file.write_text(f"FALKORDB_PATH={custom_db_path}\n", encoding="utf-8")

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_env", lambda **_kwargs: None)
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "DEFAULT_DATABASE",
        "FALKORDB_PATH",
        "CGC_RUNTIME_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    resolved = config_manager.resolve_context(read_only=True)

    assert resolved.database == "falkordb"
    assert resolved.db_path == str(custom_db_path)


def test_global_context_prefers_configured_falkordb_path_over_legacy_kuzu(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    context_config = profile / "config.yaml"
    context_config.parent.mkdir(parents=True)
    context_config.write_text("mode: global\n", encoding="utf-8")
    configured_db_path = profile / "custom" / "falkordb"
    configured_db_path.mkdir(parents=True)
    (profile / "global" / "db" / "kuzudb").mkdir(parents=True)
    config_file = profile / ".env"
    config_file.write_text(f"FALKORDB_PATH={configured_db_path}\n", encoding="utf-8")

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_env", lambda **_kwargs: None)
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "DEFAULT_DATABASE",
        "FALKORDB_PATH",
        "CGC_RUNTIME_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    resolved = config_manager.resolve_context(read_only=True)

    assert resolved.database == "falkordb"
    assert resolved.db_path == str(configured_db_path)


def test_global_context_uses_existing_kuzudb_db_file(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    context_config = profile / "config.yaml"
    context_config.parent.mkdir(parents=True)
    context_config.write_text("mode: global\n", encoding="utf-8")
    legacy_db_path = profile / "global" / "db" / "kuzudb.db"
    legacy_db_path.parent.mkdir(parents=True)
    legacy_db_path.write_bytes(b"legacy Kuzu database")

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_env", lambda **_kwargs: None)
    for name in ("CGC_RUNTIME_DB_TYPE", "DEFAULT_DATABASE", "CGC_RUNTIME_DB_PATH"):
        monkeypatch.delenv(name, raising=False)

    resolved = config_manager.resolve_context(read_only=True)

    assert resolved.database == "kuzudb"
    assert resolved.db_path == str(legacy_db_path)


def test_explicit_global_default_keeps_precedence_over_legacy_backend(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    context_config = profile / "config.yaml"
    context_config.parent.mkdir(parents=True)
    context_config.write_text("mode: global\n", encoding="utf-8")
    (profile / "global" / "db" / "kuzudb").mkdir(parents=True)
    (profile / "global" / "db" / "falkordb").mkdir(parents=True)
    config_file = profile / ".env"
    config_file.write_text("DEFAULT_DATABASE=ladybugdb\n", encoding="utf-8")

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_env", lambda **_kwargs: None)
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)
    monkeypatch.delenv("CGC_RUNTIME_DB_PATH", raising=False)

    resolved = config_manager.resolve_context(read_only=True)

    assert resolved.database == "ladybugdb"
    assert resolved.db_path == str(profile / "global" / "db" / "ladybugdb")


@pytest.mark.parametrize("context_mode", ("global", "per-repo"))
def test_generated_default_config_keeps_ladybug_fallback_implicit(
    context_mode, tmp_path, monkeypatch
):
    from codegraphcontext.cli import main as cli_main

    environment_before_test = dict(os.environ)
    profile = tmp_path / "profile"
    profile.mkdir()
    config_file = profile / ".env"
    context_config = profile / "config.yaml"
    context_config.write_text(
        f"version: 1\nmode: {context_mode}\n", encoding="utf-8"
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    local_context = repo / ".codegraphcontext"

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "ensure_config_dir", lambda: None)
    monkeypatch.setattr(config_manager, "find_local_env", lambda **_kwargs: None)
    monkeypatch.setattr(
        config_manager, "should_apply_project_dotenv", lambda **_kwargs: False
    )
    monkeypatch.setattr(
        config_manager, "get_repo_context_path", lambda _root: local_context
    )
    monkeypatch.chdir(repo)
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "CGC_RUNTIME_DB_PATH",
        "DEFAULT_DATABASE",
        "DATABASE_TYPE",
        "FALKORDB_HOST",
        "CGC_SELECTED_DATABASE",
        "CGC_DB_SELECTION_SOURCE",
    ):
        monkeypatch.delenv(name, raising=False)

    assert config_manager.ensure_config_file() is True

    monkeypatch.setattr(core, "_is_falkordb_remote_configured", lambda: False)
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: False)
    monkeypatch.setattr(core, "is_falkordb_usable", lambda: True)
    fake = type(sys)("codegraphcontext.core.database_falkordb")

    class FakeFalkorDBUnavailableError(Exception):
        pass

    class FakeFalkorDBManager:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def get_driver(self):
            return object()

        def get_backend_type(self):
            return "falkordb"

    fake.FalkorDBManager = FakeFalkorDBManager
    fake.FalkorDBUnavailableError = FakeFalkorDBUnavailableError
    monkeypatch.setitem(sys.modules, fake.__name__, fake)

    try:
        cli_main._load_credentials()

        manager = core.get_database_manager()

        assert manager.get_backend_type() == "falkordb"
        assert os.environ["CGC_SELECTED_DATABASE"] == "falkordb"
        if context_mode == "per-repo":
            assert config_manager._context_database_setting(local_context) is None
    finally:
        monkeypatch.undo()
        os.environ.clear()
        os.environ.update(environment_before_test)


def test_explicit_ladybug_config_selection_is_persisted(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(
        config_manager, "CONTEXT_CONFIG_FILE", profile / "config.yaml"
    )
    monkeypatch.setattr(config_manager, "ensure_config_dir", lambda: None)
    monkeypatch.setattr(config_manager, "find_local_env", lambda **_kwargs: None)
    monkeypatch.setattr(
        config_manager, "should_apply_project_dotenv", lambda **_kwargs: False
    )
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)

    assert config_manager.set_config_value("DEFAULT_DATABASE", "ladybugdb")

    assert "DEFAULT_DATABASE=ladybugdb" in (profile / ".env").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("select_as_default", (False, True))
def test_named_explicit_falkordb_context_ignores_sibling_kuzudb_storage(
    tmp_path, monkeypatch, select_as_default
):
    profile = tmp_path / "profile"
    context_config = profile / "config.yaml"
    legacy_db_path = profile / "contexts" / "legacy" / "db" / "kuzudb"
    legacy_db_path.mkdir(parents=True)
    context_config.parent.mkdir(parents=True, exist_ok=True)
    context_config.write_text(
        "mode: named\n"
        "default_context: legacy\n"
        "contexts:\n"
        "  legacy:\n"
        "    database: falkordb\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )

    resolved = config_manager.resolve_context(
        None if select_as_default else "legacy", read_only=True
    )

    assert resolved.database == "falkordb"
    assert resolved.db_path == str(legacy_db_path.parent / "falkordb")


def test_named_implicit_falkordb_context_reuses_sibling_kuzudb_storage(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    context_config = profile / "config.yaml"
    legacy_db_path = profile / "contexts" / "legacy" / "db" / "kuzudb"
    legacy_db_path.mkdir(parents=True)
    context_config.parent.mkdir(parents=True, exist_ok=True)
    context_config.write_text(
        "mode: named\n"
        "default_context: legacy\n"
        "contexts:\n"
        "  legacy:\n"
        "    database: falkordb\n"
        "    database_is_explicit: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )

    resolved = config_manager.resolve_context("legacy", read_only=True)

    assert resolved.database == "kuzudb"
    assert resolved.db_path == str(legacy_db_path)


def test_named_falkordb_context_preserves_explicit_custom_path_when_missing(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    context_config = profile / "config.yaml"
    custom_db_path = profile / "custom" / "falkordb"
    sibling_kuzu_path = profile / "contexts" / "legacy" / "db" / "kuzudb"
    sibling_kuzu_path.mkdir(parents=True)
    context_config.parent.mkdir(parents=True, exist_ok=True)
    context_config.write_text(
        "mode: named\n"
        "default_context: legacy\n"
        "contexts:\n"
        "  legacy:\n"
        "    database: falkordb\n"
        f"    db_path: {custom_db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )

    resolved = config_manager.resolve_context("legacy", read_only=True)

    assert not custom_db_path.exists()
    assert resolved.database == "falkordb"
    assert resolved.db_path == str(custom_db_path)


def test_workspace_mapping_falkordb_backend_uses_existing_sibling_kuzudb_storage(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    context_config = profile / "config.yaml"
    context_config.parent.mkdir(parents=True)
    context_config.write_text("mode: per-repo\n", encoding="utf-8")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    context_path = tmp_path / "saved-context"
    legacy_db_path = context_path / "db" / "kuzudb"
    legacy_db_path.mkdir(parents=True)

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_cgc_dir", lambda _cwd: None)
    monkeypatch.setattr(config_manager, "get_repo_context_path", lambda root: root / ".codegraphcontext")
    monkeypatch.setattr(
        config_manager,
        "get_workspace_mapping",
        lambda _cwd: {
            "context_path": str(context_path),
            "database": "falkordb",
        },
    )
    monkeypatch.setattr(
        config_manager, "_configured_default_database", lambda **_kwargs: "ladybugdb"
    )

    resolved = config_manager.resolve_context(cwd=cwd, read_only=True)

    assert resolved.database == "falkordb"
    assert resolved.db_path == str(legacy_db_path.parent / "falkordb")
    assert resolved.database_is_explicit is True


@pytest.mark.parametrize("select_as_default", (False, True))
def test_named_explicit_falkordb_context_ignores_sibling_kuzudb_db_file(
    tmp_path, monkeypatch, select_as_default
):
    profile = tmp_path / "profile"
    context_config = profile / "config.yaml"
    legacy_db_path = profile / "contexts" / "legacy" / "db" / "kuzudb.db"
    legacy_db_path.parent.mkdir(parents=True)
    legacy_db_path.write_bytes(b"legacy Kuzu database")
    context_config.parent.mkdir(parents=True, exist_ok=True)
    context_config.write_text(
        "mode: named\n"
        "default_context: legacy\n"
        "contexts:\n"
        "  legacy:\n"
        "    database: falkordb\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )

    resolved = config_manager.resolve_context(
        None if select_as_default else "legacy", read_only=True
    )

    assert resolved.database == "falkordb"
    assert resolved.db_path == str(legacy_db_path.parent / "falkordb")


def test_workspace_mapping_falkordb_backend_ignores_sibling_kuzudb_db_file(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    context_config = profile / "config.yaml"
    context_config.parent.mkdir(parents=True)
    context_config.write_text("mode: per-repo\n", encoding="utf-8")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    context_path = tmp_path / "saved-context"
    legacy_db_path = context_path / "db" / "kuzudb.db"
    legacy_db_path.parent.mkdir(parents=True)
    legacy_db_path.write_bytes(b"legacy Kuzu database")

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_cgc_dir", lambda _cwd: None)
    monkeypatch.setattr(
        config_manager, "get_repo_context_path", lambda root: root / ".codegraphcontext"
    )
    monkeypatch.setattr(
        config_manager,
        "get_workspace_mapping",
        lambda _cwd: {
            "context_path": str(context_path),
            "database": "falkordb",
        },
    )
    monkeypatch.setattr(
        config_manager, "_configured_default_database", lambda **_kwargs: "ladybugdb"
    )

    resolved = config_manager.resolve_context(cwd=cwd, read_only=True)

    assert resolved.database == "falkordb"
    assert resolved.db_path == str(legacy_db_path.parent / "falkordb")


def test_per_repo_context_uses_existing_kuzudb_db_file(tmp_path, monkeypatch):
    context_config = tmp_path / "config.yaml"
    context_config.write_text("mode: per-repo\n", encoding="utf-8")
    repo = tmp_path / "repo"
    context_path = repo / ".codegraphcontext"
    legacy_db_path = context_path / "db" / "kuzudb.db"
    legacy_db_path.parent.mkdir(parents=True)
    legacy_db_path.write_bytes(b"legacy Kuzu database")
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", tmp_path / "legacy-config.yaml"
    )
    monkeypatch.setattr(
        config_manager, "get_repo_context_path", lambda root: root / ".codegraphcontext"
    )
    monkeypatch.setattr(
        config_manager, "load_config", lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"}
    )
    for name in ("CGC_RUNTIME_DB_TYPE", "DEFAULT_DATABASE"):
        monkeypatch.delenv(name, raising=False)

    resolved = config_manager.resolve_context(
        cwd=repo, create_local_context=False, read_only=True
    )

    assert resolved.database == "kuzudb"
    assert resolved.db_path == str(legacy_db_path)


def test_discovered_legacy_context_uses_existing_kuzudb_db_file(tmp_path, monkeypatch):
    start = tmp_path / "workspaces"
    context_path = start / "repo" / ".codegraphcontext"
    legacy_db_path = context_path / "db" / "kuzudb.db"
    legacy_db_path.parent.mkdir(parents=True)
    legacy_db_path.write_bytes(b"legacy Kuzu database")
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr(
        config_manager, "_configured_default_database", lambda **_kwargs: "ladybugdb"
    )
    monkeypatch.setattr(
        config_manager, "get_repo_context_path", lambda repo: repo / ".codegraphcontext"
    )

    contexts = config_manager.discover_child_contexts(start)

    assert len(contexts) == 1
    assert contexts[0].database == "kuzudb"
    assert contexts[0].db_path == str(legacy_db_path)


@pytest.mark.parametrize(
    ("preview_backend", "expected"),
    (
        (
            "kuzudb",
            [
                ("/global/db/kuzudb", "kuzudb"),
                ("/global/db/ladybugdb", "ladybugdb"),
            ],
        ),
        ("falkordb-remote", []),
    ),
)
def test_gateway_preview_tracks_implicit_global_backend(
    monkeypatch, preview_backend, expected
):
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)
    monkeypatch.delenv("CGC_RUNTIME_DB_PATH", raising=False)
    monkeypatch.setattr(
        core, "_default_backend_preview", lambda: (preview_backend, ())
    )
    context = ResolvedContext(
        mode="global",
        context_name="",
        database="ladybugdb",
        db_path="/global/db/ladybugdb",
        cgcignore_path="",
    )

    assert cli_helpers._gateway_candidates_for_context(context) == expected


@pytest.mark.parametrize("context_mode", ("per-repo", "named"))
def test_gateway_preview_tracks_implicit_context_fallback(
    monkeypatch, context_mode
):
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "CGC_RUNTIME_DB_PATH",
        "DEFAULT_DATABASE",
    ):
        monkeypatch.delenv(name, raising=False)
    context = ResolvedContext(
        mode=context_mode,
        context_name="project" if context_mode == "named" else "",
        database="ladybugdb",
        db_path="/contexts/project/db/ladybugdb",
        cgcignore_path="",
        database_is_explicit=False,
    )
    preview_calls = []
    monkeypatch.setattr(
        core,
        "_default_backend_preview",
        lambda: preview_calls.append(True) or ("kuzudb", ()),
    )

    candidates = cli_helpers._gateway_candidates_for_context(context)

    kuzu_path = core._fallback_db_path_for(context.db_path, "kuzudb")
    assert preview_calls == [True]
    assert candidates == [
        (kuzu_path, "kuzudb"),
        (context.db_path, "ladybugdb"),
    ]


def test_gateway_preview_preserves_explicit_runtime_path(monkeypatch):
    runtime_path = "/runtime/db/ladybugdb"
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)
    monkeypatch.setenv("CGC_RUNTIME_DB_PATH", runtime_path)
    monkeypatch.setattr(core, "_default_backend_preview", lambda: ("kuzudb", ()))
    context = ResolvedContext(
        mode="global",
        context_name="",
        database="ladybugdb",
        db_path="/global/db/ladybugdb",
        cgcignore_path="",
    )

    assert cli_helpers._gateway_candidates_for_context(context) == [
        (runtime_path, "kuzudb"),
        (runtime_path, "ladybugdb"),
    ]


def _configure_implicit_kuzu_fallback(monkeypatch):
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "DEFAULT_DATABASE",
        "FALKORDB_HOST",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NORNIC_URI",
        "NORNIC_USERNAME",
        "NORNIC_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(core, "_is_falkordb_remote_configured", lambda: False)
    monkeypatch.setattr(core, "is_falkordb_usable", lambda: False)
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: False)
    monkeypatch.setattr(core, "_is_kuzudb_available", lambda: True)

    fake = type(sys)("codegraphcontext.core.database_kuzu")

    class FakeKuzuManager:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def get_driver(self):
            return None

        def get_backend_type(self):
            return "kuzudb"

    fake.KuzuDBManager = FakeKuzuManager
    monkeypatch.setitem(sys.modules, fake.__name__, fake)


@pytest.mark.parametrize("context_mode", ("global", "per-repo", "named"))
def test_mcp_server_keeps_implicit_default_available_for_factory_fallback(
    tmp_path, monkeypatch, context_mode
):
    _configure_implicit_kuzu_fallback(monkeypatch)
    context = ResolvedContext(
        mode=context_mode,
        context_name="workspace" if context_mode == "named" else "",
        database="ladybugdb",
        db_path="/global/db/ladybugdb",
        cgcignore_path="",
        is_local=context_mode == "per-repo",
    )
    monkeypatch.setattr(server_module, "resolve_context", lambda **_kwargs: context)
    monkeypatch.setattr(server_module, "discover_child_contexts", lambda *_a, **_k: [])
    monkeypatch.setattr(server_module, "get_database_manager", core.get_database_manager)
    monkeypatch.setattr(server_module, "JobManager", lambda: object())
    monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(MCPServer, "_init_tools", lambda self: None)

    server = MCPServer(cwd=tmp_path, initialize_database=False)

    assert server.db_manager.get_backend_type() == "kuzudb"
    assert server.db_manager.db_path == "/global/db/kuzudb"
    assert server.resolved_context.database == "kuzudb"
    assert server.resolved_context.db_path == "/global/db/kuzudb"
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "kuzudb"


@pytest.mark.parametrize("context_mode", ("global", "per-repo", "named"))
def test_cli_services_keeps_implicit_default_available_for_factory_fallback(
    monkeypatch, context_mode
):
    _configure_implicit_kuzu_fallback(monkeypatch)
    context = ResolvedContext(
        mode=context_mode,
        context_name="workspace" if context_mode == "named" else "",
        database="ladybugdb",
        db_path="/global/db/ladybugdb",
        cgcignore_path="",
        is_local=context_mode == "per-repo",
    )
    monkeypatch.setattr(cli_helpers, "ensure_first_run_bootstrap", lambda: None)
    monkeypatch.setattr(cli_helpers, "resolve_context", lambda *_a, **_k: context)
    monkeypatch.setattr(cli_helpers, "get_database_manager", core.get_database_manager)
    monkeypatch.setattr(cli_helpers, "JobManager", lambda: object())
    monkeypatch.setattr(cli_helpers, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli_helpers, "CodeFinder", lambda *_args, **_kwargs: object())

    manager, _graph_builder, _code_finder, returned_context = cli_helpers._initialize_services()

    assert manager.get_backend_type() == "kuzudb"
    assert manager.db_path == "/global/db/kuzudb"
    assert returned_context.database == "kuzudb"
    assert returned_context.db_path == "/global/db/kuzudb"
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "kuzudb"
    assert "DEFAULT_DATABASE" not in os.environ


def test_returning_to_global_allows_implicit_ladybug_default_fallback(
    tmp_path, monkeypatch
):
    _configure_implicit_kuzu_fallback(monkeypatch)
    server, _context_path = _make_switchable_server(tmp_path, monkeypatch)
    server.resolved_context = ResolvedContext(
        mode="per-repo",
        context_name="",
        database="kuzudb",
        db_path="/context/db/kuzudb",
        cgcignore_path="",
        is_local=True,
    )
    monkeypatch.setattr(server_module, "get_database_manager", core.get_database_manager)

    result = server._switch_context_tool_locked(context_path="global")

    assert result["status"] == "ok"
    assert result["database"] == "kuzudb"
    assert result["db_path"] == "/global/kuzudb"
    assert server.resolved_context.database == "kuzudb"
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "kuzudb"


def test_cli_context_selection_keeps_inherited_ladybug_available_for_fallback(
    tmp_path, monkeypatch
):
    _configure_implicit_kuzu_fallback(monkeypatch)
    monkeypatch.delenv("DATABASE_TYPE", raising=False)
    monkeypatch.delenv("CGC_SELECTED_DATABASE", raising=False)
    monkeypatch.delenv("CGC_DB_SELECTION_SOURCE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", tmp_path / "missing.env")
    monkeypatch.setattr(
        config_manager, "should_apply_project_dotenv", lambda **_kwargs: False
    )
    monkeypatch.setattr(
        config_manager,
        "resolve_context",
        lambda *_args, **_kwargs: ResolvedContext(
            mode="per-repo",
            context_name="",
            database="ladybugdb",
            db_path=str(tmp_path / ".codegraphcontext" / "db" / "ladybugdb"),
            cgcignore_path="",
            is_local=True,
        ),
    )

    from codegraphcontext.cli import main as cli_main

    cli_main._load_credentials(read_only=True)

    assert "DEFAULT_DATABASE" not in os.environ
    assert os.environ["CGC_SELECTED_DATABASE"] == "kuzudb"
    assert os.environ["CGC_DB_SELECTION_SOURCE"] == "auto-detect"


def test_reindex_keeps_inherited_ladybug_available_for_fallback(
    tmp_path, monkeypatch
):
    _configure_implicit_kuzu_fallback(monkeypatch)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    context = ResolvedContext(
        mode="per-repo",
        context_name="",
        database="ladybugdb",
        db_path=str(tmp_path / ".codegraphcontext" / "db" / "ladybugdb"),
        cgcignore_path="",
        is_local=True,
    )
    monkeypatch.setattr(cli_helpers, "ensure_first_run_bootstrap", lambda: None)
    monkeypatch.setattr(cli_helpers, "resolve_context", lambda *_a, **_k: context)
    monkeypatch.setattr(cli_helpers, "_gateway_candidates_for_context", lambda _ctx: [])
    observed = {}

    class ReindexReachedServices(Exception):
        pass

    def initialize_services(*_args, **_kwargs):
        observed["runtime"] = os.environ.get("CGC_RUNTIME_DB_TYPE")
        observed["default"] = os.environ.get("DEFAULT_DATABASE")
        raise ReindexReachedServices

    monkeypatch.setattr(cli_helpers, "_initialize_services", initialize_services)

    with pytest.raises(ReindexReachedServices):
        cli_helpers.reindex_helper(str(repo_path))

    assert observed == {"runtime": None, "default": None}


def test_reindex_respects_explicit_backend_in_workspace_mapping(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    profile.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    context_path = profile / "saved-context"
    context_path.mkdir()
    context_config = profile / "config.yaml"
    context_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mode": "per-repo",
                "workspace_mappings": {
                    str(repo_path.resolve()): {
                        "context_path": str(context_path.resolve()),
                        "database": "ladybugdb",
                        "database_is_explicit": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_cgc_dir", lambda _cwd: None)
    monkeypatch.setattr(
        config_manager,
        "get_repo_context_path",
        lambda root: root / ".codegraphcontext",
    )
    monkeypatch.setattr(cli_helpers, "ensure_first_run_bootstrap", lambda: None)
    monkeypatch.setattr(cli_helpers, "_gateway_candidates_for_context", lambda _ctx: [])
    context = config_manager.resolve_context(
        cwd=repo_path, create_local_context=False, read_only=True
    )
    assert context.database == "ladybugdb"
    assert context.database_is_explicit is True
    observed = {}

    class ReindexReachedServices(Exception):
        pass

    def initialize_services(*_args, **_kwargs):
        observed["runtime"] = os.environ.get("CGC_RUNTIME_DB_TYPE")
        observed["default"] = os.environ.get("DEFAULT_DATABASE")
        raise ReindexReachedServices

    monkeypatch.setattr(cli_helpers, "_initialize_services", initialize_services)

    with pytest.raises(ReindexReachedServices):
        cli_helpers.reindex_helper(str(repo_path))

    assert observed == {"runtime": None, "default": "ladybugdb"}


def test_path_context_switch_keeps_inherited_ladybug_available_for_fallback(
    tmp_path, monkeypatch
):
    _configure_implicit_kuzu_fallback(monkeypatch)
    server, context_path = _make_switchable_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server_module, "get_database_manager", core.get_database_manager)

    result = server._switch_context_tool_locked(context_path=str(context_path))

    assert result.get("status") == "ok", result
    assert result["database"] == "kuzudb"
    assert result["db_path"] == str(context_path / "db" / "kuzudb")
    assert server.resolved_context.database == "kuzudb"
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "kuzudb"


def test_path_context_switch_ignores_previous_context_database_default(
    tmp_path, monkeypatch
):
    _configure_implicit_kuzu_fallback(monkeypatch)
    server, context_path = _make_switchable_server(tmp_path, monkeypatch)
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda **_kwargs: {
            "DEFAULT_DATABASE": os.environ.get("DEFAULT_DATABASE") or "ladybugdb"
        },
    )
    monkeypatch.setenv("DEFAULT_DATABASE", "neo4j")
    monkeypatch.setenv("CGC_DB_SELECTION_SOURCE", "context (active-repo)")
    server.resolved_context = ResolvedContext(
        mode="per-repo",
        context_name="",
        database="neo4j",
        db_path="/active-repo/.codegraphcontext/db/neo4j",
        cgcignore_path="",
        is_local=True,
    )
    server._clear_context_default_database_on_global_restore = True
    factory_calls = []
    fail_first_attempt = True

    def make_manager(db_path):
        nonlocal fail_first_attempt
        factory_calls.append((db_path, os.environ.get("DEFAULT_DATABASE")))
        if fail_first_attempt:
            fail_first_attempt = False
            raise RuntimeError("target initialization failed")
        return core.get_database_manager(db_path=db_path)

    monkeypatch.setattr(server_module, "get_database_manager", make_manager)

    failed = server._switch_context_tool_locked(context_path=str(context_path))
    assert "error" in failed
    assert os.environ["DEFAULT_DATABASE"] == "neo4j"

    result = server._switch_context_tool_locked(context_path=str(context_path))

    assert result.get("status") == "ok", result
    assert result["database"] == "kuzudb"
    assert result["db_path"] == str(context_path / "db" / "kuzudb")
    assert server.resolved_context.database == "kuzudb"
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "kuzudb"
    assert "DEFAULT_DATABASE" not in os.environ
    assert factory_calls == [
        (str(context_path / "db" / "ladybugdb"), None),
        (str(context_path / "db" / "ladybugdb"), None),
    ]


def test_default_factory_prefers_ladybug_when_optional_backends_are_installed(
    monkeypatch,
):
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "DEFAULT_DATABASE",
        "FALKORDB_HOST",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NORNIC_URI",
        "NORNIC_USERNAME",
        "NORNIC_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(core, "_is_falkordb_remote_configured", lambda: False)
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: True)
    monkeypatch.setattr(core, "is_falkordb_usable", lambda: True)
    monkeypatch.setattr(core, "_is_kuzudb_available", lambda: True)

    selected = []
    ladybug = type(sys)("codegraphcontext.core.database_ladybug")
    ladybug.LadybugDBManager = lambda db_path=None: selected.append("ladybugdb") or object()
    monkeypatch.setitem(sys.modules, ladybug.__name__, ladybug)

    falkor = type(sys)("codegraphcontext.core.database_falkordb")
    falkor.FalkorDBManager = lambda **_kwargs: selected.append("falkordb") or object()
    falkor.FalkorDBUnavailableError = RuntimeError
    monkeypatch.setitem(sys.modules, falkor.__name__, falkor)

    kuzu = type(sys)("codegraphcontext.core.database_kuzu")
    kuzu.KuzuDBManager = lambda **_kwargs: selected.append("kuzudb") or object()
    monkeypatch.setitem(sys.modules, kuzu.__name__, kuzu)

    manager = core.get_database_manager()

    assert manager is not None
    assert selected == ["ladybugdb"]
    assert core._default_embedded_backend_name() == "ladybugdb"


def test_default_factory_falls_back_to_optional_backend_if_ladybug_unavailable(
    monkeypatch,
):
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "DEFAULT_DATABASE",
        "FALKORDB_HOST",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NORNIC_URI",
        "NORNIC_USERNAME",
        "NORNIC_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(core, "_is_falkordb_remote_configured", lambda: False)
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: False)
    monkeypatch.setattr(core, "is_falkordb_usable", lambda: False)
    monkeypatch.setattr(core, "_is_kuzudb_available", lambda: True)

    selected = []
    kuzu = type(sys)("codegraphcontext.core.database_kuzu")
    kuzu.KuzuDBManager = lambda db_path=None: selected.append("kuzudb") or object()
    monkeypatch.setitem(sys.modules, kuzu.__name__, kuzu)

    manager = core.get_database_manager()

    assert manager is not None
    assert selected == ["kuzudb"]
    assert core._default_embedded_backend_name() == "kuzudb"


@pytest.mark.parametrize("backend", ("ladybugdb", "kuzudb"))
def test_implicit_factory_preserves_exact_runtime_database_path(monkeypatch, backend):
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "DEFAULT_DATABASE",
        "FALKORDB_HOST",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NORNIC_URI",
        "NORNIC_USERNAME",
        "NORNIC_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    runtime_path = "/runtime/db/falkordb"
    monkeypatch.setenv("CGC_RUNTIME_DB_PATH", runtime_path)
    monkeypatch.setattr(core, "_is_falkordb_remote_configured", lambda: False)
    monkeypatch.setattr(core, "is_falkordb_usable", lambda: False)
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: backend == "ladybugdb")
    monkeypatch.setattr(core, "_is_kuzudb_available", lambda: backend == "kuzudb")

    selected_paths = []
    module_name = f"codegraphcontext.core.database_{backend.removesuffix('db')}"
    manager_name = "LadybugDBManager" if backend == "ladybugdb" else "KuzuDBManager"
    fake = type(sys)(module_name)
    setattr(fake, manager_name, lambda db_path=None: selected_paths.append(db_path) or object())
    monkeypatch.setitem(sys.modules, module_name, fake)

    core.get_database_manager(db_path=runtime_path)

    assert selected_paths == [runtime_path]


def test_default_global_ladybug_path_honors_config_and_runtime_override(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    profile.mkdir()
    configured_path = profile / "storage" / "ladybug"
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(
        config_manager,
        "load_config",
        lambda **_kwargs: {"LADYBUGDB_PATH": str(configured_path)},
    )
    monkeypatch.delenv("CGC_RUNTIME_DB_PATH", raising=False)

    assert config_manager._default_global_db_path("ladybugdb") == str(configured_path)

    runtime_path = tmp_path / "runtime" / "custom-db"
    monkeypatch.setenv("CGC_RUNTIME_DB_PATH", str(runtime_path))
    assert config_manager._default_global_db_path("ladybugdb") == str(runtime_path)


def test_default_global_ladybug_path_rejects_path_outside_profile(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(
        config_manager,
        "load_config",
        lambda **_kwargs: {"LADYBUGDB_PATH": str(tmp_path / "other-profile" / "db")},
    )
    monkeypatch.delenv("CGC_RUNTIME_DB_PATH", raising=False)

    assert config_manager._default_global_db_path("ladybugdb") == str(
        profile / "global" / "db" / "ladybugdb"
    )


def _make_switchable_server(tmp_path, monkeypatch, *, global_backend="ladybugdb"):
    context_path = tmp_path / ".codegraphcontext"
    context_path.mkdir()
    monkeypatch.setattr(server_module, "get_repo_context_dir_name", lambda: context_path.name)
    monkeypatch.setattr(server_module, "_default_global_db_path", lambda database: f"/global/{database}")
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda **_kwargs: {"DEFAULT_DATABASE": global_backend},
    )
    monkeypatch.setattr("codegraphcontext.utils.path_sandbox.is_path_allowed", lambda _path: True)
    monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())

    server = MCPServer.__new__(MCPServer)
    server.cwd = tmp_path
    server._runtime_db_type_before_context_switch = os.environ.get("CGC_RUNTIME_DB_TYPE")
    startup_database = os.environ.get("CGC_RUNTIME_DB_TYPE") or global_backend
    server.resolved_context = ResolvedContext(
        mode="global",
        context_name="",
        database=startup_database,
        db_path=f"/global/{startup_database}",
        cgcignore_path="",
    )
    server._startup_global_context = ResolvedContext(
        mode="global",
        context_name="",
        database=startup_database,
        db_path=f"/global/{startup_database}",
        cgcignore_path="",
    )
    server.db_manager = object()
    server.job_manager = object()
    server.loop = None
    server._active_jobs_block_context_switch = lambda: None
    server._stop_current_watcher = lambda: False
    server._close_db_manager_for_context_switch = lambda _watcher_was_running: None
    server._start_watcher_if = lambda _watcher_was_running: None
    server._context_note_pending = False
    return server, context_path


class _SwitchTestManager:
    def __init__(self, backend, db_path):
        self.backend = backend
        self.db_path = db_path

    def get_driver(self):
        return None

    def get_backend_type(self):
        return self.backend


def test_path_context_switch_preserves_explicit_workspace_mapping(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    profile.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    context_path = repo / ".codegraphcontext"
    context_path.mkdir()
    context_config = profile / "config.yaml"
    context_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mode": "per-repo",
                "workspace_mappings": {
                    str(repo.resolve()): {
                        "context_path": str(context_path.resolve()),
                        "database": "falkordb",
                        "database_is_explicit": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    server, _unused_context_path = _make_switchable_server(tmp_path, monkeypatch)
    server.cwd = repo
    monkeypatch.setattr(
        server_module, "load_config", lambda **_kwargs: {"DEFAULT_DATABASE": "kuzudb"}
    )
    calls = []

    def make_manager(db_path):
        backend = os.environ.get("CGC_RUNTIME_DB_TYPE") or "ladybugdb"
        calls.append((str(db_path), backend))
        return _SwitchTestManager(backend, str(db_path))

    monkeypatch.setattr(server_module, "get_database_manager", make_manager)

    result = server._switch_context_tool_locked(context_path=str(context_path))

    assert result["status"] == "ok"
    assert result["database"] == "falkordb"
    assert result["db_path"] == str(context_path / "db" / "falkordb")
    assert calls == [(str(context_path / "db" / "falkordb"), "falkordb")]
    assert server.resolved_context.database_is_explicit is True
    assert config_manager.get_workspace_mapping(repo) == {
        "context_path": str(context_path),
        "database": "falkordb",
        "database_is_explicit": True,
    }


@pytest.mark.parametrize(
    ("database_is_explicit", "expected_database"),
    [(False, "kuzudb"), (True, "ladybugdb")],
)
def test_path_context_switch_reuses_existing_implicit_workspace_fallback_storage(
    tmp_path, monkeypatch, database_is_explicit, expected_database
):
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)
    profile = tmp_path / "profile"
    profile.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    context_path = tmp_path / "saved-context" / ".codegraphcontext"
    context_path.mkdir(parents=True)
    fallback_path = context_path / "db" / "kuzudb"
    fallback_path.mkdir(parents=True)
    context_config = profile / "config.yaml"
    context_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mode": "per-repo",
                "workspace_mappings": {
                    str(repo.resolve()): {
                        "context_path": str(context_path.resolve()),
                        "database": "ladybugdb",
                        "database_is_explicit": database_is_explicit,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    server, _unused_context_path = _make_switchable_server(tmp_path, monkeypatch)
    server.cwd = repo
    expected_db_path = context_path / "db" / expected_database
    manager = _SwitchTestManager(expected_database, str(expected_db_path))
    requested = []
    monkeypatch.setattr(
        server_module,
        "get_database_manager",
        lambda db_path: requested.append(str(db_path)) or manager,
    )

    result = server._switch_context_tool_locked(context_path=str(context_path))

    assert result.get("status") == "ok", result
    assert result["database"] == expected_database
    assert result["db_path"] == str(expected_db_path)
    assert requested == [str(expected_db_path)]
    assert server.resolved_context.database == expected_database
    assert server.resolved_context.db_path == str(expected_db_path)
    assert server.resolved_context.database_is_explicit is database_is_explicit


def test_path_context_switch_uses_existing_kuzudb_db_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)
    server, context_path = _make_switchable_server(tmp_path, monkeypatch)
    legacy_db_path = context_path / "db" / "kuzudb.db"
    legacy_db_path.parent.mkdir(parents=True)
    legacy_db_path.write_bytes(b"legacy Kuzu database")
    manager = _SwitchTestManager("kuzudb", str(legacy_db_path))
    requested = []

    monkeypatch.setattr(
        server_module,
        "get_database_manager",
        lambda db_path: requested.append(db_path) or manager,
    )

    result = server._switch_context_tool_locked(context_path=str(context_path))

    assert result["status"] == "ok"
    assert requested == [str(legacy_db_path)]
    assert server.resolved_context.database == "kuzudb"
    assert server.resolved_context.db_path == str(legacy_db_path)


def test_switching_back_to_global_restores_startup_runtime_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "neo4j")
    server, context_path = _make_switchable_server(tmp_path, monkeypatch)
    local_path = context_path / "db" / "ladybugdb"
    managers = {
        str(local_path): _SwitchTestManager("ladybugdb", str(local_path)),
        "/global/neo4j": _SwitchTestManager("neo4j", "/global/neo4j"),
    }
    factory_calls = []

    def make_manager(db_path):
        factory_calls.append((db_path, os.environ.get("CGC_RUNTIME_DB_TYPE")))
        return managers[db_path]

    monkeypatch.setattr(server_module, "get_database_manager", make_manager)

    into_context = server._switch_context_tool_locked(
        context_path=str(context_path), save=False
    )
    back_to_global = server._switch_context_tool_locked(context_path="global")

    assert into_context["status"] == "ok"
    assert back_to_global["status"] == "ok"
    assert factory_calls == [
        (str(local_path), "ladybugdb"),
        ("/global/neo4j", "neo4j"),
    ]
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "neo4j"
    assert server.resolved_context.mode == "global"
    assert server.resolved_context.database == "neo4j"


def test_switching_back_to_global_restores_an_unset_runtime_override(tmp_path, monkeypatch):
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    server, context_path = _make_switchable_server(tmp_path, monkeypatch)
    local_path = context_path / "db" / "ladybugdb"
    managers = {
        str(local_path): _SwitchTestManager("ladybugdb", str(local_path)),
        "/global/ladybugdb": _SwitchTestManager("ladybugdb", "/global/ladybugdb"),
    }
    factory_calls = []

    def make_manager(db_path):
        factory_calls.append((db_path, os.environ.get("CGC_RUNTIME_DB_TYPE")))
        return managers[db_path]

    monkeypatch.setattr(server_module, "get_database_manager", make_manager)

    into_context = server._switch_context_tool_locked(
        context_path=str(context_path), save=False
    )
    back_to_global = server._switch_context_tool_locked(context_path="global")

    assert into_context["status"] == "ok"
    assert back_to_global["status"] == "ok"
    assert factory_calls == [
        (str(local_path), None),
        ("/global/ladybugdb", None),
    ]
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "ladybugdb"


def test_returning_to_global_uses_configured_backend_before_factory(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)
    server, context_path = _make_switchable_server(
        tmp_path, monkeypatch, global_backend="falkordb"
    )
    (context_path / "config.yaml").write_text("database: kuzudb\n", encoding="utf-8")
    local_path = context_path / "db" / "kuzudb"
    managers = {
        str(local_path): _SwitchTestManager("kuzudb", str(local_path)),
        "/global/falkordb": _SwitchTestManager("falkordb", "/global/falkordb"),
    }
    factory_calls = []

    def make_manager(db_path):
        factory_calls.append((db_path, os.environ.get("CGC_RUNTIME_DB_TYPE")))
        return managers[db_path]

    monkeypatch.setattr(server_module, "get_database_manager", make_manager)

    into_context = server._switch_context_tool_locked(
        context_path=str(context_path), save=False
    )
    back_to_global = server._switch_context_tool_locked(context_path="global")

    assert into_context["status"] == "ok"
    assert back_to_global["status"] == "ok"
    assert factory_calls == [
        (str(local_path), "kuzudb"),
        ("/global/falkordb", "falkordb"),
    ]
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "falkordb"


def test_switching_back_to_global_restores_startup_resolved_backend_and_path(
    tmp_path, monkeypatch
):
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "CGC_RUNTIME_DB_PATH",
        "DEFAULT_DATABASE",
    ):
        monkeypatch.delenv(name, raising=False)
    context_path = tmp_path / ".codegraphcontext"
    context_path.mkdir()
    global_db_path = "/global/custom/falkordb"
    startup_context = ResolvedContext(
        mode="global",
        context_name="",
        database="falkordb",
        db_path=global_db_path,
        cgcignore_path="",
    )
    local_path = context_path / "db" / "ladybugdb"
    managers = {
        global_db_path: _SwitchTestManager("falkordb", global_db_path),
        str(local_path): _SwitchTestManager("ladybugdb", str(local_path)),
    }
    factory_calls = []

    def make_manager(db_path):
        factory_calls.append((db_path, os.environ.get("CGC_RUNTIME_DB_TYPE")))
        return managers[db_path]

    monkeypatch.setattr(server_module, "resolve_context", lambda **_kwargs: startup_context)
    monkeypatch.setattr(server_module, "discover_child_contexts", lambda *_a, **_k: [])
    monkeypatch.setattr(server_module, "get_database_manager", make_manager)
    monkeypatch.setattr(server_module, "get_repo_context_dir_name", lambda: context_path.name)
    monkeypatch.setattr(
        server_module, "load_config", lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"}
    )
    monkeypatch.setattr("codegraphcontext.utils.path_sandbox.is_path_allowed", lambda _path: True)
    monkeypatch.setattr(server_module, "JobManager", lambda: object())
    monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module.MCPServer, "_init_tools", lambda _self: None)

    server = MCPServer(loop=object(), cwd=tmp_path, initialize_database=False)
    assert server._startup_global_context.database == "falkordb"
    assert server._startup_global_context.db_path == global_db_path
    factory_calls.clear()
    server._active_jobs_block_context_switch = lambda: None
    server._stop_current_watcher = lambda: False
    server._close_db_manager_for_context_switch = lambda _watcher_was_running: None
    server._start_watcher_if = lambda _watcher_was_running: None

    into_context = server._switch_context_tool_locked(
        context_path=str(context_path), save=False
    )
    back_to_global = server._switch_context_tool_locked(context_path="global")

    assert into_context["status"] == "ok"
    assert back_to_global["status"] == "ok"
    assert factory_calls == [
        (str(local_path), None),
        (global_db_path, "falkordb"),
    ]
    assert back_to_global["database"] == "falkordb"
    assert back_to_global["db_path"] == global_db_path
    assert server.resolved_context.database == "falkordb"
    assert server.resolved_context.db_path == global_db_path
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "falkordb"


def test_per_repo_mcp_startup_restores_inferred_legacy_global_backend(
    tmp_path, monkeypatch
):
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "CGC_RUNTIME_DB_PATH",
        "DEFAULT_DATABASE",
        "CGC_DB_SELECTION_SOURCE",
    ):
        monkeypatch.delenv(name, raising=False)

    profile = tmp_path / "profile"
    legacy_global_path = profile / "global" / "db" / "falkordb"
    legacy_global_path.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    context_path = repo / ".codegraphcontext"
    context_path.mkdir()
    local_db_path = context_path / "db" / "ladybugdb"
    local_db_path.mkdir(parents=True)

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(
        config_manager, "CONTEXT_CONFIG_FILE", profile / "config.yaml"
    )
    monkeypatch.setattr(
        config_manager,
        "_LEGACY_CONTEXT_CONFIG_FILE",
        profile / "cgc_config.yaml",
    )
    monkeypatch.setattr(config_manager, "find_local_env", lambda **_kwargs: None)
    monkeypatch.setattr(
        config_manager,
        "_has_explicit_database_default",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        config_manager,
        "load_config",
        lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"},
    )
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"},
    )
    per_repo_context = ResolvedContext(
        mode="per-repo",
        context_name="",
        database="ladybugdb",
        db_path=str(local_db_path),
        cgcignore_path=str(context_path / ".cgcignore"),
        is_local=True,
    )
    monkeypatch.setattr(
        server_module, "resolve_context", lambda **_kwargs: per_repo_context
    )
    monkeypatch.setenv("DEFAULT_DATABASE", "ladybugdb")
    monkeypatch.setenv("CGC_DB_SELECTION_SOURCE", "context (repo)")

    manager_calls = []

    class Manager:
        def __init__(self, backend, db_path):
            self.backend = backend
            self.db_path = db_path

        def get_backend_type(self):
            return self.backend

        def get_driver(self):
            return None

    def make_manager(db_path):
        manager_calls.append((db_path, os.environ.get("CGC_RUNTIME_DB_TYPE")))
        backend = "falkordb" if db_path == str(legacy_global_path) else "ladybugdb"
        return Manager(backend, db_path)

    monkeypatch.setattr(server_module, "get_database_manager", make_manager)
    monkeypatch.setattr(server_module, "JobManager", lambda: object())
    monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module.MCPServer, "_init_tools", lambda _self: None)

    server = MCPServer(loop=object(), cwd=repo, initialize_database=False)
    assert server.resolved_context.mode == "per-repo"
    assert server._startup_global_context.database == "falkordb"
    assert server._startup_global_context.db_path == str(legacy_global_path)
    server._active_jobs_block_context_switch = lambda: None
    server._stop_current_watcher = lambda: False
    server._close_db_manager_for_context_switch = lambda _was_running: None
    server._start_watcher_if = lambda _was_running: None

    result = server._switch_context_tool_locked(context_path="global")

    assert result.get("status") == "ok", result
    assert manager_calls == [
        (str(local_db_path), "ladybugdb"),
        (str(legacy_global_path), "falkordb"),
    ]
    assert result["database"] == "falkordb"
    assert server.resolved_context.db_path == str(legacy_global_path)


def test_per_repo_backend_default_does_not_leak_into_implicit_global_ladybug(
    tmp_path, monkeypatch
):
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "CGC_RUNTIME_DB_PATH",
        "DEFAULT_DATABASE",
        "CGC_DB_SELECTION_SOURCE",
    ):
        monkeypatch.delenv(name, raising=False)

    profile = tmp_path / "profile"
    repo = tmp_path / "repo"
    local_context_path = repo / ".codegraphcontext"
    local_db_path = local_context_path / "db" / "falkordb"
    local_db_path.mkdir(parents=True)
    repo.mkdir(exist_ok=True)

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(
        config_manager, "CONTEXT_CONFIG_FILE", profile / "config.yaml"
    )
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_env", lambda **_kwargs: None)
    monkeypatch.setattr(
        config_manager, "_has_explicit_database_default", lambda **_kwargs: False
    )
    monkeypatch.setattr(
        config_manager,
        "load_config",
        lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"},
    )
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"},
    )
    monkeypatch.setattr(
        server_module,
        "_default_global_db_path",
        lambda database, **_kwargs: f"/global/{database}",
    )
    per_repo_context = ResolvedContext(
        mode="per-repo",
        context_name="",
        database="falkordb",
        db_path=str(local_db_path),
        cgcignore_path=str(local_context_path / ".cgcignore"),
        is_local=True,
    )
    monkeypatch.setattr(
        server_module, "resolve_context", lambda **_kwargs: per_repo_context
    )
    # CLI credential loading sets these to the selected repository backend.
    monkeypatch.setenv("DEFAULT_DATABASE", "falkordb")
    monkeypatch.setenv("CGC_DB_SELECTION_SOURCE", "context (repo)")

    manager_calls = []

    class Manager:
        def __init__(self, backend, db_path):
            self.backend = backend
            self.db_path = db_path

        def get_backend_type(self):
            return self.backend

        def get_driver(self):
            return None

    fail_global_manager = True

    def make_manager(db_path):
        backend = (
            os.environ.get("CGC_RUNTIME_DB_TYPE")
            or os.environ.get("DEFAULT_DATABASE")
            or "ladybugdb"
        )
        manager_calls.append((db_path, backend))
        if db_path == "/global/ladybugdb" and fail_global_manager:
            raise RuntimeError("global database unavailable")
        return Manager(backend, db_path)

    monkeypatch.setattr(server_module, "get_database_manager", make_manager)
    monkeypatch.setattr(server_module, "JobManager", lambda: object())
    monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module.MCPServer, "_init_tools", lambda _self: None)

    server = MCPServer(loop=object(), cwd=repo, initialize_database=False)
    assert server.resolved_context.database == "falkordb"
    assert server._startup_global_context.database == "ladybugdb"

    server._active_jobs_block_context_switch = lambda: None
    server._stop_current_watcher = lambda: False
    server._close_db_manager_for_context_switch = lambda _was_running: None
    server._start_watcher_if = lambda _was_running: None
    failed_restore = server._switch_context_tool_locked(context_path="global")
    assert "error" in failed_restore
    assert os.environ["DEFAULT_DATABASE"] == "falkordb"
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "falkordb"

    fail_global_manager = False
    result = server._switch_context_tool_locked(context_path="global")

    assert result.get("status") == "ok", result
    assert manager_calls == [
        (str(local_db_path), "falkordb"),
        ("/global/ladybugdb", "ladybugdb"),
        ("/global/ladybugdb", "ladybugdb"),
    ]
    assert result["database"] == "ladybugdb"
    assert server.resolved_context.db_path == "/global/ladybugdb"
    assert "DEFAULT_DATABASE" not in os.environ


def test_global_context_restore_keeps_implicit_remote_falkordb(tmp_path, monkeypatch):
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "CGC_RUNTIME_DB_PATH",
        "DEFAULT_DATABASE",
        "CGC_DB_SELECTION_SOURCE",
    ):
        monkeypatch.delenv(name, raising=False)
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(
        config_manager, "_LEGACY_FALKORDB_PATH", profile / "global" / "falkordb.db"
    )
    monkeypatch.setenv("FALKORDB_HOST", "graph.example")
    monkeypatch.setattr(
        config_manager,
        "_has_explicit_database_default",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        config_manager,
        "load_config",
        lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"},
    )
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"},
    )

    resolved = server_module._resolve_global_context_for_restore()

    assert resolved.database == "falkordb-remote"


def _load_project_env_for_global_restore(
    tmp_path, monkeypatch, contents, *, global_contents=None
):
    from codegraphcontext.cli import main as cli_main

    profile = tmp_path / "profile"
    profile.mkdir()
    if global_contents is not None:
        (profile / ".env").write_text(global_contents, encoding="utf-8")
    repo = tmp_path / "repo"
    project_context = repo / ".codegraphcontext"
    project_context.mkdir(parents=True)
    project_env = project_context / ".env"
    project_env.write_text(contents, encoding="utf-8")
    monkeypatch.chdir(repo)

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(server_module, "CONFIG_DIR", profile)

    def should_apply_project_env(**_kwargs):
        return os.getenv("CGC_IGNORE_PROJECT_ENV") not in ("1", "true", "yes")

    monkeypatch.setattr(
        config_manager, "should_apply_project_dotenv", should_apply_project_env
    )
    monkeypatch.setattr(
        config_manager,
        "codegraphcontext_dotenv_at_cwd",
        lambda _cwd=None: project_env,
    )
    monkeypatch.setattr(
        config_manager,
        "resolve_context",
        lambda *_args, **_kwargs: ResolvedContext(
            mode="global",
            context_name="",
            database="ladybugdb",
            db_path=str(profile / "global" / "db" / "ladybugdb"),
            cgcignore_path="",
        ),
    )
    for name in (
        "CGC_IGNORE_PROJECT_ENV",
        "CGC_LOAD_PROJECT_ENV",
        "CGC_RUNTIME_DB_TYPE",
        "CGC_RUNTIME_DB_PATH",
        "CGC_SELECTED_DATABASE",
        "CGC_DB_SELECTION_SOURCE",
        "DATABASE_TYPE",
        "DEFAULT_DATABASE",
        "FALKORDB_HOST",
    ):
        # Record an absent variable before deleting it so direct assignments
        # made by `_load_credentials()` are removed when the fixture unwinds.
        monkeypatch.setenv(name, "__test_value_to_remove__")
        monkeypatch.delenv(name, raising=False)

    cli_main._load_credentials(read_only=True)


def _switch_to_global_with_environment_aware_factory(
    tmp_path, monkeypatch, *, expected_backend="ladybugdb", expected_host=None
):
    profile = tmp_path / "profile"
    global_db_path = profile / "global" / "db" / expected_backend
    manager_calls = []

    def make_manager(db_path):
        backend = (
            os.environ.get("CGC_RUNTIME_DB_TYPE")
            or os.environ.get("DEFAULT_DATABASE")
            or ("falkordb-remote" if os.environ.get("FALKORDB_HOST") else "ladybugdb")
        )
        manager_calls.append(
            (
                db_path,
                backend,
                os.environ.get("FALKORDB_HOST"),
                os.environ.get("DEFAULT_DATABASE"),
            )
        )
        return _SwitchTestManager(backend, str(db_path))

    monkeypatch.setattr(server_module, "get_database_manager", make_manager)
    monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())

    server = MCPServer.__new__(MCPServer)
    server.cwd = Path.cwd()
    server.resolved_context = ResolvedContext(
        mode="per-repo",
        context_name="",
        database="falkordb",
        db_path="/repo/db/falkordb",
        cgcignore_path="",
    )
    server.db_manager = object()
    server.job_manager = object()
    server.loop = None
    server._runtime_db_type_before_context_switch = None
    server._startup_global_context = None
    server._clear_context_default_database_on_global_restore = False
    server._active_jobs_block_context_switch = lambda: None
    server._stop_current_watcher = lambda: False
    server._close_db_manager_for_context_switch = lambda _watcher_was_running: None
    server._start_watcher_if = lambda _watcher_was_running: None
    server._context_note_pending = False

    result = server._switch_context_tool_locked(context_path="global")

    assert result.get("status") == "ok", result
    assert result["database"] == expected_backend
    assert result["db_path"] == str(global_db_path)
    assert manager_calls == [
        (str(global_db_path), expected_backend, expected_host, None)
    ]
    return result


@pytest.mark.parametrize(
    "project_environment",
    (
        "CGC_RUNTIME_DB_TYPE=falkordb\n",
        "CGC_RUNTIME_DB_PATH=/repo/project-db\n",
        "CGC_RUNTIME_DB_TYPE=falkordb\nCGC_RUNTIME_DB_PATH=/repo/project-db\n",
    ),
)
def test_global_context_restore_ignores_project_dotenv_runtime_overrides(
    tmp_path, monkeypatch, project_environment
):
    _load_project_env_for_global_restore(
        tmp_path, monkeypatch, project_environment
    )
    repo = Path.cwd()
    context_path = repo / ".codegraphcontext"
    local_context = ResolvedContext(
        mode="per-repo",
        context_name="",
        database="ladybugdb",
        db_path=str(context_path / "db" / "ladybugdb"),
        cgcignore_path="",
        is_local=True,
    )
    manager_calls = []

    def make_manager(db_path):
        backend = os.environ.get("CGC_RUNTIME_DB_TYPE") or os.environ.get(
            "DEFAULT_DATABASE", "ladybugdb"
        )
        manager_calls.append((str(db_path), backend))
        return _SwitchTestManager(backend, str(db_path))

    monkeypatch.setattr(server_module, "resolve_context", lambda **_kwargs: local_context)
    monkeypatch.setattr(server_module, "get_database_manager", make_manager)
    monkeypatch.setattr(server_module, "JobManager", lambda: object())
    monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module.MCPServer, "_init_tools", lambda _self: None)

    server = MCPServer(cwd=repo, initialize_database=False)
    server._active_jobs_block_context_switch = lambda: None
    server._stop_current_watcher = lambda: False
    server._close_db_manager_for_context_switch = lambda _watcher_was_running: None
    server._start_watcher_if = lambda _watcher_was_running: None
    manager_calls.clear()

    result = server._switch_context_tool_locked(context_path="global")

    expected_global_path = str(
        config_manager.CONFIG_DIR / "global" / "db" / "ladybugdb"
    )
    assert result.get("status") == "ok", result
    assert manager_calls == [(expected_global_path, "ladybugdb")]
    assert result["database"] == "ladybugdb"
    assert result["db_path"] == expected_global_path


def test_global_context_restore_ignores_project_dotenv_falkordb_host(
    tmp_path, monkeypatch
):
    _load_project_env_for_global_restore(
        tmp_path, monkeypatch, "FALKORDB_HOST=project-graph.example\n"
    )

    assert os.environ["FALKORDB_HOST"] == "project-graph.example"
    _switch_to_global_with_environment_aware_factory(tmp_path, monkeypatch)

    assert os.environ["FALKORDB_HOST"] == "project-graph.example"


def test_global_context_restore_ignores_project_dotenv_default_database(
    tmp_path, monkeypatch
):
    _load_project_env_for_global_restore(
        tmp_path, monkeypatch, "DEFAULT_DATABASE=falkordb\n"
    )

    assert os.environ["DEFAULT_DATABASE"] == "falkordb"
    _switch_to_global_with_environment_aware_factory(tmp_path, monkeypatch)

    assert os.environ["DEFAULT_DATABASE"] == "falkordb"


def test_global_context_restore_uses_global_host_when_project_host_overrides_it(
    tmp_path, monkeypatch
):
    _load_project_env_for_global_restore(
        tmp_path,
        monkeypatch,
        "FALKORDB_HOST=project-graph.example\n",
        global_contents="FALKORDB_HOST=global-graph.example\n",
    )

    _switch_to_global_with_environment_aware_factory(
        tmp_path,
        monkeypatch,
        expected_backend="falkordb-remote",
        expected_host="global-graph.example",
    )

    assert os.environ["FALKORDB_HOST"] == "project-graph.example"


def test_auto_created_repo_context_keeps_default_backend_implicit(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", profile / "config.yaml")
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )

    repo = tmp_path / "repo"
    repo.mkdir()

    assert config_manager.register_repo_in_context(
        "auto-created", str(repo), auto_create=True
    )
    context = config_manager.load_context_config().contexts["auto-created"]

    assert context.database == "ladybugdb"
    assert context.database_is_explicit is False


def test_failed_context_initialization_restores_previous_runtime_override(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "neo4j")
    server, context_path = _make_switchable_server(tmp_path, monkeypatch)

    class FailingManager:
        def get_driver(self):
            raise RuntimeError("driver initialization failed")

    monkeypatch.setattr(server_module, "get_database_manager", lambda _db_path: FailingManager())

    result = server._switch_context_tool_locked(
        context_path=str(context_path), save=False
    )

    assert "error" in result
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "neo4j"


def test_failed_global_initialization_restores_context_runtime_backend(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "neo4j")
    server, context_path = _make_switchable_server(tmp_path, monkeypatch)
    local_path = context_path / "db" / "ladybugdb"
    local_manager = _SwitchTestManager("ladybugdb", str(local_path))
    factory_calls = 0

    def make_manager(db_path):
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            return local_manager
        raise RuntimeError("global driver initialization failed")

    monkeypatch.setattr(server_module, "get_database_manager", make_manager)
    into_context = server._switch_context_tool_locked(
        context_path=str(context_path), save=False
    )
    back_to_global = server._switch_context_tool_locked(context_path="global")

    assert into_context.get("status") == "ok", into_context
    assert "error" in back_to_global
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "ladybugdb"


def test_per_repo_context_infers_existing_legacy_kuzudb_storage(tmp_path, monkeypatch):
    context_config = tmp_path / "config.yaml"
    context_config.write_text("mode: per-repo\n", encoding="utf-8")
    repo = tmp_path / "repo"
    context_path = repo / ".codegraphcontext"
    kuzu_path = context_path / "db" / "kuzudb"
    kuzu_path.mkdir(parents=True)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path / "global")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager,
        "_LEGACY_CONTEXT_CONFIG_FILE",
        tmp_path / "legacy-config.yaml",
    )
    monkeypatch.setattr(
        config_manager,
        "get_repo_context_path",
        lambda root: root / ".codegraphcontext",
    )
    monkeypatch.setattr(
        config_manager,
        "load_config",
        lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"},
    )
    for name in ("CGC_RUNTIME_DB_TYPE", "DEFAULT_DATABASE"):
        monkeypatch.delenv(name, raising=False)

    resolved = config_manager.resolve_context(
        cwd=repo, create_local_context=False, read_only=True
    )

    assert resolved.database == "kuzudb"
    assert resolved.db_path == str(kuzu_path)


@pytest.mark.parametrize("mode", ("named", "per-repo"))
def test_resolve_context_marks_explicit_ladybug_backend(mode, tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    config_path = profile / "config.yaml"
    repo = tmp_path / "repo"
    repo.mkdir()
    local_context = repo / ".codegraphcontext"
    local_context.mkdir()

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", config_path)
    monkeypatch.setattr(
        config_manager,
        "_LEGACY_CONTEXT_CONFIG_FILE",
        profile / "cgc_config.yaml",
    )
    monkeypatch.setattr(
        config_manager,
        "CONFIG_FILE",
        profile / ".env",
    )
    monkeypatch.setattr(
        config_manager,
        "get_repo_context_path",
        lambda _root: local_context,
    )
    monkeypatch.setattr(
        config_manager,
        "load_config",
        lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"},
    )

    if mode == "named":
        config_path.write_text(
            "version: 1\n"
            "mode: named\n"
            "default_context: project\n"
            "contexts:\n"
            "  project:\n"
            "    database: ladybugdb\n"
            "    db_path: /project/db/ladybugdb\n",
            encoding="utf-8",
        )
    else:
        config_path.write_text("version: 1\nmode: per-repo\n", encoding="utf-8")
        (local_context / "config.yaml").write_text(
            "mode: per-repo\ndatabase: ladybugdb\n", encoding="utf-8"
        )

    resolved = config_manager.resolve_context(
        cwd=repo, create_local_context=False, read_only=True
    )

    assert resolved.database == "ladybugdb"
    assert getattr(resolved, "database_is_explicit", False) is True


@pytest.mark.parametrize("mode", ("named", "per-repo"))
def test_mcp_startup_preserves_explicit_ladybug_context_with_remote_host(
    mode, tmp_path, monkeypatch
):
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "CGC_RUNTIME_DB_PATH",
        "DEFAULT_DATABASE",
        "CGC_DB_SELECTION_SOURCE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FALKORDB_HOST", "graph.example")
    context = ResolvedContext(
        mode=mode,
        context_name="project" if mode == "named" else "",
        database="ladybugdb",
        db_path=f"/{mode}/db/ladybugdb",
        cgcignore_path="",
        is_local=mode == "per-repo",
    )
    context.database_is_explicit = True
    factory_calls = []

    def make_manager(db_path):
        backend = (
            os.environ.get("CGC_RUNTIME_DB_TYPE")
            or os.environ.get("DEFAULT_DATABASE")
            or ("falkordb-remote" if os.environ.get("FALKORDB_HOST") else "ladybugdb")
        )
        factory_calls.append((db_path, backend))
        return _SwitchTestManager(backend, db_path)

    monkeypatch.setattr(server_module, "resolve_context", lambda **_kwargs: context)
    monkeypatch.setattr(
        server_module,
        "_resolve_global_context_for_restore",
        lambda: ResolvedContext(
            mode="global",
            context_name="",
            database="ladybugdb",
            db_path="/global/db/ladybugdb",
            cgcignore_path="",
        ),
    )
    monkeypatch.setattr(server_module, "get_database_manager", make_manager)
    monkeypatch.setattr(server_module, "JobManager", lambda: object())
    monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module.MCPServer, "_init_tools", lambda _self: None)

    server = MCPServer(loop=object(), cwd=tmp_path, initialize_database=False)

    assert factory_calls == [(context.db_path, "ladybugdb")]
    assert server.resolved_context.database == "ladybugdb"


@pytest.mark.parametrize("mode", ("named", "per-repo"))
def test_cli_initialization_preserves_explicit_ladybug_context_with_remote_host(
    mode, tmp_path, monkeypatch
):
    for name in ("CGC_RUNTIME_DB_TYPE", "CGC_RUNTIME_DB_PATH", "DEFAULT_DATABASE"):
        monkeypatch.setenv(name, "__test_value_to_remove__")
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FALKORDB_HOST", "graph.example")
    context = ResolvedContext(
        mode=mode,
        context_name="project" if mode == "named" else "",
        database="ladybugdb",
        db_path=f"/{mode}/db/ladybugdb",
        cgcignore_path="",
        is_local=mode == "per-repo",
    )
    context.database_is_explicit = True
    factory_calls = []

    def make_manager(db_path):
        backend = (
            os.environ.get("CGC_RUNTIME_DB_TYPE")
            or os.environ.get("DEFAULT_DATABASE")
            or ("falkordb-remote" if os.environ.get("FALKORDB_HOST") else "ladybugdb")
        )
        factory_calls.append((db_path, backend))
        return _SwitchTestManager(backend, db_path)

    monkeypatch.setattr(cli_helpers, "ensure_first_run_bootstrap", lambda: None)
    monkeypatch.setattr(cli_helpers, "resolve_context", lambda *_a, **_k: context)
    monkeypatch.setattr(cli_helpers, "get_database_manager", make_manager)
    monkeypatch.setattr(cli_helpers, "JobManager", lambda: object())
    monkeypatch.setattr(cli_helpers, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli_helpers, "CodeFinder", lambda *_args, **_kwargs: object())

    manager, _graph_builder, _code_finder, resolved = cli_helpers._initialize_services()

    assert factory_calls == [(context.db_path, "ladybugdb")]
    assert manager.get_backend_type() == "ladybugdb"
    assert resolved.database == "ladybugdb"


def _create_named_context_with_implicit_default(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    config_path = profile / "config.yaml"
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", config_path)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )

    from codegraphcontext.cli import main as cli_main

    cli_main.context_create("project", database=None, db_path=None)
    config_manager.set_context_mode("named")
    config_manager.set_default_context("project")
    return cli_main


def test_context_create_without_database_persists_implicit_backend(
    tmp_path, monkeypatch
):
    _create_named_context_with_implicit_default(tmp_path, monkeypatch)

    resolved = config_manager.resolve_context("project", read_only=True)

    assert resolved.database == "ladybugdb"
    assert resolved.database_is_explicit is False


@pytest.mark.parametrize("fallback_backend", ("falkordb", "kuzudb"))
@pytest.mark.parametrize("select_by_cli", (False, True), ids=("default-context", "cli-context"))
def test_implicit_named_context_reuses_existing_fallback_storage(
    tmp_path, monkeypatch, fallback_backend, select_by_cli
):
    _create_named_context_with_implicit_default(tmp_path, monkeypatch)
    fallback_path = (
        tmp_path / "profile" / "contexts" / "project" / "db" / fallback_backend
    )
    fallback_path.mkdir(parents=True)

    resolved = config_manager.resolve_context(
        "project" if select_by_cli else None, read_only=True
    )

    assert resolved.database == fallback_backend
    assert resolved.db_path == str(fallback_path)
    assert resolved.database_is_explicit is False


def test_explicit_named_ladybug_ignores_sibling_fallback_storage(
    tmp_path, monkeypatch
):
    cli_main = _create_named_context_with_implicit_default(tmp_path, monkeypatch)
    cli_main.context_create("explicit", database="ladybugdb", db_path=None)
    fallback_path = tmp_path / "profile" / "contexts" / "explicit" / "db" / "kuzudb"
    fallback_path.mkdir(parents=True)

    resolved = config_manager.resolve_context("explicit", read_only=True)

    assert resolved.database == "ladybugdb"
    assert resolved.db_path == str(
        tmp_path / "profile" / "contexts" / "explicit" / "db" / "ladybugdb"
    )
    assert resolved.database_is_explicit is True


def test_named_context_without_database_keeps_automatic_embedded_fallback(
    tmp_path, monkeypatch
):
    _configure_implicit_kuzu_fallback(monkeypatch)
    cli_main = _create_named_context_with_implicit_default(tmp_path, monkeypatch)
    cli_main._load_credentials()

    assert "DEFAULT_DATABASE" not in os.environ
    assert os.environ["CGC_SELECTED_DATABASE"] == "kuzudb"
    assert os.environ["CGC_DB_SELECTION_SOURCE"] == "auto-detect"

    monkeypatch.setattr(
        server_module,
        "_resolve_global_context_for_restore",
        lambda: ResolvedContext(
            mode="global",
            context_name="",
            database="ladybugdb",
            db_path="/global/db/ladybugdb",
            cgcignore_path="",
        ),
    )
    monkeypatch.setattr(server_module, "get_database_manager", core.get_database_manager)
    monkeypatch.setattr(server_module, "JobManager", lambda: object())
    monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module.MCPServer, "_init_tools", lambda _self: None)

    server = MCPServer(loop=object(), cwd=tmp_path, initialize_database=False)

    assert server.resolved_context.database == "kuzudb"
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "kuzudb"


def test_load_credentials_probes_implicit_fallback_in_resolved_context_path(
    tmp_path, monkeypatch
):
    from codegraphcontext.cli import main as cli_main

    profile = tmp_path / "profile"
    profile.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    context_db_path = profile / "contexts" / "workspace" / "db" / "ladybugdb"
    resolved = ResolvedContext(
        mode="named",
        context_name="workspace",
        database="ladybugdb",
        db_path=str(context_db_path),
        cgcignore_path=str(context_db_path.parent.parent / ".cgcignore"),
        database_is_explicit=False,
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", profile / "config.yaml")
    monkeypatch.setattr(config_manager, "ensure_config_dir", lambda: None)
    monkeypatch.setattr(
        config_manager, "should_apply_project_dotenv", lambda **_kwargs: False
    )
    monkeypatch.setattr(config_manager, "resolve_context", lambda *_a, **_k: resolved)
    monkeypatch.setattr(core, "_is_falkordb_remote_configured", lambda: False)
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: False)
    monkeypatch.setattr(core, "is_falkordb_usable", lambda: True)

    created_paths = []

    class FakeFalkorDBManager:
        def __init__(self, db_path=None):
            self.db_path = db_path
            created_paths.append(db_path)

        def get_driver(self):
            return object()

        def get_backend_type(self):
            return "falkordb"

    fake_falkordb = type(sys)("codegraphcontext.core.database_falkordb")
    fake_falkordb.FalkorDBManager = FakeFalkorDBManager
    fake_falkordb.FalkorDBUnavailableError = type(
        "FalkorDBUnavailableError", (Exception,), {}
    )
    monkeypatch.setitem(sys.modules, fake_falkordb.__name__, fake_falkordb)

    cli_main._load_credentials()

    expected_fallback_path = str(context_db_path.parent / "falkordb")
    assert created_paths == [expected_fallback_path]
    assert os.environ["CGC_SELECTED_DATABASE"] == "falkordb"


def test_context_create_preserves_explicit_database_selections(tmp_path, monkeypatch):
    cli_main = _create_named_context_with_implicit_default(tmp_path, monkeypatch)
    cli_main.context_create("chosen", database="ladybugdb", db_path=None)

    resolved = config_manager.resolve_context("chosen", read_only=True)

    assert resolved.database == "ladybugdb"
    assert resolved.database_is_explicit is True

    monkeypatch.setenv("DEFAULT_DATABASE", "kuzudb")
    cli_main.context_create("configured", database=None, db_path=None)

    configured = config_manager.resolve_context("configured", read_only=True)

    assert configured.database == "kuzudb"
    assert configured.database_is_explicit is True


def test_saved_workspace_mapping_keeps_implicit_ladybug_available_for_kuzu_fallback(
    tmp_path, monkeypatch
):
    _configure_implicit_kuzu_fallback(monkeypatch)
    profile = tmp_path / "profile"
    profile.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    context_path = tmp_path / "saved-context"
    context_path.mkdir()
    context_config = profile / "config.yaml"
    context_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mode": "per-repo",
                "workspace_mappings": {
                    str(repo.resolve()): {
                        "context_path": str(context_path.resolve()),
                        "database": "ladybugdb",
                        "database_is_explicit": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_cgc_dir", lambda _cwd: None)
    monkeypatch.setattr(
        config_manager,
        "get_repo_context_path",
        lambda root: root / ".codegraphcontext",
    )
    monkeypatch.setattr(
        server_module,
        "_resolve_global_context_for_restore",
        lambda: ResolvedContext(
            mode="global",
            context_name="",
            database="ladybugdb",
            db_path="/global/db/ladybugdb",
            cgcignore_path="",
        ),
    )
    monkeypatch.setattr(server_module, "get_database_manager", core.get_database_manager)
    monkeypatch.setattr(server_module, "JobManager", lambda: object())
    monkeypatch.setattr(server_module, "CodeFinder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "GraphBuilder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "CodeWatcher", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(MCPServer, "_init_tools", lambda _self: None)

    server = MCPServer(cwd=repo, initialize_database=False)

    assert server.resolved_context.database == "kuzudb"
    assert server.resolved_context.db_path == str(context_path / "db" / "kuzudb")
    assert os.environ["CGC_RUNTIME_DB_TYPE"] == "kuzudb"


def test_saved_workspace_mapping_reuses_existing_implicit_fallback_storage(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    profile.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    context_path = tmp_path / "saved-context"
    context_path.mkdir()
    fallback_path = context_path / "db" / "kuzudb"
    fallback_path.mkdir(parents=True)
    context_config = profile / "config.yaml"
    context_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mode": "per-repo",
                "workspace_mappings": {
                    str(repo.resolve()): {
                        "context_path": str(context_path.resolve()),
                        "database": "ladybugdb",
                        "database_is_explicit": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_cgc_dir", lambda _cwd: None)
    monkeypatch.setattr(
        config_manager,
        "get_repo_context_path",
        lambda root: root / ".codegraphcontext",
    )

    resolved = config_manager.resolve_context(
        cwd=repo, create_local_context=False, read_only=True
    )

    assert resolved.database == "kuzudb"
    assert resolved.db_path == str(fallback_path)
    assert resolved.database_is_explicit is False


def test_saved_workspace_mapping_honors_explicit_ladybug_selection(
    tmp_path, monkeypatch
):
    _configure_implicit_kuzu_fallback(monkeypatch)
    profile = tmp_path / "profile"
    profile.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    context_path = tmp_path / "saved-context"
    context_path.mkdir()
    (context_path / "db" / "kuzudb").mkdir(parents=True)
    context_config = profile / "config.yaml"
    context_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mode": "per-repo",
                "workspace_mappings": {
                    str(repo.resolve()): {
                        "context_path": str(context_path.resolve()),
                        "database": "ladybugdb",
                        "database_is_explicit": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", context_config)
    monkeypatch.setattr(
        config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", profile / "cgc_config.yaml"
    )
    monkeypatch.setattr(config_manager, "find_local_cgc_dir", lambda _cwd: None)
    monkeypatch.setattr(
        config_manager,
        "get_repo_context_path",
        lambda root: root / ".codegraphcontext",
    )
    monkeypatch.chdir(repo)

    resolved = config_manager.resolve_context(
        create_local_context=False, read_only=True
    )
    assert resolved.database == "ladybugdb"
    assert resolved.database_is_explicit is True

    from codegraphcontext.cli import main as cli_main

    cli_main._load_credentials(read_only=True)

    assert os.environ["DEFAULT_DATABASE"] == "ladybugdb"
    assert os.environ["CGC_SELECTED_DATABASE"] == "ladybugdb"
    assert os.environ["CGC_DB_SELECTION_SOURCE"] == "context (resolved)"

@pytest.mark.parametrize("system", ("Darwin", "Windows"))
def test_no_backend_error_uses_platform_aware_kuzu_guidance(monkeypatch, system):
    monkeypatch.setattr(core.sys, "platform", "darwin" if system == "Darwin" else "win32")
    monkeypatch.setattr(core.sys, "version_info", (3, 14, 0))
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: False)
    monkeypatch.setattr(core, "_is_falkordb_available", lambda: False)
    monkeypatch.setattr(core, "_is_kuzudb_available", lambda: False)
    monkeypatch.setattr(core, "_is_neo4j_available", lambda: False)
    monkeypatch.setattr(core, "_is_neo4j_configured", lambda: False)
    monkeypatch.setattr(core, "_is_nornic_configured", lambda: False)
    with pytest.raises(ValueError) as exc_info:
        core.get_database_manager()
    message = str(exc_info.value)
    assert "does not install Kùzu" in message
    assert "Python <3.14 or Linux" in message

@pytest.mark.parametrize(
    ("python_platform", "version"),
    (("linux", (3, 14, 0)), ("win32", (3, 13, 0))),
)
def test_kuzu_guidance_keeps_install_command_on_supported_platforms(
    monkeypatch, python_platform, version
):
    monkeypatch.setattr(core.sys, "platform", python_platform)
    monkeypatch.setattr(core.sys, "version_info", version)
    guidance = core._kuzudb_install_guidance()
    assert "pip install" in guidance
    assert "codegraphcontext-fork[kuzu]" in guidance
