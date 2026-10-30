
import ast
import os
import sys
import types
from pathlib import Path
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

import codegraphcontext.cli.main as cli_main
import codegraphcontext.cli.cli_helpers as cli_helpers
from codegraphcontext.cli.main import app, _load_credentials

runner = CliRunner()


def _command_name_from_decorator(decorator: ast.Call, func_name: str) -> str:
    if decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
        return decorator.args[0].value

    for kw in decorator.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value

    return func_name.replace("_", "-")


def _inventory_from_main_source() -> dict[str, set[str]]:
    source = Path(cli_main.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    group_alias_to_name: dict[str, str] = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        if not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr != "add_typer":
            continue
        if not isinstance(call.func.value, ast.Name) or call.func.value.id != "app":
            continue
        if not call.args or not isinstance(call.args[0], ast.Name):
            continue

        group_alias = call.args[0].id
        for kw in call.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                group_alias_to_name[group_alias] = kw.value.value

    inventory: dict[str, set[str]] = {
        "root": set(),
        "mcp": set(),
        "neo4j": set(),
        "config": set(),
        "bundle": set(),
        "hook": set(),
        "registry": set(),
        "find": set(),
        "analyze": set(),
    }

    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue

        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr != "command":
                continue
            if not isinstance(decorator.func.value, ast.Name):
                continue

            owner = decorator.func.value.id
            family = "root" if owner == "app" else group_alias_to_name.get(owner)
            if family is None:
                continue

            command_name = _command_name_from_decorator(decorator, node.name)
            inventory.setdefault(family, set()).add(command_name)

    return inventory


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, **kwargs):
        if "MATCH (n:File)" in query:
            return [{"name": "main.py", "path": "repo/main.py", "is_dependency": False}]
        return [{"type": "Function", "name": "demo", "path": "repo/main.py", "line_number": 1, "is_dependency": False}]


class _FakeDriver:
    def session(self):
        return _FakeSession()


class _FakeDBManager:
    def get_driver(self, graph_name: str = None):
        return _FakeDriver()

    def close_driver(self):
        return None


class _FakeGraphBuilder:
    def delete_repository_from_graph(self, _):
        return None


class _FakeCodeFinder:
    def export_diagram_edges(self, *_args, **_kwargs):
        return {"edges": [("repo/main.py", "json")], "total_count": 1, "truncated": False}

    def find_by_function_name(self, *_args, **_kwargs):
        return [{"name": "foo", "path": "repo/main.py", "line_number": 2, "is_dependency": False}]

    def find_by_class_name(self, *_args, **_kwargs):
        return [{"name": "Foo", "path": "repo/main.py", "line_number": 3, "is_dependency": False}]

    def find_by_variable_name(self, *_args, **_kwargs):
        return [{"name": "value", "path": "repo/main.py", "line_number": 4, "context": "module", "is_dependency": False}]

    def find_by_module_name(self, *_args, **_kwargs):
        return [{"name": "repo.module", "path": "repo/module.py", "line_number": 1, "is_dependency": False}]

    def find_imports(self, *_args, **_kwargs):
        return [{"alias": "json", "imported_name": "json", "path": "repo/main.py", "line_number": 1, "is_dependency": False}]

    def find_by_type(self, *_args, **_kwargs):
        return [{"name": "foo", "path": "repo/main.py", "line_number": 2, "is_dependency": False}]

    def find_by_content(self, *_args, **_kwargs):
        return [{"name": "foo", "type": "Function", "path": "repo/main.py", "line_number": 2}]

    def find_functions_by_decorator(self, *_args, **_kwargs):
        return [{"function_name": "foo", "path": "repo/main.py", "line_number": 2, "decorators": ["route"]}]

    def find_functions_by_argument(self, *_args, **_kwargs):
        return [{"function_name": "foo", "path": "repo/main.py", "line_number": 2}]

    def what_does_function_call(self, *_args, **_kwargs):
        return [{"called_function": "bar", "called_file_path": "repo/main.py", "called_line_number": 10, "called_is_dependency": False}]

    def who_calls_function(self, *_args, **_kwargs):
        return [{"caller_function": "main", "caller_file_path": "repo/main.py", "caller_line_number": 1, "caller_is_dependency": False}]

    def find_function_call_chain(self, *_args, **_kwargs):
        return [{
            "chain_length": 2,
            "function_chain": [
                {"name": "main", "path": "repo/main.py", "line_number": 1},
                {"name": "foo", "path": "repo/main.py", "line_number": 2},
            ],
            "call_details": [{"call_line": 1, "args": ["x"]}],
        }]

    def find_module_dependencies(self, *_args, **_kwargs):
        return {
            "importers": [{"importer_file_path": "repo/main.py", "import_line_number": 1}],
            "imports": [{"imported_module": "json", "import_line_number": 1}],
        }

    def find_class_hierarchy(self, *_args, **_kwargs):
        return {
            "parent_classes": [{"parent_class": "Base", "parent_file_path": "repo/base.py", "parent_line_number": 1}],
            "child_classes": [{"child_class": "Derived", "child_file_path": "repo/main.py", "child_line_number": 2}],
            "methods": [{"method_name": "run", "method_args": "self"}],
        }

    def get_cyclomatic_complexity(self, *_args, **_kwargs):
        return {"complexity": 3, "path": "repo/main.py", "line_number": 2}

    def find_most_complex_functions(self, *_args, **_kwargs):
        return [{"function_name": "complex", "complexity": 12, "path": "repo/main.py", "line_number": 20}]

    def find_most_complex_functions_in_file(self, *_args, **_kwargs):
        return [{"function_name": "complex", "complexity": 12, "path": "repo/main.py", "line_number": 20}]

    def find_dead_code(self, *_args, **_kwargs):
        return {
            "potentially_unused_functions": [{"function_name": "unused", "path": "repo/main.py", "line_number": 30}],
            "note": "Static approximation",
        }

    def find_function_overrides(self, *_args, **_kwargs):
        return [{"class_name": "Derived", "function_name": "run", "class_file_path": "repo/main.py", "function_line_number": 20}]

    def find_variable_usage_scope(self, *_args, **_kwargs):
        return {
            "instances": [{
                "scope_type": "function",
                "scope_name": "foo",
                "path": "repo/main.py",
                "line_number": 2,
                "variable_value": "1",
            }]
        }

    def audit_kotlin_call_ambiguity(self, *_args, **_kwargs):
        return {
            "kotlin_fn_to_fn_edges": 0,
            "ambiguous_groups": 0,
            "ambiguous_edges": 0,
            "top_names": [],
            "examples": [],
        }

    def list_indexed_repositories(self):
        return [{"name": "repo", "path": "repo"}]


@pytest.fixture
def kuzudb_env():
    env = {
        "DEFAULT_DATABASE": "kuzudb",
        "CGC_RUNTIME_DB_TYPE": "kuzudb",
    }
    with patch.dict(os.environ, env, clear=False):
        yield


