"""End-to-end live watcher: real watchdog events → debounce → graph updates.

The startup-sync and handler internals are unit-tested; this covers the one
path nothing exercised — actual filesystem events driving incremental
updates on a real embedded database.
"""
import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from codegraphcontext.core.database_kuzu import KuzuDBManager
from codegraphcontext.core.watcher import CodeWatcher, RepositoryEventHandler
from codegraphcontext.tools.graph_builder import GraphBuilder

kuzu = pytest.importorskip("kuzu")


class _DBM:
    def __init__(self, d): self._d = d
    def get_driver(self, graph_name=None): return self._d
    def get_backend_type(self): return "kuzudb"


class _JM:
    def update_job(self, *a, **k): pass


def _wait_until(predicate, timeout=30.0, interval=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def _graph_semantics(driver, repo: Path):
    prefix = repo.resolve().as_posix() + "/"
    with driver.session() as session:
        files = session.run(
            "MATCH (f:File) WHERE f.path STARTS WITH $prefix "
            "RETURN f.path AS path, f.name AS name",
            prefix=prefix,
        ).data()
        functions = session.run(
            "MATCH (f:Function) WHERE f.path STARTS WITH $prefix "
            "RETURN f.path AS path, f.name AS name, f.line_number AS line",
            prefix=prefix,
        ).data()
        classes = session.run(
            "MATCH (c:Class) WHERE c.path STARTS WITH $prefix "
            "RETURN c.path AS path, c.name AS name, c.line_number AS line",
            prefix=prefix,
        ).data()
        calls = session.run(
            "MATCH (caller:Function)-[r:CALLS|HEURISTIC_CALLS]->(callee:Function) "
            "WHERE caller.path STARTS WITH $prefix "
            "RETURN caller.path AS caller_path, caller.name AS caller_name, "
            "caller.line_number AS caller_line, type(r) AS relationship, "
            "callee.path AS callee_path, callee.name AS callee_name, "
            "callee.line_number AS callee_line",
            prefix=prefix,
        ).data()
        inheritance = session.run(
            "MATCH (child:Class)-[r:INHERITS]->(parent) "
            "WHERE child.path STARTS WITH $prefix "
            "RETURN child.path AS child_path, child.name AS child_name, "
            "type(r) AS relationship, parent.path AS parent_path, "
            "parent.name AS parent_name",
            prefix=prefix,
        ).data()
        imports = session.run(
            "MATCH (f:File)-[r:IMPORTS]->(m:Module) "
            "WHERE f.path STARTS WITH $prefix "
            "RETURN f.path AS file_path, type(r) AS relationship, "
            "r.line_number AS line, r.imported_name AS imported_name, m.name AS module_name",
            prefix=prefix,
        ).data()

    def ordered(rows):
        return tuple(sorted((tuple(row.values()) for row in rows), key=repr))

    return {
        "files": ordered(files),
        "functions": ordered(functions),
        "classes": ordered(classes),
        "calls": ordered(calls),
        "inheritance": ordered(inheritance),
        "imports": ordered(imports),
    }


def _clean_graph_semantics(repo: Path, db_path: Path):
    manager = KuzuDBManager(str(db_path))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), _JM(), loop)
    try:
        loop.run_until_complete(builder.build_graph_from_path_async(repo, is_dependency=False))
        return _graph_semantics(driver, repo)
    finally:
        manager.close_driver()
        loop.close()