@pytest.fixture
def cli_test_stubs(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_main.config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli_main.config_manager, "CONFIG_FILE", tmp_path / "config.json")
    # `delete`/`rm` are gated behind ALLOW_DB_DELETION; enable it so the
    # canonical-command smoke matrix can exercise them without a real config.
    monkeypatch.setattr(cli_main.config_manager, "is_db_deletion_allowed", lambda: True)

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "configure_mcp_client", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "run_neo4j_setup_wizard", lambda *_args, **_kwargs: None)

    monkeypatch.setattr(cli_main, "index_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "update_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "setup_scip_helper", lambda *_args, **_kwargs: None)
    
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)

    monkeypatch.setattr(cli_main, "add_package_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "list_repos_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "delete_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "cypher_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "cypher_helper_visual", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "visualize_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "reindex_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "clean_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "stats_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "watch_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "unwatch_helper", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "list_watching_helper", lambda *_args, **_kwargs: None)

    fake_db = _FakeDBManager()
    monkeypatch.setattr(cli_main, "_initialize_services", lambda *_args, **_kwargs: (fake_db, _FakeGraphBuilder(), _FakeCodeFinder(), MagicMock()))
    from codegraphcontext.core.database import DatabaseManager
    monkeypatch.setattr(DatabaseManager, "test_connection", staticmethod(lambda *_args, **_kwargs: (True, None)))
    monkeypatch.setattr(cli_main.typer, "confirm", lambda *_args, **_kwargs: True)

    class _FakeMCPServer:
        def __init__(self, *_args, **_kwargs):
            self.tools = {
                "demo": {"name": "demo.tool", "description": "demo"},
            }

        async def run(self):
            return None

        def shutdown(self):
            return None

    monkeypatch.setattr(cli_main, "MCPServer", _FakeMCPServer)

    downloaded_bundle = tmp_path / "downloaded.cgc"
    downloaded_bundle.write_text("bundle", encoding="utf-8")

    registry_module = types.ModuleType("codegraphcontext.cli.registry_commands")
    registry_module.list_bundles = lambda *_args, **_kwargs: None
    registry_module.search_bundles = lambda *_args, **_kwargs: None
    registry_module.download_bundle = lambda *_args, **_kwargs: str(downloaded_bundle)
    registry_module.request_bundle = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "codegraphcontext.cli.registry_commands", registry_module)

    bundle_module = types.ModuleType("codegraphcontext.core.cgc_bundle")

    class _FakeCGCBundle:
        def __init__(self, _db_manager):
            pass

        def export_to_bundle(self, *_args, **_kwargs):
            return True, "Bundle exported"

        def import_from_bundle(self, *_args, **_kwargs):
            return True, "Bundle imported"

    bundle_module.CGCBundle = _FakeCGCBundle
    monkeypatch.setitem(sys.modules, "codegraphcontext.core.cgc_bundle", bundle_module)

    monkeypatch.setattr(cli_main, "_write_datasource_graph", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "install_hooks",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            repo_root=tmp_path, git_dir=tmp_path / ".git"
        ),
    )
    monkeypatch.setattr(
        cli_main,
        "uninstall_hooks",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            repo_root=tmp_path, git_dir=tmp_path / ".git"
        ),
    )
    monkeypatch.setattr(
        cli_main,
        "get_hook_status",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            repo_root=tmp_path,
            git_dir=tmp_path / ".git",
            installed_hooks=("post-commit",),
            unmanaged_hooks=(),
            has_merge_driver=True,
            has_gitattributes_entry=True,
        ),
    )

    datasource_pkg = types.ModuleType("codegraphcontext.tools.datasources")
    monkeypatch.setitem(sys.modules, "codegraphcontext.tools.datasources", datasource_pkg)

    mysql_module = types.ModuleType("codegraphcontext.tools.datasources.mysql_ingester")
    mysql_module.ingest = lambda **_kwargs: {
        "datasource": {"name": "mysql-test"},
        "tables": [{"name": "users"}],
        "columns": [{"name": "id"}],
    }
    monkeypatch.setitem(sys.modules, "codegraphcontext.tools.datasources.mysql_ingester", mysql_module)

    cassandra_module = types.ModuleType("codegraphcontext.tools.datasources.cassandra_ingester")
    cassandra_module.ingest = lambda **_kwargs: {
        "datasource": {"name": "cassandra-test"},
        "tables": [{"name": "users"}],
        "columns": [{"name": "id"}],
    }
    monkeypatch.setitem(sys.modules, "codegraphcontext.tools.datasources.cassandra_ingester", cassandra_module)

    redis_module = types.ModuleType("codegraphcontext.tools.datasources.redis_ingester")
    redis_module.ingest = lambda **_kwargs: {
        "datasource": {"name": "redis-test"},
        "tables": [{"name": "pattern:user:*"}],
        "columns": [{"name": "key"}],
    }
    monkeypatch.setitem(sys.modules, "codegraphcontext.tools.datasources.redis_ingester", redis_module)

    return {
        "bundle_file": downloaded_bundle,
        "bundle_export": tmp_path / "exported.cgc",
    }


def _matrix_command_set(entries: list[list[str]]) -> set[tuple[str, str]]:
    families = set(_inventory_from_main_source().keys()) - {"root"}
    covered: set[tuple[str, str]] = set()
    for args in entries:
        if args[0] in families:
            covered.add((args[0], args[1]))
        else:
            covered.add(("root", args[0]))
    return covered


@pytest.mark.parametrize(
    ("args", "expected_sync_on_start"),
    [
        (["watch", "."], False),
        (["watch", "--sync-on-start", "."], True),
        (["w", "."], False),
        (["w", "--sync-on-start", "."], True),
    ],
)
def test_watch_sync_on_start_flag_is_explicit(monkeypatch, args, expected_sync_on_start):
    calls = []
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "watch_helper", lambda *call_args, **kwargs: calls.append((call_args, kwargs)))

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert calls
    assert calls[0][1]["sync_on_start"] is expected_sync_on_start


def test_cli_inventory_grouped_from_source():
    inventory = _inventory_from_main_source()

    assert {"root", "mcp", "neo4j", "config", "bundle", "hook", "registry", "find", "analyze"}.issubset(set(inventory.keys()))
    assert inventory["mcp"] == {"setup", "start", "tools"}
    assert inventory["neo4j"] == {"setup"}
    assert inventory["config"] == {"show", "set", "reset", "db"}
    assert inventory["bundle"] == {
        "export",
        "import",
        "load",
        "merge",
        # Added alongside bundle signing/encryption (#1060).
        "verify",
        "inspect",
        "diff",
    }
    assert inventory["hook"] == {"install", "uninstall", "status"}
    assert inventory["registry"] == {"list", "search", "download", "request"}
    assert inventory["find"] == {"name", "pattern", "type", "variable", "content", "decorator", "argument"}
    assert inventory["analyze"] == {
        "calls",
        "callers",
        "chain",
        "deps",
        "tree",
        "complexity",
        "dead-code",
        "overrides",
        "variable",
        "kotlin-call-audit",
    }
    if "api" in inventory:
        assert inventory["api"] == {"start"}
    if "datasource" in inventory:
        assert inventory["datasource"] == {"mysql", "cassandra", "redis"}
    if "context" in inventory:
        assert inventory["context"] == {"list", "create", "delete", "mode", "default"}


def test_all_canonical_cli_commands_run_with_kuzudb(kuzudb_env, cli_test_stubs, tmp_path):
    bundle_file = str(cli_test_stubs["bundle_file"])
    bundle_export = str(cli_test_stubs["bundle_export"])

    command_matrix = [
        ["mcp", "setup"],
        ["mcp", "start"],
        ["mcp", "tools"],
        ["neo4j", "setup"],
        ["config", "show"],
        ["config", "set", "MAX_FILE_SIZE_MB", "11"],
        ["config", "reset"],
        ["config", "db", "kuzudb"],
        ["bundle", "export", bundle_export],
        ["bundle", "import", bundle_file],
        ["bundle", "load", bundle_file],
        ["hook", "install"],
        ["hook", "uninstall"],
        ["hook", "status"],
        ["registry", "list"],
        ["registry", "search", "numpy"],
        ["registry", "download", "numpy"],
        ["registry", "request", "https://github.com/example/repo"],
        ["doctor"],
        ["setup-scip"],
        ["api", "start"],
        ["index", "."],
        ["update", "."],
        ["clean"],
        ["stats"],
        ["diagram"],
        ["delete", "."],
        ["visualize"],
        ["list"],
        ["add-package", "requests", "python"],
        ["watch", "."],
        ["unwatch", "."],
        ["watching"],
        ["find", "name", "foo"],
        ["find", "pattern", "foo"],
        ["find", "type", "function"],
        ["find", "variable", "value"],
        ["find", "content", "foo"],
        ["find", "decorator", "route"],
        ["find", "argument", "user_id"],
        ["analyze", "calls", "foo"],
        ["analyze", "callers", "foo"],
        ["analyze", "chain", "main", "foo"],
        ["analyze", "deps", "json"],
        ["analyze", "tree", "Foo"],
        ["analyze", "complexity"],
        ["analyze", "dead-code"],
        ["analyze", "overrides", "run"],
        ["analyze", "variable", "value"],
        ["analyze", "kotlin-call-audit"],
        ["query", "MATCH (n) RETURN n LIMIT 1"],
        ["cypher", "MATCH (n) RETURN n LIMIT 1"],
        ["i", "."],
        ["ls"],
        ["rm", "."],
        ["v", "."],
        ["w", "."],
        ["help"],
        ["version"],
        ["m"],
        ["n"],
        ["export", bundle_export],
        ["load", bundle_file],
        ["report"],
    ]

    source_inventory = _inventory_from_main_source()
    if "context" in source_inventory:
        command_matrix.extend(
            [
                ["context", "list"],
                ["context", "create", "ci-context"],
                ["context", "default", "ci-context"],
                ["context", "mode", "global"],
                ["context", "delete", "ci-context"],
            ]
        )
    if "datasource" in source_inventory:
        command_matrix.extend(
            [
                ["datasource", "mysql", "--host", "localhost", "--user", "root", "--password", "pw", "--database", "demo"],
                ["datasource", "cassandra", "--host", "localhost", "--keyspace", "demo"],
                ["datasource", "redis", "--host", "localhost"],
            ]
        )
    if "prompt" in source_inventory:
        # The prompt sub-app was added without matrix entries, so this test —
        # which asserts every command in the source inventory is smoke-tested —
        # has been failing on main. `add` and `remove` take a path argument.
        prompt_file = tmp_path / "ci-prompt.md"
        prompt_file.write_text("# ci prompt\n", encoding="utf-8")
        command_matrix.extend(
            [
                ["prompt", "list"],
                ["prompt", "add", str(prompt_file)],
                ["prompt", "remove", str(prompt_file)],
            ]
        )

    expected_inventory = source_inventory
    expected_set = {(family, name) for family, names in expected_inventory.items() for name in names}
    # `bundle merge` is a non-interactive git merge driver that takes file
    # arguments; it is not exercised by this smoke matrix.
    expected_set.discard(("bundle", "merge"))
    # `bundle verify`/`inspect`/`diff` (#1060) each require an existing .cgc
    # file (two, for diff) as a positional argument, so they cannot be invoked
    # bare the way this matrix invokes everything else. Their behaviour is
    # covered by the bundle round-trip tests instead.
    expected_set.discard(("bundle", "verify"))
    expected_set.discard(("bundle", "inspect"))
    expected_set.discard(("bundle", "diff"))
    assert _matrix_command_set(command_matrix) == expected_set

    for args in command_matrix:
        result = runner.invoke(app, ["--database", "kuzudb", *args])
        assert result.exit_code == 0, f"command failed: {' '.join(args)}\n{result.output}"
        assert result.exception is None, f"exception raised for {' '.join(args)}"
        assert "Traceback" not in result.output


def test_config_db_rejects_invalid_backend_with_clear_error(kuzudb_env):
    result = runner.invoke(app, ["config", "db", "invalid-backend"])

    assert result.exit_code == 1
    assert "Invalid backend" in result.output
    assert "kuzudb" in result.output


def test_config_show_with_empty_config_does_not_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_main.config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli_main.config_manager, "CONFIG_FILE", tmp_path / "config.json")

    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    assert "Configuration Settings" in result.output


def test_find_content_surfaces_query_errors(monkeypatch):
    class _FakeDBManager:
        def close_driver(self):
            return None

    class _FailingFinder:
        def find_by_content(self, _query):
            raise Exception("database connection lost")

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _FailingFinder()),
    )

    original_runtime_db_type = os.environ.get("CGC_RUNTIME_DB_TYPE")
    with monkeypatch.context() as env_patch:
        env_patch.setenv("CGC_RUNTIME_DB_TYPE", original_runtime_db_type or "kuzudb")
        result = runner.invoke(app, ["--database", "falkordb", "find", "content", "foo"])

    assert result.exit_code != 0
    assert "Full-text search is not supported on FalkorDB" not in result.output
    assert os.environ.get("CGC_RUNTIME_DB_TYPE") == original_runtime_db_type


def test_find_name_scopes_codefinder_and_direct_queries_to_current_repository(monkeypatch):
    finder_calls = []
    query_calls = []
    repo_path = Path.cwd().resolve().as_posix()

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def run(self, query, **kwargs):
            query_calls.append((query, kwargs))
            return []

    class _Driver:
        def session(self):
            return _Session()

    class _DBManager:
        def get_driver(self):
            return _Driver()

        def close_driver(self):
            return None

    class _CodeFinder:
        def _search(self, method, **kwargs):
            finder_calls.append((method, kwargs))
            return [{"name": "demo", "path": "repo/demo.py", "line_number": 1}]

        def find_by_function_name(self, _name, **kwargs):
            return self._search("function", **kwargs)

        def find_by_class_name(self, _name, **kwargs):
            return self._search("class", **kwargs)

        def find_by_variable_name(self, _name, **kwargs):
            return self._search("variable", **kwargs)

        def find_by_module_name(self, _name, **kwargs):
            return self._search("module", **kwargs)

        def find_imports(self, _name, **kwargs):
            return self._search("imports", **kwargs)

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_DBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )

    result = runner.invoke(app, ["find", "name", "demo"])

    assert result.exit_code == 0, result.output
    assert {method for method, _ in finder_calls} == {
        "function", "class", "variable", "module", "imports"
    }
    assert all(kwargs.get("repo_path") == repo_path for _, kwargs in finder_calls)
    assert len(query_calls) == 4
    assert all("n.path STARTS WITH $repo_prefix" in query for query, _ in query_calls)
    assert all(params.get("repo_root") == repo_path for _, params in query_calls)
    assert all(
        params.get("repo_prefix") == repo_path.rstrip("/") + "/"
        for _, params in query_calls
    )

    file_result = runner.invoke(app, ["find", "name", "demo", "--type", "file"])
    assert file_result.exit_code == 0, file_result.output
    assert "n.path STARTS WITH $repo_prefix" in query_calls[-1][0]
    assert query_calls[-1][1].get("repo_root") == repo_path
    assert query_calls[-1][1].get("repo_prefix") == repo_path.rstrip("/") + "/"


def test_find_content_scopes_search_to_current_repository(monkeypatch):
    calls = []
    repo_path = Path.cwd().resolve().as_posix()

    class _DBManager:
        def close_driver(self):
            return None

    class _CodeFinder:
        def find_by_content(self, query, **kwargs):
            calls.append((query, kwargs))
            return [{"name": "demo", "type": "Function", "path": "repo/demo.py", "line_number": 1}]

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_DBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )

    result = runner.invoke(app, ["find", "content", "needle"])

    assert result.exit_code == 0, result.output
    assert calls == [("needle", {"repo_path": repo_path, "limit": 51})]


def test_query_defaults_to_repository_scope_and_supports_explicit_global_paging(
    monkeypatch, tmp_path
):
    (tmp_path / ".git").mkdir()
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "cypher_helper",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        cli_main,
        "cypher_helper_visual",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    query = "MATCH (f:Function) RETURN f.uid ORDER BY f.uid"
    scoped = runner.invoke(
        app, ["query", query, "--offset", "4", "--page-size", "7"]
    )
    global_query = runner.invoke(app, ["query", query, "--global-scope"])
    visual = runner.invoke(
        app, ["query", query, "--visual", "--offset", "4", "--page-size", "7"]
    )

    assert scoped.exit_code == 0, scoped.output
    assert global_query.exit_code == 0, global_query.output
    assert visual.exit_code == 0, visual.output
    assert calls == [
        ((query, None), {
            "repo_path": str(tmp_path.resolve()),
            "global_scope": False,
            "offset": 4,
            "page_size": 7,
        }),
        ((query, None), {
            "repo_path": None,
            "global_scope": True,
            "offset": 0,
            "page_size": 100,
        }),
        ((query, None), {
            "repo_path": str(tmp_path.resolve()),
            "global_scope": False,
            "offset": 4,
            "page_size": 7,
        }),
    ]


def test_cli_variable_analysis_forwards_row_and_evidence_pages(monkeypatch, tmp_path):
    repo = tmp_path.resolve().as_posix()
    (tmp_path / ".git").mkdir()
    calls = []
    value_page = "v" * 64
    context_page = "c" * 64

    class _CodeFinder:
        def find_variable_usage_scope(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {
                "instances": [{
                    "scope_type": "function",
                    "scope_name": "load_config",
                    "path": f"{repo}/settings.py",
                    "line_number": 12,
                    "variable_value": value_page,
                    "variable_value_offset": 100,
                    "variable_value_total_chars": 300,
                    "variable_value_next_offset": 164,
                    "variable_value_truncated": True,
                    "context": context_page,
                    "context_offset": 200,
                    "context_total_chars": 500,
                    "context_next_offset": 264,
                    "context_truncated": True,
                }],
                "offset": 5,
                "page_size": 2,
                "result_limit": 2,
                "truncated": True,
                "next_offset": 7,
                "continuation_limit_reached": False,
            }

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "analyze", "variable", "setting", "--offset", "5", "--page-size", "2",
            "--value-offset", "100", "--context-offset", "200",
            "--evidence-char-limit", "64",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(
        ("setting", None),
        {
            "repo_path": repo,
            "offset": 5,
            "page_size": 2,
            "value_offset": 100,
            "context_offset": 200,
            "evidence_char_limit": 64,
        },
    )]
    assert "--offset 7" in result.output
    assert "--value-offset 164" in result.output
    assert "--context-offset 264" in result.output
    compact_output = "".join(result.output.split())
    assert value_page in compact_output
    assert context_page in compact_output


@pytest.mark.parametrize(
    ("command", "method"),
    [
        (["find", "decorator", "route"], "find_functions_by_decorator"),
        (["find", "argument", "account_id"], "find_functions_by_argument"),
        (["analyze", "calls", "entry"], "what_does_function_call"),
        (["analyze", "callers", "entry"], "who_calls_function"),
        (["analyze", "chain", "entry", "target"], "find_function_call_chain"),
        (["analyze", "overrides", "render"], "find_function_overrides"),
    ],
)
def test_cli_relationship_commands_forward_offsets_and_print_continuation(
    monkeypatch, tmp_path, command, method
):
    repo = tmp_path.resolve().as_posix()
    (tmp_path / ".git").mkdir()
    calls = []

    class _CodeFinder:
        def __getattr__(self, name):
            def query(*_args, **kwargs):
                calls.append((name, kwargs))
                offset = kwargs["offset"]
                limit = kwargs["limit"]
                rows = []
                for index in range(offset, offset + limit):
                    if name == "find_functions_by_decorator":
                        rows.append({
                            "function_name": f"fn_{index}",
                            "path": f"{repo}/a.py",
                            "line_number": index,
                            "decorators": ["route"],
                        })
                    elif name == "find_functions_by_argument":
                        rows.append({
                            "function_name": f"fn_{index}",
                            "path": f"{repo}/a.py",
                            "line_number": index,
                        })
                    elif name == "what_does_function_call":
                        rows.append({
                            "called_function": f"called_{index}",
                            "called_file_path": f"{repo}/a.py",
                            "called_line_number": index,
                        })
                    elif name == "who_calls_function":
                        rows.append({
                            "caller_function": f"caller_{index}",
                            "caller_file_path": f"{repo}/a.py",
                            "caller_line_number": index,
                        })
                    elif name == "find_function_call_chain":
                        rows.append({
                            "chain_length": 1,
                            "function_chain": [{
                                "name": f"entry_{index}",
                                "path": f"{repo}/a.py",
                                "line_number": index,
                            }],
                            "call_details": [],
                        })
                    else:
                        rows.append({
                            "class_name": f"Class_{index}",
                            "function_name": "render",
                            "class_file_path": f"{repo}/a.py",
                            "function_line_number": index,
                        })
                return rows
            return query

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_main, "get_tool_result_limit", lambda _name: 2)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [*command, "--offset", "6"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0] == method
    assert calls[0][1]["offset"] == 6
    assert calls[0][1]["limit"] == 3
    assert "--offset 8" in result.output