def test_watcher_applies_modify_create_and_delete(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def one():\n    pass\n", encoding="utf-8")

    manager = KuzuDBManager(str(tmp_path / "db"))
    driver = manager.get_driver()
    gb = GraphBuilder(_DBM(driver), _JM(), asyncio.new_event_loop())
    asyncio.new_event_loop().run_until_complete(
        gb.build_graph_from_path_async(repo, is_dependency=False)
    )

    def funcs():
        with driver.session() as s:
            return sorted(
                r["n"] for r in s.run(
                    "MATCH (f:Function) WHERE f.name <> '<module>' RETURN f.name AS n"
                ).data()
            )

    assert funcs() == ["one"]

    watcher = CodeWatcher(gb)
    watcher.watch_directory(str(repo), perform_initial_scan=False)
    watcher.start()
    try:
        (repo / "a.py").write_text(
            "def one():\n    pass\n\ndef two():\n    return 1\n", encoding="utf-8"
        )
        assert _wait_until(lambda: "two" in funcs()), (
            f"modify event never reached the graph: {funcs()}"
        )

        (repo / "b.py").write_text("def three():\n    return 3\n", encoding="utf-8")
        assert _wait_until(lambda: "three" in funcs()), (
            f"create event never reached the graph: {funcs()}"
        )

        (repo / "b.py").unlink()
        assert _wait_until(lambda: "three" not in funcs()), (
            f"delete event never reached the graph: {funcs()}"
        )

        assert funcs() == ["one", "two"]
    finally:
        watcher.stop()
        manager.close_driver()


@pytest.mark.integration
def test_incremental_graph_matches_clean_rebuild_after_file_and_branch_batches(tmp_path: Path):
    repo = tmp_path / "disposable-repo"
    repo.mkdir()
    (repo / "base.py").write_text(
        "class Animal:\n"
        "    def sound(self):\n"
        "        return 'base'\n\n"
        "class Dog(Animal):\n"
        "    def bark(self):\n"
        "        return self.sound()\n\n"
        "def helper():\n"
        "    return 3\n",
        encoding="utf-8",
    )
    (repo / "client.py").write_text(
        "from base import Dog, helper\n"
        "from extra import extra\n\n"
        "def run():\n"
        "    dog = Dog()\n"
        "    return dog.bark() + helper() + extra()\n",
        encoding="utf-8",
    )
    (repo / "client.js").write_text(
        "import * as extra_module from './extra.js';\n"
        "export function run_js() { return extra_module.extra_js(); }\n",
        encoding="utf-8",
    )
    (repo / "client_default.js").write_text(
        "import extra_default from './extra.js';\n\n"
        "export function run_default() { return extra_default(); }\n",
        encoding="utf-8",
    )
    (repo / "client_require.js").write_text(
        "const extra_module = require('./extra.js');\n"
        "export function run_require() { return extra_module.extra_js(); }\n",
        encoding="utf-8",
    )

    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Watcher Test")
    _git(repo, "config", "user.email", "watcher-test@example.invalid")
    _git(
        repo,
        "add",
        "base.py",
        "client.py",
        "client.js",
        "client_default.js",
        "client_require.js",
    )
    _git(repo, "commit", "-m", "initial")

    incremental_db = tmp_path / "incremental-db"
    manager = KuzuDBManager(str(incremental_db))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), _JM(), loop)
    handler = None
    try:
        handler = RepositoryEventHandler(builder, repo, perform_initial_scan=True)

        def assert_matches_clean_build(label: str, build_index: int) -> None:
            nonlocal manager, driver, builder
            actual = _graph_semantics(driver, repo)
            # KuzuDBManager is a singleton and switches databases by closing
            # the previous driver, so compare sequentially and then reopen the
            # incremental database before applying the next event.
            manager.close_driver()
            expected = _clean_graph_semantics(repo, tmp_path / f"clean-{build_index}")
            manager = KuzuDBManager(str(incremental_db))
            driver = manager.get_driver()
            builder = GraphBuilder(_DBM(driver), _JM(), loop)
            handler.graph_builder = builder
            assert actual == expected, f"incremental graph diverged from a clean build after {label}"

        assert_matches_clean_build("initial scan", 0)

        added = repo / "extra.py"
        added.write_text("def extra():\n    return 5\n", encoding="utf-8")
        handler._handle_modification(str(added))
        assert_matches_clean_build("addition", 1)

        added_js = repo / "extra.js"
        added_js.write_text(
            "export default function extra_default() { return 1; }\n"
            "export function extra_js() { return 7; }\n",
            encoding="utf-8",
        )
        handler._handle_modification(str(added_js))
        js_calls = _graph_semantics(driver, repo)["calls"]
        assert any(call[1] == "run_js" and call[5] == "extra_js" for call in js_calls), (
            "adding an imported JS module should resolve calls from an unchanged namespace importer"
        )
        selected_importers = handler.graph_builder.get_importer_file_paths(
            set(), repo, added_js
        )
        assert {
            (repo / "client.js").resolve().as_posix(),
            (repo / "client_default.js").resolve().as_posix(),
            (repo / "client_require.js").resolve().as_posix(),
        } <= selected_importers, (
            "relative namespace, default, and CommonJS importers should be selected"
        )
        assert_matches_clean_build("JS namespace module addition", 2)

        client = repo / "client.py"
        client.write_text(
            "from base import Dog, helper\n"
            "from extra import extra\n\n"
            "def run():\n"
            "    dog = Dog()\n"
            "    return dog.bark() + helper() + extra() * 2\n",
            encoding="utf-8",
        )
        handler._handle_modification(str(client))
        assert_matches_clean_build("modification", 3)

        added.unlink()
        handler._handle_modification(str(added))
        assert_matches_clean_build("deletion", 4)

        old_path = repo / "base.py"
        new_path = repo / "relocated.py"
        old_path.rename(new_path)
        handler._handle_move(str(old_path), str(new_path))
        assert_matches_clean_build("move", 5)

        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "record incremental operations")
        _git(repo, "switch", "-c", "feature")
        (repo / "relocated.py").unlink()
        (repo / "feature.py").write_text(
            "class FeatureBase:\n"
            "    def value(self):\n"
            "        return 7\n\n"
            "class Feature(FeatureBase):\n"
            "    def current(self):\n"
            "        return self.value()\n",
            encoding="utf-8",
        )
        (repo / "client.py").write_text(
            "from feature import Feature\n\n"
            "def run():\n"
            "    return Feature().current()\n",
            encoding="utf-8",
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "feature graph state")
        handler.synchronize_with_disk()
        assert_matches_clean_build("feature branch switch", 6)

        _git(repo, "switch", "main")
        handler.synchronize_with_disk()
        assert_matches_clean_build("switch back to main", 7)
    finally:
        if handler is not None:
            handler.stop()
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_python_package_relative_importer_relinks_after_module_definition_is_added(tmp_path: Path):
    repo = tmp_path / "relative-import-repo"
    package = repo / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    importer = package / "client.py"
    # Keep the imported name outside foo.py's definitions to exercise relative module-path matching.
    importer.write_text(
        "from .foo import unrelated_api\n\n"
        "def run():\n"
        "    return added_api()\n",
        encoding="utf-8",
    )
    changed = package / "foo.py"
    changed.write_text("def existing_api():\n    return 1\n", encoding="utf-8")

    manager = KuzuDBManager(str(tmp_path / "relative-incremental-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), _JM(), loop)
    handler = None
    try:
        handler = RepositoryEventHandler(builder, repo, perform_initial_scan=True)
        changed.write_text(
            "def existing_api():\n    return 1\n\n"
            "def added_api():\n    return 2\n",
            encoding="utf-8",
        )
        handler._handle_modification(str(changed))

        importer_path = importer.resolve().as_posix()
        changed_path = changed.resolve().as_posix()
        selected_importers = handler.graph_builder.get_importer_file_paths(
            {"added_api"}, repo, changed
        )
        assert importer_path in selected_importers, (
            "Python package-relative imports should select the unchanged importer"
        )
        actual = _graph_semantics(driver, repo)
        manager.close_driver()
        expected = _clean_graph_semantics(repo, tmp_path / "relative-clean-db")
        manager = KuzuDBManager(str(tmp_path / "relative-incremental-db"))
        driver = manager.get_driver()
        handler.graph_builder = GraphBuilder(_DBM(driver), _JM(), loop)

        def has_expected_call(graph):
            return any(
                call[0] == importer_path
                and call[1] == "run"
                and call[3] in {"CALLS", "HEURISTIC_CALLS"}
                and call[4] == changed_path
                and call[5] == "added_api"
                for call in graph["calls"]
            )

        assert has_expected_call(expected), (
            "the package fixture should resolve the call from a clean rebuild"
        )
        assert has_expected_call(actual), (
            "adding a package-relative import target should relink the unchanged caller"
        )
        assert actual == expected, (
            "incremental graph should match a clean rebuild after adding a definition "
            "to a package-relative import target"
        )
    finally:
        if handler is not None:
            handler.stop()
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_js_repository_root_importers_relink_after_module_definition_is_added(tmp_path: Path):
    repo = tmp_path / "root-import-repo"
    src = repo / "src"
    src.mkdir(parents=True)
    changed = src / "api.js"
    changed.write_text(
        "export default function default_api() { return 1; }\n"
        "export function existing_api() { return 2; }\n",
        encoding="utf-8",
    )
    default_importer = src / "client_default.js"
    default_importer.write_text(
        "import default_api from '/src/api.js';\n"
        "export function run_default() { return default_api(); }\n",
        encoding="utf-8",
    )
    namespace_importer = src / "client_namespace.js"
    namespace_importer.write_text(
        "import * as api from '/src/api.js';\n"
        "export function run_namespace() { return api.added_api(); }\n",
        encoding="utf-8",
    )

    incremental_db = tmp_path / "root-incremental-db"
    manager = KuzuDBManager(str(incremental_db))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), _JM(), loop)
    handler = None
    try:
        handler = RepositoryEventHandler(builder, repo, perform_initial_scan=True)
        changed.write_text(
            "export default function default_api() { return 1; }\n"
            "export function existing_api() { return 2; }\n"
            "export function added_api() { return 3; }\n",
            encoding="utf-8",
        )

        importer_paths = handler.graph_builder.get_importer_file_paths(
            {"added_api"}, repo, changed
        )
        assert {
            default_importer.resolve().as_posix(),
            namespace_importer.resolve().as_posix(),
        } <= importer_paths, (
            "repository-root imports should select both default and namespace importers"
        )

        handler._handle_modification(str(changed))
        actual = _graph_semantics(driver, repo)
        manager.close_driver()
        expected = _clean_graph_semantics(repo, tmp_path / "root-clean-db")
        manager = KuzuDBManager(str(incremental_db))
        driver = manager.get_driver()
        handler.graph_builder = GraphBuilder(_DBM(driver), _JM(), loop)

        assert actual == expected, (
            "incremental graph should match a clean rebuild after changing a JS module "
            "imported from the repository root"
        )
    finally:
        if handler is not None:
            handler.stop()
        manager.close_driver()
        loop.close()