def test_visualize_defaults_to_repository_scope(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "visualize_helper",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = runner.invoke(app, ["visualize"])

    assert result.exit_code == 0, result.output
    assert calls == [
        ((), {
            "repo_path": str(tmp_path.resolve()),
            "host": "127.0.0.1",
            "port": 8000,
            "context": None,
            "global_scope": False,
            "offset": 0,
        })
    ]


def test_find_visualization_receives_the_active_repository_scope(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (
            _FakeDBManager(), _FakeGraphBuilder(), _FakeCodeFinder()
        ),
    )
    monkeypatch.setattr(
        cli_main,
        "visualize_search_results",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = runner.invoke(
        app,
        ["find", "name", "foo", "--type", "function", "--no-fuzzy", "--visual"],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][1]["repo_path"] == str(tmp_path.resolve())


@pytest.mark.parametrize(
    ("command", "expected_page_size"),
    [
        (
            ["find", "name", "foo", "--type", "function", "--no-fuzzy", "--visual", "--offset", "5"],
            50,
        ),
        (["find", "pattern", "foo", "--visual", "--offset", "5"], 50),
        (["find", "type", "function", "--visual", "--offset", "5", "--limit", "7"], 7),
    ],
)
def test_find_visualization_forwards_selected_page_window(
    monkeypatch, tmp_path, command, expected_page_size
):
    (tmp_path / ".git").mkdir()
    calls = []

    class _ManyCodeFinder(_FakeCodeFinder):
        @staticmethod
        def _rows():
            return [
                {
                    "name": f"foo_{index}",
                    "path": f"repo/foo_{index}.py",
                    "line_number": index + 1,
                    "is_dependency": False,
                }
                for index in range(10)
            ]

        def find_by_function_name(self, *_args, **_kwargs):
            return self._rows()

        def find_by_type(self, *_args, **_kwargs):
            return self._rows()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        _FakeSession,
        "run",
        lambda *_args, **_kwargs: _ManyCodeFinder._rows(),
    )
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (
            _FakeDBManager(), _FakeGraphBuilder(), _ManyCodeFinder()
        ),
    )
    monkeypatch.setattr(
        cli_main,
        "visualize_search_results",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = runner.invoke(app, command)

    assert result.exit_code == 0, result.output
    assert calls[0][1]["offset"] == 5
    assert calls[0][1]["page_size"] == expected_page_size


def test_cli_find_scope_resolves_repository_root_from_subdirectory(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "src" / "pkg"
    (repo / ".git").mkdir(parents=True)
    nested.mkdir(parents=True)
    calls = []

    class _CodeFinder(_FakeCodeFinder):
        def find_by_type(self, *args, **kwargs):
            calls.append(kwargs)
            return [{"name": "f", "path": f"{repo}/src/f.py", "line_number": 1}]

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )
    monkeypatch.chdir(nested)

    result = runner.invoke(app, ["find", "type", "function"])

    assert result.exit_code == 0, result.output
    assert calls[0]["repo_path"] == str(repo.resolve())


def test_cli_find_fails_closed_from_multi_repository_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo_a = workspace / "teams" / "a" / "repo-a"
    repo_b = workspace / "teams" / "b" / "repo-b"
    (repo_a / ".git").mkdir(parents=True)
    (repo_b / ".git").mkdir(parents=True)
    calls = []

    class _CodeFinder(_FakeCodeFinder):
        def find_by_type(self, *args, **kwargs):
            calls.append(kwargs)
            return []

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, ["find", "type", "function"])

    assert result.exit_code != 0
    assert calls == []
    assert "repository" in result.output.lower()


@pytest.mark.parametrize(
    "command_args",
    [["find", "type", "function"], ["diagram"]],
)
def test_cli_search_and_diagram_fail_closed_without_git_repository_roots(
    monkeypatch, tmp_path, command_args
):
    workspace = tmp_path / "workspace"
    (workspace / "indexed-a" / "src").mkdir(parents=True)
    (workspace / "indexed-b" / "src").mkdir(parents=True)
    calls = []

    class _CodeFinder(_FakeCodeFinder):
        def find_by_type(self, *args, **kwargs):
            calls.append(("find_by_type", args, kwargs))
            return [
                {"name": "found-a", "path": str(workspace / "indexed-a" / "src" / "a.py"), "line_number": 1},
                {"name": "found-b", "path": str(workspace / "indexed-b" / "src" / "b.py"), "line_number": 2},
            ]

        def export_diagram_edges(self, **kwargs):
            calls.append(("export_diagram_edges", (), kwargs))
            return {"edges": [("indexed-a", "indexed-b")], "total_count": 1, "truncated": False}

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )
    monkeypatch.chdir(workspace)

    result = runner.invoke(app, command_args)

    assert result.exit_code != 0
    assert calls == []
    assert "repository" in result.output.lower()


def test_kotlin_call_audit_defaults_to_current_repository(monkeypatch, tmp_path):
    repo = tmp_path / "kotlin-repo"
    (repo / ".git").mkdir(parents=True)
    calls = []

    class _CodeFinder:
        def audit_kotlin_call_ambiguity(self, **kwargs):
            calls.append(kwargs)
            return {
                "kotlin_fn_to_fn_edges": 0,
                "ambiguous_groups": 0,
                "ambiguous_edges": 0,
                "top_names": [],
                "examples": [],
            }

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["analyze", "kotlin-call-audit"])

    assert result.exit_code == 0, result.output
    assert calls == [{
        "repo_path": str(repo.resolve()), "limit": 20, "offset": 0, "page_size": 100
    }]


def test_kotlin_call_audit_forwards_database_page_and_prints_continuation(monkeypatch, tmp_path):
    repo = tmp_path / "kotlin-audit-page-repo"
    (repo / ".git").mkdir(parents=True)
    calls = []

    class _CodeFinder:
        def audit_kotlin_call_ambiguity(self, **kwargs):
            calls.append(kwargs)
            return {
                "kotlin_fn_to_fn_edges": 3,
                "ambiguous_groups": 0,
                "ambiguous_edges": 0,
                "top_names": [],
                "examples": [],
                "offset": 7,
                "page_size": 3,
                "truncated": True,
                "next_offset": 10,
                "continuation_limit_reached": False,
            }

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )
    monkeypatch.chdir(repo)

    result = runner.invoke(
        app,
        ["analyze", "kotlin-call-audit", "--offset", "7", "--page-size", "3"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [{
        "repo_path": str(repo.resolve()), "limit": 20, "offset": 7, "page_size": 3
    }]
    assert "--offset 10" in result.output


def test_analyze_chain_defaults_to_current_repository(monkeypatch, tmp_path):
    repo = tmp_path / "call-chain-repo"
    (repo / ".git").mkdir(parents=True)
    calls = []

    class _CodeFinder:
        def find_function_call_chain(self, *args, **kwargs):
            calls.append((args, kwargs))
            return []

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["analyze", "chain", "entry", "target"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (("entry", "target", 5, None, None), {
            "repo_path": str(repo.resolve()), "limit": 21, "offset": 0
        })
    ]


def test_cli_name_content_and_variable_searches_report_overflow(monkeypatch, tmp_path):
    repo = tmp_path.resolve().as_posix()
    (tmp_path / ".git").mkdir()
    seen = []

    def rows():
        return [
            {"name": f"item-{index}", "type": "Function", "path": f"{repo}/{index}.py", "line_number": index}
            for index in range(51)
        ]

    class _CodeFinder:
        def find_by_function_name(self, _name, **kwargs):
            seen.append(("function", kwargs))
            return rows()

        def find_by_class_name(self, _name, **kwargs):
            return []

        def find_by_variable_name(self, _name, **kwargs):
            seen.append(("variable", kwargs))
            return rows()

        def find_by_content(self, _name, **kwargs):
            seen.append(("content", kwargs))
            return rows()

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )
    monkeypatch.chdir(tmp_path)

    name = runner.invoke(app, ["find", "name", "item", "--type", "function", "--no-fuzzy"])
    variable = runner.invoke(app, ["find", "variable", "item"])
    content = runner.invoke(app, ["find", "content", "item"])

    for result in (name, variable, content):
        assert result.exit_code == 0, result.output
        assert "truncated" in result.output.lower()
        assert "Found 50" in result.output
    assert all(kwargs["limit"] == 51 for _, kwargs in seen)
    assert all(kwargs["repo_path"] == repo for _, kwargs in seen)


def test_cli_find_searches_support_continuation_offsets(monkeypatch, tmp_path):
    repo = tmp_path.resolve().as_posix()
    (tmp_path / ".git").mkdir()
    seen = []

    def rows(prefix, start, count):
        return [
            {
                "name": f"{prefix}-{index:03d}",
                "type": "Function",
                "path": f"{repo}/{index:03d}.py",
                "line_number": index,
                "context": "module",
            }
            for index in range(start, start + count)
        ]

    class _CodeFinder:
        def find_by_function_name(self, _name, **kwargs):
            seen.append(("name", kwargs))
            return rows("name", 0, kwargs["limit"])

        def find_by_type(self, _type, limit, repo_path=None, offset=0):
            seen.append(("type", {"limit": limit, "repo_path": repo_path, "offset": offset}))
            return rows("type", offset, limit)

        def find_by_variable_name(self, _name, **kwargs):
            seen.append(("variable", kwargs))
            return rows("variable", kwargs.get("offset", 0), kwargs["limit"])

        def find_by_content(self, _name, **kwargs):
            seen.append(("content", kwargs))
            return rows("content", kwargs.get("offset", 0), kwargs["limit"])

    class _Session(_FakeSession):
        def run(self, query, **kwargs):
            if "MATCH (n)" in query:
                seen.append(("pattern", kwargs))
                return rows("pattern", 0, kwargs["limit"])
            return []

    class _Driver(_FakeDriver):
        def session(self):
            return _Session()

    class _DBManager(_FakeDBManager):
        def get_driver(self, graph_name=None):
            return _Driver()

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_DBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )
    monkeypatch.chdir(tmp_path)

    name = runner.invoke(
        app, ["find", "name", "name", "--type", "function", "--no-fuzzy", "--offset", "50"]
    )
    pattern = runner.invoke(app, ["find", "pattern", "pattern", "--offset", "50"])
    by_type = runner.invoke(app, ["find", "type", "function", "--limit", "3", "--offset", "4"])
    variable = runner.invoke(app, ["find", "variable", "variable", "--offset", "50"])
    content = runner.invoke(app, ["find", "content", "content", "--offset", "50"])

    for result in (name, pattern, by_type, variable, content):
        assert result.exit_code == 0, result.output
    for result, prefix in (
        (name, "name"),
        (pattern, "pattern"),
        (variable, "variable"),
        (content, "content"),
    ):
        assert f"{prefix}-050" in result.output
        assert f"{prefix}-049" not in result.output
        assert "--offset 100" in result.output
    assert "type-004" in by_type.output
    assert "type-003" not in by_type.output
    assert "--offset 7" in by_type.output
    assert ("name", {"fuzzy_search": False, "repo_path": repo, "limit": 101}) in seen
    assert any(kind == "pattern" and kwargs["limit"] == 101 for kind, kwargs in seen)
    assert ("type", {"limit": 4, "repo_path": repo, "offset": 4}) in seen
    assert any(kind == "variable" and kwargs.get("offset") == 50 and kwargs["limit"] == 51 for kind, kwargs in seen)
    assert any(kind == "content" and kwargs.get("offset") == 50 and kwargs["limit"] == 51 for kind, kwargs in seen)


def test_cli_find_continuation_does_not_suggest_offset_past_limit(monkeypatch):
    output = StringIO()
    console = Console(file=output, highlight=False)
    monkeypatch.setattr(cli_main, "find_result_console", console)

    cli_main._print_find_continuation(99_999, 50)

    rendered = output.getvalue()
    assert "--offset 100049" not in rendered
    assert "99999" in rendered
    assert "continuation" in rendered.lower()


def test_cli_search_scope_uses_the_selected_context_repository(monkeypatch, tmp_path):
    cwd_repo = tmp_path / "cwd-repo"
    selected_repo = tmp_path / "context-repo"
    (cwd_repo / ".git").mkdir(parents=True)
    (selected_repo / ".git").mkdir(parents=True)
    calls = []

    class _Context:
        repos = [selected_repo.as_posix()]

    class _ContextConfig:
        mode = "global"
        default_context = ""
        contexts = {"selected": _Context()}

    class _CodeFinder(_FakeCodeFinder):
        def find_by_function_name(self, _name, **kwargs):
            calls.append(kwargs)
            return [{"name": "target", "path": f"{selected_repo}/target.py", "line_number": 1}]

    monkeypatch.setattr(cli_main.config_manager, "load_context_config", lambda **_kwargs: _ContextConfig())
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )
    monkeypatch.chdir(cwd_repo)

    result = runner.invoke(
        app,
        ["find", "name", "target", "--type", "function", "--no-fuzzy", "--context", "selected"],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["repo_path"] == selected_repo.resolve().as_posix()


def test_cli_per_repo_context_scopes_an_unversioned_source_directory(monkeypatch, tmp_path):
    source_root = tmp_path / "unversioned-source"
    local_context = source_root / ".codegraphcontext"
    local_context.mkdir(parents=True)
    (local_context / "config.yaml").write_text("database: kuzudb\n", encoding="utf-8")

    class _ContextConfig:
        mode = "per-repo"
        default_context = ""
        contexts = {}

    monkeypatch.setattr(
        cli_main.config_manager,
        "load_context_config",
        lambda **_kwargs: _ContextConfig(),
    )

    assert cli_main._resolve_cli_repo_scope(start=source_root) == source_root.resolve().as_posix()


def test_cli_per_repo_mode_fails_closed_for_implicit_multi_repository_root(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    (workspace / "projects" / "repo-a" / ".git").mkdir(parents=True)
    (workspace / "projects" / "repo-b" / ".git").mkdir(parents=True)

    class _ContextConfig:
        mode = "per-repo"
        default_context = ""
        contexts = {}

    monkeypatch.setattr(
        cli_main.config_manager,
        "load_context_config",
        lambda **_kwargs: _ContextConfig(),
    )
    monkeypatch.setattr(
        cli_main.config_manager,
        "get_repo_context_path",
        lambda root: workspace / ".codegraphcontext",
    )
    monkeypatch.setattr(
        cli_main.config_manager,
        "load_config",
        lambda **_kwargs: {"DEFAULT_DATABASE": "kuzudb"},
    )

    assert not (workspace / ".codegraphcontext").exists()
    with pytest.raises(cli_main.typer.BadParameter, match="single repository scope"):
        cli_main._resolve_cli_repo_scope(start=workspace)


def test_cli_global_scope_still_fails_closed_for_ambiguous_workspace(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    (workspace / "project-a" / ".git").mkdir(parents=True)
    (workspace / "project-b" / ".git").mkdir(parents=True)

    class _ContextConfig:
        mode = "global"
        default_context = ""
        contexts = {}

    monkeypatch.setattr(
        cli_main.config_manager,
        "load_context_config",
        lambda **_kwargs: _ContextConfig(),
    )

    with pytest.raises(cli_main.typer.BadParameter, match="single repository scope"):
        cli_main._resolve_cli_repo_scope(start=workspace)


def test_cli_selected_multi_repository_context_fails_closed_outside_its_repos(
    monkeypatch, tmp_path
):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    cwd = tmp_path / "outside"
    (repo_a / ".git").mkdir(parents=True)
    (repo_b / ".git").mkdir(parents=True)
    (cwd / ".git").mkdir(parents=True)

    class _Context:
        repos = [repo_a.as_posix(), repo_b.as_posix()]

    class _ContextConfig:
        mode = "global"
        default_context = ""
        contexts = {"selected": _Context()}

    monkeypatch.setattr(cli_main.config_manager, "load_context_config", lambda **_kwargs: _ContextConfig())
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _FakeCodeFinder()),
    )
    monkeypatch.chdir(cwd)

    result = runner.invoke(
        app,
        ["find", "name", "target", "--type", "function", "--context", "selected"],
    )

    assert result.exit_code == 2
    assert "inside one of that context's registered repositories" in result.output


@pytest.mark.parametrize(
    ("command", "method"),
    [
        (["find", "decorator", "route"], "find_functions_by_decorator"),
        (["find", "argument", "account_id"], "find_functions_by_argument"),
        (["analyze", "calls", "entry"], "what_does_function_call"),
        (["analyze", "callers", "entry"], "who_calls_function"),
        (["analyze", "deps", "package"], "find_module_dependencies"),
        (["analyze", "tree", "Model"], "find_class_hierarchy"),
        (["analyze", "complexity"], "find_most_complex_functions"),
        (["analyze", "dead-code"], "find_dead_code"),
        (["analyze", "overrides", "render"], "find_function_overrides"),
        (["analyze", "variable", "setting"], "find_variable_usage_scope"),
    ],
)
def test_cli_analysis_queries_pass_the_active_repository_scope(
    monkeypatch, tmp_path, command, method
):
    repo = tmp_path.resolve().as_posix()
    (tmp_path / ".git").mkdir()
    calls = []

    class _CodeFinder:
        def __getattr__(self, name):
            def query(*_args, **kwargs):
                calls.append((name, kwargs))
                if name == "find_module_dependencies":
                    return {"importers": [], "imports": []}
                if name == "find_class_hierarchy":
                    return {"parent_classes": [], "child_classes": [], "methods": []}
                if name == "find_dead_code":
                    return {"potentially_unused_functions": [], "total_count": 0}
                if name == "find_variable_usage_scope":
                    return {"instances": []}
                return []
            return query

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, command)

    assert result.exit_code == 0, result.output
    assert calls[0][0] == method
    assert calls[0][1]["repo_path"] == repo


def test_find_decorator_forwards_nested_pages_and_prints_continuations(
    monkeypatch, tmp_path
):
    (tmp_path / ".git").mkdir()
    calls = []

    class _CodeFinder:
        def find_functions_by_decorator(self, *_args, **kwargs):
            calls.append(kwargs)
            return [{
                "function_name": "endpoint",
                "path": f"{tmp_path}/routes.py",
                "line_number": 4,
                "decorators": ["@route-preview"],
                "decorators_truncated": True,
                "decorators_next_offset": 24,
                "decorators_value_next_offset": 192,
            }]

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [
        "find", "decorator", "route",
        "--decorator-offset", "16",
        "--decorator-page-size", "8",
        "--decorator-value-offset", "128",
        "--decorator-value-page-size", "64",
    ])

    assert result.exit_code == 0, result.output
    assert calls[0]["decorator_offset"] == 16
    assert calls[0]["decorator_page_size"] == 8
    assert calls[0]["decorator_value_offset"] == 128
    assert calls[0]["decorator_value_page_size"] == 64
    assert "continue decorator lists, use --decorator-offset 24" in result.output
    assert "continue decorator text, use --decorator-value-offset 192" in result.output


def test_analyze_dependencies_forwards_page_window_and_collection_cursor(
    monkeypatch, tmp_path
):
    (tmp_path / ".git").mkdir()
    calls = []

    class _PagedCodeFinder(_FakeCodeFinder):
        def find_module_dependencies(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {
                "importers": [{
                    "importer_file_path": "repo/consumer.py",
                    "import_line_number": 1,
                }],
                "imports": [],
                "offset": 4,
                "page_size": 2,
                "importers_truncated": True,
                "imports_truncated": False,
                "next_offset": 6,
                "continuation_limit_reached": False,
            }

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (
            _FakeDBManager(), _FakeGraphBuilder(), _PagedCodeFinder()
        ),
    )

    result = runner.invoke(
        app, ["analyze", "deps", "package", "--offset", "4", "--page-size", "2"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0] == ("package",)
    assert calls[0][1]["offset"] == 4
    assert calls[0][1]["page_size"] == 2
    assert "importers" in result.output.lower()
    assert "--offset 6" in result.output
    assert "--page-size 2" in result.output


def test_analyze_tree_forwards_page_window_and_per_collection_cursors(
    monkeypatch, tmp_path
):
    (tmp_path / ".git").mkdir()
    calls = []

    class _PagedCodeFinder(_FakeCodeFinder):
        def find_class_hierarchy(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {
                "parent_classes": [{
                    "parent_class": "Base",
                    "parent_file_path": "repo/base.py",
                    "parent_line_number": 1,
                }],
                "child_classes": [],
                "methods": [{"method_name": "run", "method_args": "self"}],
                "parent_classes_truncated": True,
                "parent_classes_next_offset": 6,
                "parent_classes_continuation_limit_reached": False,
                "child_classes_truncated": False,
                "child_classes_next_offset": None,
                "child_classes_continuation_limit_reached": False,
                "methods_truncated": True,
                "methods_next_offset": 6,
                "methods_continuation_limit_reached": False,
                "offset": 4,
                "page_size": 2,
            }

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (
            _FakeDBManager(), _FakeGraphBuilder(), _PagedCodeFinder()
        ),
    )

    result = runner.invoke(
        app, ["analyze", "tree", "Model", "--offset", "4", "--page-size", "2"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0] == ("Model", None)
    assert calls[0][1]["offset"] == 4
    assert calls[0][1]["page_size"] == 2
    assert "Parents" in result.output
    assert "Methods" in result.output
    assert result.output.count("--offset 6") >= 2
    assert "--page-size 2" in result.output


@pytest.mark.parametrize(
    ("command", "visualizer", "finder_result"),
    [
        (
            ["analyze", "deps", "package"],
            "visualize_dependencies",
            {"importers": [{"importer_file_path": "repo/a.py"}], "imports": []},
        ),
        (
            ["analyze", "tree", "Model"],
            "visualize_inheritance_tree",
            {"parent_classes": [{"parent_class": "Base"}], "child_classes": [], "methods": []},
        ),
    ],
)
def test_analyze_visualization_forwards_selected_page_window(
    monkeypatch, tmp_path, command, visualizer, finder_result
):
    (tmp_path / ".git").mkdir()
    calls = []

    class _PagedCodeFinder(_FakeCodeFinder):
        def find_module_dependencies(self, *_args, **_kwargs):
            return finder_result

        def find_class_hierarchy(self, *_args, **_kwargs):
            return finder_result

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (
            _FakeDBManager(), _FakeGraphBuilder(), _PagedCodeFinder()
        ),
    )
    monkeypatch.setattr(
        cli_main,
        visualizer,
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = runner.invoke(
        app, [*command, "--visual", "--offset", "4", "--page-size", "2"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0][1]["offset"] == 4
    assert calls[0][1]["page_size"] == 2


def test_analyze_dead_code_pages_and_prints_its_next_offset(monkeypatch):
    calls = []
    db_manager = MagicMock()
    code_finder = MagicMock()
    code_finder.find_dead_code.side_effect = lambda *args, **kwargs: (
        calls.append((args, kwargs))
        or {
            "potentially_unused_functions": [
                {"function_name": "unused_two", "path": "/repo/two.py", "line_number": 2},
                {"function_name": "unused_three", "path": "/repo/three.py", "line_number": 3},
            ],
            "total_count": 5,
            "offset": 2,
            "page_size": 2,
            "truncated": True,
            "next_offset": 4,
            "continuation_limit_reached": False,
            "confidence_counts": {"high": 5, "medium": 0, "low": 0},
            "note": "Static approximation",
        }
    )
    monkeypatch.setattr(cli_main, "_load_credentials", lambda: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (db_manager, object(), code_finder),
    )
    monkeypatch.setattr(cli_main, "get_tool_result_limit", lambda *_a, **_k: 2)

    result = runner.invoke(app, ["analyze", "dead-code", "/repo", "--offset", "2"])

    assert result.exit_code == 0, result.output
    assert calls[0][1]["offset"] == 2
    assert calls[0][1]["limit"] == 2
    assert "unused_two" in result.output and "unused_three" in result.output
    assert "Total: 5 function(s)" in result.output
    assert "continue with --offset 4" in result.output


def test_report_defaults_to_the_active_repository_scope(monkeypatch, tmp_path):
    repo = tmp_path.resolve().as_posix()
    (tmp_path / ".git").mkdir()
    calls = []

    def generate_report(_db_manager, **kwargs):
        calls.append(kwargs)
        return "# scoped report"

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_FakeDBManager(), None, None, None),
    )
    monkeypatch.setattr(
        "codegraphcontext.tools.report_generator.generate_report", generate_report
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["report"])

    assert result.exit_code == 0, result.output
    assert calls[0]["repo_path"] == repo


def test_find_pattern_scopes_and_caps_cli_results(monkeypatch, tmp_path):
    calls = []
    repo_path = tmp_path.resolve().as_posix()
    (tmp_path / ".git").mkdir()

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def run(self, query, **kwargs):
            calls.append((query, kwargs))
            return [
                {
                    "type": "Function",
                    "name": f"match-{index}",
                    "path": f"{repo_path}/src/{index}.py",
                    "line_number": index,
                    "is_dependency": False,
                }
                for index in range(51)
            ]

    class _DBManager:
        def get_driver(self):
            return types.SimpleNamespace(session=lambda: _Session())

        def close_driver(self):
            return None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_DBManager(), _FakeGraphBuilder(), _FakeCodeFinder()),
    )

    result = runner.invoke(app, ["find", "pattern", "match-"])

    assert result.exit_code == 0, result.output
    queries = [query for query, _ in calls]
    path_query = next(query for query in queries if "MATCH (n)" in query)
    module_query = next(
        query for query in queries if "MATCH (file:File)-[:IMPORTS]->(n:Module)" in query
    )
    assert any("n.path = $repo_root" in query for query in queries)
    assert any("n.path STARTS WITH $repo_prefix" in query for query in queries)
    assert any("MATCH (file:File)-[:IMPORTS]->(n:Module)" in query for query in queries)
    assert "n.uid" in path_query
    assert "ORDER BY is_dependency ASC, name, path, line_number" in module_query
    assert "n.uid" not in module_query
    assert "file.uid" not in module_query
    assert all(params["repo_root"] == repo_path for _, params in calls)
    assert all(params["repo_prefix"] == repo_path.rstrip("/") + "/" for _, params in calls)
    assert all(params["limit"] == 51 for _, params in calls)
    assert "match-50" not in result.output
    assert "truncated" in result.output.lower()


def test_find_pattern_module_query_runs_with_kuzu_primary_keys(monkeypatch, tmp_path):
    pytest.importorskip("kuzu")
    from codegraphcontext.core.database_kuzu import KuzuDBManager

    repo_path = tmp_path.resolve().as_posix()
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("CGC_EMBEDDED_BUFFER_POOL_MB", "256")
    manager = KuzuDBManager(str(tmp_path / "db"))
    try:
        driver = manager.get_driver()
        with driver.session() as session:
            session.run(
                "CREATE (:File {path: $path, name: 'app.py', relative_path: 'app.py', "
                "is_dependency: false})",
                path=f"{repo_path}/app.py",
            )
            for module_name in ("match-beta", "match-alpha"):
                session.run(
                    "CREATE (:Module {name: $name, lang: 'python', full_import_name: $name, "
                    "path: $path, line_number: 1})",
                    name=module_name,
                    path=f"{repo_path}/app.py",
                )
                session.run(
                    "MATCH (file:File {path: $path}), (module:Module {name: $name}) "
                    "CREATE (file)-[:IMPORTS {line_number: 1}]->(module)",
                    path=f"{repo_path}/app.py",
                    name=module_name,
                )

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            cli_main,
            "_initialize_services",
            lambda *_args, **_kwargs: (
                manager, _FakeGraphBuilder(), _FakeCodeFinder()
            ),
        )

        result = runner.invoke(app, ["find", "pattern", "match-"])

        assert result.exit_code == 0, result.output
        assert "match-alpha" in result.output
        assert "match-beta" in result.output
        assert result.output.index("match-alpha") < result.output.index("match-beta")
    finally:
        manager.close_driver()


def test_find_type_passes_repository_scope_and_caps_cli_results(monkeypatch, tmp_path):
    calls = []
    repo_path = tmp_path.resolve().as_posix()
    (tmp_path / ".git").mkdir()

    class _CodeFinder:
        def find_by_type(self, element_type, limit=50, repo_path=None):
            calls.append((element_type, limit, repo_path))
            return [
                {
                    "name": f"match-{index}",
                    "path": f"{repo_path}/src/{index}.py",
                    "line_number": index,
                    "is_dependency": False,
                }
                for index in range(limit)
            ]

    class _DBManager:
        def close_driver(self):
            return None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_DBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )

    result = runner.invoke(app, ["find", "type", "function"])

    assert result.exit_code == 0, result.output
    assert calls == [("function", 51, repo_path)]
    assert "match-50" not in result.output
    assert "truncated" in result.output.lower()


def test_find_name_file_search_is_bounded_and_reports_more(monkeypatch, tmp_path):
    calls = []
    repo_path = tmp_path.resolve().as_posix()
    (tmp_path / ".git").mkdir()

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def run(self, query, **kwargs):
            calls.append((query, kwargs))
            return [
                {
                    "name": "same.py",
                    "path": f"{repo_path}/src/{index}/same.py",
                    "is_dependency": False,
                }
                for index in range(51)
            ]

    class _DBManager:
        def get_driver(self):
            return types.SimpleNamespace(session=lambda: _Session())

        def close_driver(self):
            return None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_DBManager(), _FakeGraphBuilder(), _FakeCodeFinder()),
    )

    result = runner.invoke(app, ["find", "name", "same.py", "--type", "file"])

    assert result.exit_code == 0, result.output
    query, params = calls[0]
    assert "LIMIT $limit" in query
    assert params["limit"] == 51
    assert params["repo_root"] == repo_path
    assert params["repo_prefix"] == repo_path.rstrip("/") + "/"
    assert "Found 50 matches" in result.output
    assert "/src/49/same.py" in result.output
    assert "/src/50/same.py" not in result.output
    assert "truncated" in result.output.lower()


def test_find_name_named_type_queries_are_bounded_and_report_more(monkeypatch, tmp_path):
    calls = []
    repo_path = tmp_path.resolve().as_posix()
    (tmp_path / ".git").mkdir()

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def run(self, query, **kwargs):
            calls.append((query, kwargs))
            return [
                {
                    "name": f"SameName-{index}",
                    "path": f"{repo_path}/src/{index}.rs",
                    "line_number": index,
                }
                for index in range(51)
            ]

    class _DBManager:
        def get_driver(self):
            return types.SimpleNamespace(session=lambda: _Session())

        def close_driver(self):
            return None

    class _EmptyCodeFinder(_FakeCodeFinder):
        def find_by_function_name(self, *_args, **_kwargs):
            return []

        def find_by_class_name(self, *_args, **_kwargs):
            return []

        def find_by_variable_name(self, *_args, **_kwargs):
            return []

        def find_by_module_name(self, *_args, **_kwargs):
            return []

        def find_imports(self, *_args, **_kwargs):
            return []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_DBManager(), _FakeGraphBuilder(), _EmptyCodeFinder()),
    )

    result = runner.invoke(app, ["find", "name", "SameName"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 4
    assert all("LIMIT $limit" in query for query, _ in calls)
    assert all(params["limit"] == 51 for _, params in calls)
    assert all(params["repo_root"] == repo_path for _, params in calls)
    assert "truncated" in result.output.lower()
    assert "Found 50 matches" in result.output
    assert "SameName-49" in result.output
    assert "SameName-50" not in result.output


def test_diagram_defaults_scope_to_current_repository(monkeypatch, tmp_path):
    captured = []
    (tmp_path / ".git").mkdir()

    class RecordingCodeFinder(_FakeCodeFinder):
        def export_diagram_edges(self, **kwargs):
            captured.append(kwargs)
            return {"edges": [], "total_count": 0, "truncated": False}

    monkeypatch.setattr(cli_main, "_load_credentials", lambda: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda _context: (_FakeDBManager(), _FakeGraphBuilder(), RecordingCodeFinder()),
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["diagram"])

    assert result.exit_code == 0, result.output
    assert captured[0]["repo_path"] == str(tmp_path.resolve())


def test_find_variable_requests_context_for_cli_column(monkeypatch):
    calls = []

    class _DBManager:
        def close_driver(self):
            return None

    class _CodeFinder:
        def find_by_variable_name(self, name, **kwargs):
            calls.append((name, kwargs))
            return [{
                "name": name,
                "path": "repo/main.py",
                "line_number": 4,
                "context": "configuration = 42",
            }]

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "_initialize_services",
        lambda *_args, **_kwargs: (_DBManager(), _FakeGraphBuilder(), _CodeFinder()),
    )

    result = runner.invoke(app, ["find", "variable", "configuration"])

    assert result.exit_code == 0, result.output
    assert "configuration = 42" in result.output
    assert calls == [(
        "configuration",
        {
            "include_context": True,
            "repo_path": str(Path.cwd().resolve()),
            "limit": 51,
        },
    )]
    assert "Context (preview)" in result.output


def test_db_flag_kuzudb_not_overwritten_by_context_database(monkeypatch):
    class _Ctx:
        mode = "named"
        context_name = "test"
        database = "neo4j"
        db_path = None
        cgcignore_path = None

    class _FakeResult:
        def single(self):
            return {"repo_count": 0, "dep_count": 0}

        def __iter__(self):
            return iter([])

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def run(self, *_args, **_kwargs):
            return _FakeResult()

    class _FakeDriver:
        def session(self):
            return _FakeSession()

    class _FakeManager:
        def get_driver(self, graph_name=None):
            return _FakeDriver()

        def close_driver(self):
            return None

    monkeypatch.setattr(cli_main, "_load_credentials", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_helpers, "resolve_context", lambda *_args, **_kwargs: _Ctx())
    monkeypatch.setattr(cli_helpers, "ensure_first_run_bootstrap", lambda *_args, **_kwargs: None)

    def _fake_get_database_manager(*_args, **_kwargs):
        # --db kuzudb must remain effective, even when context prefers neo4j.
        assert os.environ.get("CGC_RUNTIME_DB_TYPE") == "kuzudb"
        return _FakeManager()

    monkeypatch.setattr(cli_helpers, "get_database_manager", _fake_get_database_manager)

    # Use a clean environment so the command path is deterministic.
    clean_env = {k: v for k, v in os.environ.items() if k not in {"CGC_RUNTIME_DB_TYPE", "DEFAULT_DATABASE", "DATABASE_TYPE"}}
    with patch.dict(os.environ, clean_env, clear=True):
        result = runner.invoke(app, ["--db", "kuzudb", "list"])

    assert result.exit_code == 0, result.output
    assert "Database Connection Error" not in result.output


class TestNeo4jDatabaseNameCLI:
    """Integration tests for NEO4J_DATABASE display in CLI commands."""

    @patch("codegraphcontext.cli.main.config_manager")
    @patch("codegraphcontext.core.database.DatabaseManager.test_connection")
    def test_doctor_passes_database_to_test_connection(self, mock_test_conn, mock_config_mgr):
        """Test that the doctor command passes NEO4J_DATABASE to test_connection."""
        mock_config_mgr.load_config.return_value = {"DEFAULT_DATABASE": "neo4j"}
        mock_config_mgr.CONFIG_FILE = MagicMock()
        mock_config_mgr.CONFIG_FILE.exists.return_value = True
        mock_config_mgr.validate_config_value.return_value = (True, None)
        mock_test_conn.return_value = (True, None)

        env = {
            "NEO4J_URI": "bolt://localhost:7687",
            "NEO4J_USERNAME": "neo4j",
            "NEO4J_PASSWORD": "password",
            "NEO4J_DATABASE": "mydb",
            "DEFAULT_DATABASE": "neo4j",
            # The credential loader is mocked below, so set the selected backend
            # directly instead of inheriting a previous test's selection.
            "CGC_SELECTED_DATABASE": "neo4j",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("codegraphcontext.cli.main._load_credentials"):
                runner.invoke(app, ["doctor"])

        mock_test_conn.assert_called_once_with(
            "bolt://localhost:7687", "neo4j", "password", database="mydb"
        )

    @patch("codegraphcontext.cli.main.config_manager")
    def test_load_credentials_displays_database_name(self, mock_config_mgr, monkeypatch, tmp_path):
        """Test _load_credentials prints database name when NEO4J_DATABASE is set."""
        mock_config_mgr.ensure_config_dir.return_value = None

        env = {
            "NEO4J_URI": "bolt://localhost:7687",
            "NEO4J_USERNAME": "neo4j",
            "NEO4J_PASSWORD": "password",
            "NEO4J_DATABASE": "mydb",
            "DEFAULT_DATABASE": "neo4j",
        }
        monkeypatch.chdir(tmp_path)
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in {
                "DEFAULT_DATABASE",
                "CGC_RUNTIME_DB_TYPE",
                "NEO4J_URI",
                "NEO4J_USERNAME",
                "NEO4J_PASSWORD",
                "NEO4J_DATABASE",
            }
        }
        clean_env.update(env)
        with patch.dict(os.environ, clean_env, clear=True):
            output = StringIO()
            with patch("codegraphcontext.cli.main.console", Console(file=output, force_terminal=False)):
                _load_credentials()

            printed = output.getvalue()
            lowered = printed.lower()
            assert "using database: neo4j" in lowered
            assert "database: mydb" in lowered
            assert "source:" in lowered

    @patch("codegraphcontext.cli.main.config_manager")
    def test_load_credentials_no_database_name(self, mock_config_mgr, monkeypatch, tmp_path):
        """Test _load_credentials prints Neo4j without database when NEO4J_DATABASE is not set."""
        mock_config_mgr.ensure_config_dir.return_value = None

        env = {
            "NEO4J_URI": "bolt://localhost:7687",
            "NEO4J_USERNAME": "neo4j",
            "NEO4J_PASSWORD": "password",
            "DEFAULT_DATABASE": "neo4j",
        }
        monkeypatch.chdir(tmp_path)
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in {
                "DEFAULT_DATABASE",
                "CGC_RUNTIME_DB_TYPE",
                "NEO4J_URI",
                "NEO4J_USERNAME",
                "NEO4J_PASSWORD",
                "NEO4J_DATABASE",
            }
        }
        clean_env.update(env)
        with patch.dict(os.environ, clean_env, clear=True):
            output = StringIO()
            with patch("codegraphcontext.cli.main.console", Console(file=output, force_terminal=False)):
                _load_credentials()

            printed = output.getvalue()
            lowered = printed.lower()
            assert "using database: neo4j" in lowered
            assert "source:" in lowered
            assert "(database:" not in lowered


def test_load_credentials_displays_kuzudb_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_main.config_manager, "ensure_config_dir", lambda *_args, **_kwargs: None)

    monkeypatch.chdir(tmp_path)
    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in {
            "DEFAULT_DATABASE",
            "CGC_RUNTIME_DB_TYPE",
            "NEO4J_URI",
            "NEO4J_USERNAME",
            "NEO4J_PASSWORD",
            "NEO4J_DATABASE",
        }
    }
    clean_env["DEFAULT_DATABASE"] = "kuzudb"
    with patch.dict(os.environ, clean_env, clear=True):
        output = StringIO()
        with patch("codegraphcontext.cli.main.console", Console(file=output, force_terminal=False)):
            _load_credentials()

        lowered = output.getvalue().lower()
        assert "using database: kuzudb" in lowered
        assert "source:" in lowered


def test_load_credentials_normalizes_tilde_paths_from_mcp_json(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main.config_manager, "ensure_config_dir", lambda *_args, **_kwargs: None)

    mcp_data = {
        "mcpServers": {
            "CodeGraphContext": {
                "env": {
                    "DEFAULT_DATABASE": "falkordb",
                    "FALKORDB_PATH": "~/.codegraphcontext/global/db/falkordb",
                    "FALKORDB_SOCKET_PATH": "~/.codegraphcontext/global/db/falkordb.sock",
                    "DEBUG_LOG_PATH": "~/mcp_debug.log",
                    "LOG_FILE_PATH": "~/.codegraphcontext/logs/cgc.log",
                }
            }
        }
    }
    (tmp_path / "mcp.json").write_text(str(mcp_data).replace("'", '"'), encoding="utf-8")

    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in {
            "DEFAULT_DATABASE",
            "CGC_RUNTIME_DB_TYPE",
            "FALKORDB_PATH",
            "FALKORDB_SOCKET_PATH",
            "DEBUG_LOG_PATH",
            "LOG_FILE_PATH",
        }
    }
    with patch.dict(os.environ, clean_env, clear=True):
        _load_credentials()

        assert os.environ["FALKORDB_PATH"] == str((tmp_path / ".codegraphcontext" / "global" / "db" / "falkordb").resolve())
        assert os.environ["FALKORDB_SOCKET_PATH"] == str((tmp_path / ".codegraphcontext" / "global" / "db" / "falkordb.sock").resolve())
        assert os.environ["DEBUG_LOG_PATH"] == str((tmp_path / "mcp_debug.log").resolve())
        assert os.environ["LOG_FILE_PATH"] == str((tmp_path / ".codegraphcontext" / "logs" / "cgc.log").resolve())


def test_load_credentials_utf8_decoding_robustness(monkeypatch, tmp_path):
    """Test that _load_credentials successfully handles .env files with invalid UTF-8 bytes."""
    class _Ctx:
        mode = "global"
        context_name = ""
        database = "falkordb"
        db_path = "/path/to/db"
        cgcignore_path = "/path/to/cgcignore"

    global_dir = tmp_path / ".codegraphcontext"
    global_env_path = global_dir / ".env"
    monkeypatch.setattr(cli_main.config_manager, "CONFIG_DIR", global_dir)
    monkeypatch.setattr(cli_main.config_manager, "CONFIG_FILE", global_env_path)
    monkeypatch.setattr(cli_main.config_manager, "CONTEXT_CONFIG_FILE", global_dir / "config.yaml")
    monkeypatch.setattr(cli_main.config_manager, "ensure_config_dir", lambda *_args, **_kwargs: None)
    monkeypatch.chdir(tmp_path)
    
    import codegraphcontext.cli.config_manager as config_manager
    monkeypatch.setattr(config_manager, "resolve_context", lambda *_args: _Ctx())
    
    # We will write invalid UTF-8 bytes to the global .env file path
    monkeypatch.setenv("HOME", str(tmp_path))
    
    global_dir.mkdir(parents=True, exist_ok=True)
    
    # Write invalid UTF-8 bytes to the file (e.g. 0x97 invalid start byte)
    global_env_path.write_bytes(b"DEFAULT_DATABASE=ladybugdb\n# invalid byte: \x97\n")
    
    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in {"DEFAULT_DATABASE", "CGC_RUNTIME_DB_TYPE", "DATABASE_TYPE"}
    }
    with patch.dict(os.environ, clean_env, clear=True):
        output = StringIO()
        with patch("codegraphcontext.cli.main.console", Console(file=output, force_terminal=False)):
            _load_credentials()
        
        # DEFAULT_DATABASE should still be loaded successfully despite the invalid byte!
        assert os.environ.get("DEFAULT_DATABASE") == "ladybugdb"
        assert "Warning" not in output.getvalue()


def test_load_credentials_resolves_context_database(monkeypatch, tmp_path):
    """Test that _load_credentials resolves context database and sets DEFAULT_DATABASE accordingly."""
    class _Ctx:
        mode = "named"
        context_name = "TinyCTX"
        database = "ladybugdb"
        db_path = "/path/to/db"
        cgcignore_path = "/path/to/cgcignore"

    monkeypatch.setattr(cli_main.config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli_main.config_manager, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cli_main.config_manager, "CONTEXT_CONFIG_FILE", tmp_path / "config.yaml")
    monkeypatch.setattr(cli_main.config_manager, "ensure_config_dir", lambda *_args, **_kwargs: None)
    monkeypatch.chdir(tmp_path)
    
    import codegraphcontext.cli.config_manager as config_manager
    monkeypatch.setattr(config_manager, "resolve_context", lambda cli_context_flag: _Ctx() if cli_context_flag == "TinyCTX" else None)

    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in {"DEFAULT_DATABASE", "CGC_RUNTIME_DB_TYPE", "DATABASE_TYPE"}
    }
    with patch.dict(os.environ, clean_env, clear=True):
        output = StringIO()
        with patch("codegraphcontext.cli.main.console", Console(file=output, force_terminal=False)):
            _load_credentials(cli_context_flag="TinyCTX")
        
        # It should resolve context database to ladybugdb and override DEFAULT_DATABASE
        assert os.environ.get("DEFAULT_DATABASE") == "ladybugdb"
        lowered = output.getvalue().lower()
        assert "using database: ladybugdb" in lowered
        assert "context (tinyctx)" in lowered
