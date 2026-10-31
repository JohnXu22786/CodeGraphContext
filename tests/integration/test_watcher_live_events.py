"""End-to-end live watcher: real watchdog events → debounce → graph updates.

The startup-sync and handler internals are unit-tested; this covers the one
path nothing exercised — actual filesystem events driving incremental
updates on a real embedded database.
"""
import asyncio
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codegraphcontext.core.database_kuzu import KuzuDBManager
from codegraphcontext.core.jobs import JobManager
from codegraphcontext.core.watcher import CodeWatcher, RepositoryEventHandler
from codegraphcontext.cli import cli_helpers
from codegraphcontext.tools.code_finder import CodeFinder
from codegraphcontext.tools.graph_builder import GraphBuilder
from codegraphcontext.tools import graph_builder as graph_builder_module

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
        repositories = session.run(
            "MATCH (r:Repository {path: $path}) "
            "RETURN r.path AS path, r.commit_hash AS commit_hash",
            path=repo.resolve().as_posix(),
        ).data()
        files = session.run(
            "MATCH (f:File) WHERE f.path STARTS WITH $prefix "
            "RETURN f.path AS path, f.name AS name",
            prefix=prefix,
        ).data()
        functions = session.run(
            "MATCH (f:Function) WHERE f.path STARTS WITH $prefix "
            "RETURN f.path AS path, f.name AS name, f.line_number AS line, f.source AS source",
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
    query_results = CodeFinder(_DBM(driver)).who_calls_function(
        "helper", repo_path=repo.resolve().as_posix()
    )

    def ordered(rows):
        return tuple(sorted((tuple(row.values()) for row in rows), key=repr))

    return {
        "repository": ordered(repositories),
        "files": ordered(files),
        "functions": ordered(functions),
        "classes": ordered(classes),
        "calls": ordered(calls),
        "inheritance": ordered(inheritance),
        "imports": ordered(imports),
        "query_results": ordered(query_results),
    }


def _clean_graph_semantics(repo: Path, db_path: Path, cgcignore_path: str = None):
    manager = KuzuDBManager(str(db_path))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), _JM(), loop)
    try:
        loop.run_until_complete(
            builder.build_graph_from_path_async(
                repo, is_dependency=False, cgcignore_path=cgcignore_path
            )
        )
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
def test_watcher_reconciliation_preserves_repository_dependency_metadata(
    tmp_path: Path,
):
    repo = tmp_path / "dependency-repo"
    repo.mkdir()
    manager = KuzuDBManager(str(tmp_path / "dependency-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), _JM(), loop)
    try:
        builder.add_repository_to_graph(repo, is_dependency=True)
        handler = RepositoryEventHandler(builder, repo, perform_initial_scan=False)

        handler.synchronize_with_disk()

        with driver.session() as session:
            repository = session.run(
                "MATCH (r:Repository {path: $path}) RETURN r.is_dependency AS is_dependency",
                path=repo.resolve().as_posix(),
            ).data()
        assert repository == [{"is_dependency": True}]
    finally:
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_incremental_graph_matches_clean_rebuild_after_file_and_branch_batches(
    tmp_path: Path
):
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
        previous_stat = client.stat()
        client.write_text(
            "from base import Dog, helper\n"
            "from extra import extra\n\n"
            "def run():\n"
            "    dog = Dog()\n"
            "    return dog.bark() + helper() + extra() * 2\n",
            encoding="utf-8",
        )
        # Preserve the original timestamp after changing file contents, so
        # freshness cannot depend on mtime alone.
        os.utime(client, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
        handler._handle_modification(str(client))
        assert_matches_clean_build("modification", 3)

        added.unlink()
        handler._handle_modification(str(added))
        assert_matches_clean_build("deletion", 4)

        subtree = repo / "subtree"
        subtree.mkdir()
        (subtree / "obsolete.py").write_text(
            "def subtree_helper():\n    return 1\n\ndef subtree_caller():\n    return subtree_helper()\n",
            encoding="utf-8",
        )
        handler._handle_modification(str(subtree / "obsolete.py"))
        assert_matches_clean_build("nested subtree addition", 5)

        shutil.rmtree(subtree)
        handler.on_deleted(SimpleNamespace(is_directory=True, src_path=str(subtree)))
        assert _wait_until(
            lambda: not handler._resync_required,
            timeout=15.0,
            interval=0.05,
        ), "deleting a directory did not complete the repository sync"
        assert_matches_clean_build("directory deletion", 6)

        old_path = repo / "base.py"
        new_path = repo / "relocated.py"
        old_path.rename(new_path)
        handler._handle_move(str(old_path), str(new_path))
        assert_matches_clean_build("move", 7)

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
        handler._handle_modification(str(repo / "feature.py"))
        handler.synchronize_with_disk()
        expected_commit = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        assert _graph_semantics(driver, repo)["repository"][0][1] == expected_commit
        assert_matches_clean_build("feature branch switch", 8)

        _git(repo, "switch", "main")
        handler.synchronize_with_disk()
        assert_matches_clean_build("switch back to main", 9)
    finally:
        if handler is not None:
            handler.stop()
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_directory_deletion_relinks_callers_in_other_repositories(tmp_path: Path):
    removed_repo = tmp_path / "a-removed-repository"
    caller_repo = tmp_path / "b-caller-repository"
    removed_package = removed_repo / "pkg"
    caller_package = caller_repo / "pkg"
    removed_package.mkdir(parents=True)
    caller_package.mkdir(parents=True)
    for package in (removed_package, caller_package):
        (package / "__init__.py").write_text("", encoding="utf-8")
    removed_helper = removed_package / "common.py"
    fallback_helper = caller_package / "common.py"
    caller_file = caller_package / "client.py"
    removed_helper.write_text("def helper():\n    return 'removed repository'\n", encoding="utf-8")
    fallback_helper.write_text("def helper():\n    return 'caller repository'\n", encoding="utf-8")
    caller_file.write_text(
        "from .common import helper\n\ndef invoke():\n    return helper()\n",
        encoding="utf-8",
    )

    manager = KuzuDBManager(str(tmp_path / "cross-repository-incremental-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), _JM(), loop)
    handler = None
    try:
        repo_files = [
            (removed_repo, removed_package / "__init__.py"),
            (removed_repo, removed_helper),
            (caller_repo, caller_package / "__init__.py"),
            (caller_repo, fallback_helper),
            (caller_repo, caller_file),
        ]
        for repo in (removed_repo, caller_repo):
            builder.add_repository_to_graph(repo)
        all_file_data = []
        for repo, path in repo_files:
            parsed = builder.parse_file(repo, path)
            all_file_data.append(parsed)
            builder.add_file_to_graph(
                parsed,
                repo.name,
                {},
                repo_path_str=repo.resolve().as_posix(),
            )
        imports_map = builder.pre_scan_imports([path for _, path in repo_files])
        builder.link_function_calls(all_file_data, imports_map)

        def caller_targets(query_driver):
            with query_driver.session() as session:
                rows = session.run(
                    "MATCH (caller:Function)-[:CALLS|HEURISTIC_CALLS]->(callee:Function) "
                    "WHERE caller.path = $caller_path "
                    "RETURN DISTINCT callee.path AS path",
                    caller_path=caller_file.resolve().as_posix(),
                ).data()
            return {row["path"] for row in rows}

        assert caller_targets(driver) == {removed_helper.resolve().as_posix()}

        handler = RepositoryEventHandler(builder, removed_repo, perform_initial_scan=False)
        shutil.rmtree(removed_package)
        handler.synchronize_with_disk()
        actual = _graph_semantics(driver, caller_repo)
        actual_targets = caller_targets(driver)
        manager.close_driver()
        expected = _clean_graph_semantics(caller_repo, tmp_path / "cross-repository-clean-db")

        assert actual_targets == {fallback_helper.resolve().as_posix()}, (
            "deleting a callee in another repository should relink the surviving caller to its remaining helper"
        )
        assert actual == expected, (
            "cross-repository callers should remain equivalent to a clean rebuild after a callee directory is deleted"
        )
    finally:
        if handler is not None:
            handler.stop()
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_existing_callee_file_replacement_relinks_external_callers(tmp_path: Path):
    changed_repo = tmp_path / "a-changed-repository"
    caller_repo = tmp_path / "b-caller-repository"
    changed_package = changed_repo / "pkg"
    caller_package = caller_repo / "pkg"
    changed_package.mkdir(parents=True)
    caller_package.mkdir(parents=True)
    for package in (changed_package, caller_package):
        (package / "__init__.py").write_text("", encoding="utf-8")
    changed_helper = changed_package / "common.py"
    fallback_helper = caller_package / "common.py"
    caller_file = caller_package / "client.py"
    changed_helper.write_text("def helper():\n    return 'changed repository'\n", encoding="utf-8")
    fallback_helper.write_text("def helper():\n    return 'caller repository'\n", encoding="utf-8")
    caller_file.write_text(
        "from .common import helper\n\ndef invoke():\n    return helper()\n",
        encoding="utf-8",
    )

    manager = KuzuDBManager(str(tmp_path / "cross-repository-replacement-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), _JM(), loop)
    handler = None
    try:
        repo_files = [
            (changed_repo, changed_package / "__init__.py"),
            (changed_repo, changed_helper),
            (caller_repo, caller_package / "__init__.py"),
            (caller_repo, fallback_helper),
            (caller_repo, caller_file),
        ]
        for repo in (changed_repo, caller_repo):
            builder.add_repository_to_graph(repo)
        all_file_data = []
        for repo, path in repo_files:
            parsed = builder.parse_file(repo, path)
            all_file_data.append(parsed)
            builder.add_file_to_graph(
                parsed,
                repo.name,
                {},
                repo_path_str=repo.resolve().as_posix(),
            )
        imports_map = builder.pre_scan_imports([path for _, path in repo_files])
        builder.link_function_calls(all_file_data, imports_map)

        def caller_targets(query_driver):
            with query_driver.session() as session:
                rows = session.run(
                    "MATCH (caller:Function)-[:CALLS|HEURISTIC_CALLS]->(callee:Function) "
                    "WHERE caller.path = $caller_path "
                    "RETURN DISTINCT callee.path AS path",
                    caller_path=caller_file.resolve().as_posix(),
                ).data()
            return {row["path"] for row in rows}

        assert caller_targets(driver) == {changed_helper.resolve().as_posix()}

        handler = RepositoryEventHandler(builder, changed_repo, perform_initial_scan=False)
        changed_helper.write_text("def replacement():\n    return 'changed repository'\n", encoding="utf-8")
        handler.synchronize_with_disk()
        actual = _graph_semantics(driver, caller_repo)
        actual_targets = caller_targets(driver)
        manager.close_driver()
        expected = _clean_graph_semantics(caller_repo, tmp_path / "cross-repository-replacement-clean-db")

        assert actual_targets == {fallback_helper.resolve().as_posix()}, (
            "replacing a callee definition should relink callers in another repository"
        )
        assert actual == expected, (
            "cross-repository callers should remain equivalent to a clean rebuild after a callee file is replaced"
        )
    finally:
        if handler is not None:
            handler.stop()
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_watcher_clears_commit_metadata_after_switch_to_unborn_branch(tmp_path: Path):
    repo = tmp_path / "unborn-repo"
    repo.mkdir()
    (repo / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Watcher Test")
    _git(repo, "config", "user.email", "watcher-test@example.invalid")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "initial")

    manager = KuzuDBManager(str(tmp_path / "unborn-incremental-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), _JM(), loop)
    handler = None
    try:
        handler = RepositoryEventHandler(builder, repo, perform_initial_scan=True)
        assert _graph_semantics(driver, repo)["repository"][0][1]

        _git(repo, "switch", "--orphan", "unborn")
        with pytest.raises(subprocess.CalledProcessError):
            subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL,
                text=True,
            )

        handler.synchronize_with_disk()

        actual = _graph_semantics(driver, repo)
        expected = _clean_graph_semantics(repo, tmp_path / "unborn-clean-db")
        assert actual["repository"][0][1] is None, (
            "an unborn branch must clear the previous commit hash from Repository metadata"
        )
        assert actual == expected, "unborn-branch graph should match a clean rebuild"
    finally:
        if handler is not None:
            handler.stop()
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_watcher_clears_commit_metadata_after_git_metadata_removed(tmp_path: Path):
    repo = tmp_path / "git-removed-repo"
    repo.mkdir()
    (repo / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Watcher Test")
    _git(repo, "config", "user.email", "watcher-test@example.invalid")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "initial")

    manager = KuzuDBManager(str(tmp_path / "git-removed-incremental-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), _JM(), loop)
    handler = None
    try:
        handler = RepositoryEventHandler(builder, repo, perform_initial_scan=True)
        assert _graph_semantics(driver, repo)["repository"][0][1]

        shutil.rmtree(repo / ".git")
        with patch.object(handler, "_require_disk_resync") as resync:
            handler.on_deleted(
                SimpleNamespace(is_directory=True, src_path=str(repo / ".git"))
            )
            resync.assert_called_once_with()

        handler.synchronize_with_disk()

        actual = _graph_semantics(driver, repo)
        expected = _clean_graph_semantics(repo, tmp_path / "git-removed-clean-db")
        assert actual["repository"][0][1] is None, (
            "removing Git metadata must clear the old Repository commit hash"
        )
        assert actual == expected, "the unversioned graph should match a clean rebuild"
    finally:
        if handler is not None:
            handler.stop()
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_watcher_reconciles_after_linked_worktree_gitdir_removed(tmp_path: Path):
    source_repo = tmp_path / "linked-worktree-source"
    source_repo.mkdir()
    (source_repo / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    _git(source_repo, "init", "--initial-branch=main")
    _git(source_repo, "config", "user.name", "Watcher Test")
    _git(source_repo, "config", "user.email", "watcher-test@example.invalid")
    _git(source_repo, "add", "app.py")
    _git(source_repo, "commit", "-m", "initial")

    repo = tmp_path / "linked-worktree"
    _git(source_repo, "worktree", "add", "--detach", str(repo), "HEAD")

    manager = KuzuDBManager(str(tmp_path / "linked-worktree-incremental-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), _JM(), loop)
    watcher = None
    try:
        with patch("codegraphcontext.core.watcher.Observer") as observer_cls:
            observer_cls.return_value.schedule.side_effect = lambda *args, **kwargs: object()
            watcher = CodeWatcher(builder)
            watcher.watch_directory(str(repo), perform_initial_scan=True)
            handler = watcher.handlers[str(repo.resolve())]

        assert _graph_semantics(driver, repo)["repository"][0][1]
        git_admin_dir = handler.git_metadata_dirs[0]
        assert (repo / ".git").is_file()
        assert str(git_admin_dir) in {call.args[1] for call in observer_cls.return_value.schedule.call_args_list}

        shutil.rmtree(git_admin_dir)
        with patch.object(handler, "_require_disk_resync") as resync:
            handler.on_deleted(SimpleNamespace(is_directory=True, src_path=str(git_admin_dir)))
            resync_requested = resync.call_count == 1
        assert handler.git_metadata_dirs == ()
        assert len(watcher.watches[str(repo.resolve())]) == 1

        handler.synchronize_with_disk()
        actual = _graph_semantics(driver, repo)
        expected = _clean_graph_semantics(repo, tmp_path / "linked-worktree-clean-db")
        assert resync_requested and actual["repository"][0][1] is None and actual == expected, (
            "removing a linked worktree's external Git metadata must schedule a "
            "reconciliation and clear the stale commit hash; "
            f"resync_requested={resync_requested}, "
            f"actual_commit={actual['repository'][0][1]!r}, "
            f"expected_commit={expected['repository'][0][1]!r}"
        )
    finally:
        if watcher is not None:
            watcher.unwatch_directory(str(repo))
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_watcher_reconciles_ignore_metadata_changes(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ignore_file = repo / ".cgcignore"
    ignore_file.write_text("excluded.py\n", encoding="utf-8")
    excluded = repo / "excluded.py"
    excluded.write_text("def excluded():\n    return 1\n", encoding="utf-8")
    tracked = repo / "tracked.py"
    tracked.write_text("def tracked():\n    return 2\n", encoding="utf-8")

    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    (other_repo / "other.py").write_text(
        "def unrelated():\n    return 3\n", encoding="utf-8"
    )

    manager = KuzuDBManager(str(tmp_path / "metadata-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), _JM(), loop)
    handler = None
    try:
        handler = RepositoryEventHandler(
            builder,
            repo,
            debounce_interval=0.01,
            perform_initial_scan=True,
        )
        loop.run_until_complete(
            builder.build_graph_from_path_async(other_repo, is_dependency=False)
        )
        other_before = _graph_semantics(driver, other_repo)
        assert "excluded.py" not in {Path(row[0]).name for row in _graph_semantics(driver, repo)["files"]}

        resync_requested = threading.Event()
        request_resync = handler._require_disk_resync

        def request_and_signal_resync():
            request_resync()
            resync_requested.set()

        handler._require_disk_resync = request_and_signal_resync
        ignore_file.write_text("tracked.py\n", encoding="utf-8")
        handler.on_modified(
            SimpleNamespace(is_directory=False, src_path=str(ignore_file))
        )
        assert resync_requested.wait(timeout=2.0), "ignore metadata edit did not request a repository sync"
        assert _wait_until(
            lambda: not handler._resync_required,
            timeout=5.0,
            interval=0.05,
        ), "ignore metadata sync did not complete"

        actual = _graph_semantics(driver, repo)
        assert "excluded.py" in {Path(row[0]).name for row in actual["files"]}
        assert "tracked.py" not in {Path(row[0]).name for row in actual["files"]}
        assert _graph_semantics(driver, other_repo) == other_before

        manager.close_driver()
        expected = _clean_graph_semantics(repo, tmp_path / "metadata-clean-db")
        manager = KuzuDBManager(str(tmp_path / "metadata-db"))
        driver = manager.get_driver()
        assert actual == expected, "metadata-driven incremental graph should match a clean rebuild"
    finally:
        if handler is not None:
            handler.stop()
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_watcher_reconciles_external_explicit_ignore_file_changes(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    excluded = repo / "excluded.py"
    excluded.write_text("def excluded():\n    return 1\n", encoding="utf-8")
    tracked = repo / "tracked.py"
    tracked.write_text("def tracked():\n    return 2\n", encoding="utf-8")

    config_dir = tmp_path / "global-config"
    config_dir.mkdir()
    explicit_ignore = config_dir / ".cgcignore"
    explicit_ignore.write_text("excluded.py\n", encoding="utf-8")

    manager = KuzuDBManager(str(tmp_path / "external-ignore-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    watcher = None
    try:
        builder = GraphBuilder(_DBM(driver), _JM(), loop)
        with patch("codegraphcontext.core.watcher.Observer") as observer_cls:
            watcher = CodeWatcher(builder)
            watcher.watch_directory(
                str(repo),
                perform_initial_scan=True,
                cgcignore_path=str(explicit_ignore),
            )
            handler = watcher.handlers[str(repo.resolve())]

        assert handler._is_ignore_metadata_path(explicit_ignore), (
            "the configured external ignore file must be recognized as watcher metadata"
        )
        assert not handler._is_ignore_metadata_path(config_dir / "other.cgcignore"), (
            "other files in the external configuration directory must not trigger this repository"
        )
        scheduled_paths = {
            call.args[1]
            for call in observer_cls.return_value.schedule.call_args_list
        }
        assert str(config_dir.resolve()) in scheduled_paths, (
            "the explicit ignore file's parent directory must be watched"
        )
        assert "excluded.py" not in {
            Path(row[0]).name for row in _graph_semantics(driver, repo)["files"]
        }

        resync_requested = threading.Event()
        request_resync = handler._require_disk_resync

        def request_and_signal_resync():
            request_resync()
            resync_requested.set()

        handler._require_disk_resync = request_and_signal_resync
        handler.debounce_interval = 0.01
        explicit_ignore.write_text("tracked.py\n", encoding="utf-8")
        handler.on_modified(
            SimpleNamespace(is_directory=False, src_path=str(explicit_ignore))
        )
        assert resync_requested.wait(timeout=2.0), (
            "external ignore metadata edit did not request repository reconciliation"
        )
        assert _wait_until(
            lambda: not handler._resync_required,
            timeout=5.0,
            interval=0.05,
        ), "external ignore metadata sync did not complete"

        actual = _graph_semantics(driver, repo)
        assert "excluded.py" in {Path(row[0]).name for row in actual["files"]}
        assert "tracked.py" not in {Path(row[0]).name for row in actual["files"]}

        manager.close_driver()
        expected = _clean_graph_semantics(
            repo,
            tmp_path / "external-ignore-clean-db",
            cgcignore_path=str(explicit_ignore),
        )
        manager = KuzuDBManager(str(tmp_path / "external-ignore-db"))
        driver = manager.get_driver()
        assert actual == expected, (
            "external-ignore incremental reconciliation should match a clean rebuild"
        )
    finally:
        if watcher is not None:
            watcher.stop()
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_watcher_tracks_retargeted_external_ignore_symlink(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "excluded.py").write_text(
        "def excluded():\n    return 1\n", encoding="utf-8"
    )
    (repo / "tracked.py").write_text(
        "def tracked():\n    return 2\n", encoding="utf-8"
    )

    config_dir = tmp_path / "global-config"
    config_dir.mkdir()
    target_a_dir = tmp_path / "rules-a"
    target_a_dir.mkdir()
    target_a = target_a_dir / "rules.txt"
    target_a.write_text("excluded.py\n", encoding="utf-8")
    target_b_dir = tmp_path / "rules-b"
    target_b_dir.mkdir()
    target_b = target_b_dir / "rules.txt"
    target_b.write_text("tracked.py\n", encoding="utf-8")
    configured_path = config_dir / ".cgcignore"
    try:
        configured_path.symlink_to(target_a)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    manager = KuzuDBManager(str(tmp_path / "symlink-ignore-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    watcher = None
    try:
        builder = GraphBuilder(_DBM(driver), _JM(), loop)
        with patch("codegraphcontext.core.watcher.Observer") as observer_cls:
            watcher = CodeWatcher(builder)
            watcher.watch_directory(
                str(repo),
                perform_initial_scan=True,
                cgcignore_path=str(configured_path),
            )
            handler = watcher.handlers[str(repo.resolve())]

        handler.debounce_interval = 0.01
        resync_requested = threading.Event()
        request_resync = handler._require_disk_resync

        def request_and_signal_resync():
            request_resync()
            resync_requested.set()

        handler._require_disk_resync = request_and_signal_resync

        def send_event(event_path: Path, callback, description: str) -> None:
            resync_requested.clear()
            callback(SimpleNamespace(is_directory=False, src_path=str(event_path)))
            assert resync_requested.wait(timeout=2.0), (
                f"{description} did not request repository reconciliation"
            )
            assert _wait_until(
                lambda: not handler._resync_required,
                timeout=5.0,
                interval=0.05,
            ), f"{description} reconciliation did not complete"

        assert "excluded.py" not in {
            Path(row[0]).name for row in _graph_semantics(driver, repo)["files"]
        }
        configured_path.unlink()
        configured_path.symlink_to(target_b)
        send_event(
            configured_path,
            handler.on_created,
            "retargeted external ignore symlink",
        )
        after_retarget = _graph_semantics(driver, repo)
        assert "excluded.py" in {
            Path(row[0]).name for row in after_retarget["files"]
        }
        assert "tracked.py" not in {
            Path(row[0]).name for row in after_retarget["files"]
        }

        scheduled_paths = {
            call.args[1] for call in observer_cls.return_value.schedule.call_args_list
        }
        assert str(config_dir.resolve()) in scheduled_paths
        assert str(target_a_dir.resolve()) in scheduled_paths
        assert str(target_b_dir.resolve()) in scheduled_paths

        target_b.write_text("excluded.py\n", encoding="utf-8")
        send_event(target_b, handler.on_modified, "new ignore symlink target edit")
        actual = _graph_semantics(driver, repo)
        assert "excluded.py" not in {Path(row[0]).name for row in actual["files"]}
        assert "tracked.py" in {Path(row[0]).name for row in actual["files"]}

        manager.close_driver()
        expected = _clean_graph_semantics(
            repo,
            tmp_path / "symlink-ignore-clean-db",
            cgcignore_path=str(configured_path),
        )
        manager = KuzuDBManager(str(tmp_path / "symlink-ignore-db"))
        driver = manager.get_driver()
        assert actual == expected, (
            "symlink-backed ignore updates should match a clean rebuild"
        )
    finally:
        if watcher is not None:
            watcher.stop()
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_watcher_tracks_repository_local_ignore_symlink(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "excluded.py").write_text(
        "def excluded():\n    return 1\n", encoding="utf-8"
    )
    (repo / "tracked.py").write_text(
        "def tracked():\n    return 2\n", encoding="utf-8"
    )

    target_a_dir = tmp_path / "local-rules-a"
    target_a_dir.mkdir()
    target_a = target_a_dir / "rules.txt"
    target_a.write_text("excluded.py\n", encoding="utf-8")
    target_b_dir = tmp_path / "local-rules-b"
    target_b_dir.mkdir()
    target_b = target_b_dir / "rules.txt"
    target_b.write_text("tracked.py\n", encoding="utf-8")
    configured_path = repo / ".cgcignore"
    try:
        configured_path.symlink_to(target_a)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    manager = KuzuDBManager(str(tmp_path / "local-symlink-ignore-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    watcher = None
    try:
        builder = GraphBuilder(_DBM(driver), _JM(), loop)
        with patch("codegraphcontext.core.watcher.Observer") as observer_cls:
            watcher = CodeWatcher(builder)
            watcher.watch_directory(str(repo), perform_initial_scan=True)
            handler = watcher.handlers[str(repo.resolve())]

        handler.debounce_interval = 0.01
        resync_requested = threading.Event()
        request_resync = handler._require_disk_resync

        def request_and_signal_resync():
            request_resync()
            resync_requested.set()

        handler._require_disk_resync = request_and_signal_resync

        def send_event(event_path: Path, callback, description: str) -> None:
            resync_requested.clear()
            callback(SimpleNamespace(is_directory=False, src_path=str(event_path)))
            assert resync_requested.wait(timeout=2.0), (
                f"{description} did not request repository reconciliation"
            )
            assert _wait_until(
                lambda: not handler._resync_required,
                timeout=5.0,
                interval=0.05,
            ), f"{description} reconciliation did not complete"

        assert "excluded.py" not in {
            Path(row[0]).name for row in _graph_semantics(driver, repo)["files"]
        }
        configured_path.unlink()
        configured_path.symlink_to(target_b)
        send_event(
            configured_path,
            handler.on_created,
            "retargeted repository-local ignore symlink",
        )
        after_retarget = _graph_semantics(driver, repo)
        assert "excluded.py" in {
            Path(row[0]).name for row in after_retarget["files"]
        }
        assert "tracked.py" not in {
            Path(row[0]).name for row in after_retarget["files"]
        }

        scheduled_paths = {
            call.args[1] for call in observer_cls.return_value.schedule.call_args_list
        }
        assert str(target_a_dir.resolve()) in scheduled_paths
        assert str(target_b_dir.resolve()) in scheduled_paths

        target_b.write_text("excluded.py\n", encoding="utf-8")
        send_event(target_b, handler.on_modified, "local symlink target edit")
        actual = _graph_semantics(driver, repo)
        assert "excluded.py" not in {Path(row[0]).name for row in actual["files"]}
        assert "tracked.py" in {Path(row[0]).name for row in actual["files"]}

        manager.close_driver()
        expected = _clean_graph_semantics(
            repo,
            tmp_path / "local-symlink-ignore-clean-db",
        )
        manager = KuzuDBManager(str(tmp_path / "local-symlink-ignore-db"))
        driver = manager.get_driver()
        assert actual == expected, (
            "local symlink-backed ignore updates should match a clean rebuild"
        )
    finally:
        if watcher is not None:
            watcher.stop()
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_repository_owner_lookup_uses_ancestor_for_symlinked_file(tmp_path: Path):
    target_repo = tmp_path / "z"
    host_repo = tmp_path / f"long-symlink-host-{'x' * 100}"
    target_repo.mkdir()
    host_repo.mkdir()
    target_file = target_repo / "module.py"
    target_file.write_text("def module():\n    return 1\n", encoding="utf-8")
    alias_dir = host_repo / "alias"
    alias_dir.symlink_to(target_repo, target_is_directory=True)
    alias_file = alias_dir / target_file.name

    manager = KuzuDBManager(str(tmp_path / "overlapping-repository-owner-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), _JM(), loop)
    try:
        for repo in (host_repo, target_repo):
            builder.add_repository_to_graph(repo)
        for repo, path in ((host_repo, alias_file), (target_repo, target_file)):
            parsed = builder.parse_file(repo, path)
            builder.add_file_to_graph(
                parsed,
                repo.name,
                {},
                repo_path_str=repo.resolve().as_posix(),
            )

        host_path = host_repo.resolve().as_posix()
        target_path = target_repo.resolve().as_posix()
        assert len(host_path) > len(target_path)
        assert builder.get_repository_path_for_file(target_file.resolve().as_posix()) == target_path
    finally:
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_watcher_refreshes_commit_metadata_for_same_tree_branch_switch(tmp_path: Path):
    source_repo = tmp_path / "same-tree-source"
    source_repo.mkdir()
    (source_repo / "app.py").write_text(
        "def stable():\n    return 1\n", encoding="utf-8"
    )
    _git(source_repo, "init", "--initial-branch=main")
    _git(source_repo, "config", "user.name", "Same Tree Test")
    _git(source_repo, "config", "user.email", "same-tree-test@example.invalid")
    _git(source_repo, "add", "app.py")
    _git(source_repo, "commit", "-m", "initial")
    original_commit = subprocess.check_output(
        ["git", "-C", str(source_repo), "rev-parse", "HEAD"], text=True
    ).strip()
    original_tree = subprocess.check_output(
        ["git", "-C", str(source_repo), "rev-parse", "HEAD^{tree}"], text=True
    ).strip()
    _git(source_repo, "switch", "-c", "same-tree")
    _git(source_repo, "commit", "--allow-empty", "-m", "same tree, new commit")
    expected_commit = subprocess.check_output(
        ["git", "-C", str(source_repo), "rev-parse", "HEAD"], text=True
    ).strip()
    expected_tree = subprocess.check_output(
        ["git", "-C", str(source_repo), "rev-parse", "HEAD^{tree}"], text=True
    ).strip()
    _git(source_repo, "switch", "main")

    repo = tmp_path / "same-tree-worktree"
    _git(source_repo, "worktree", "add", "--detach", str(repo), "main")

    manager = KuzuDBManager(str(tmp_path / "same-tree-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    watcher = None
    try:
        builder = GraphBuilder(_DBM(driver), _JM(), loop)
        with patch("codegraphcontext.core.watcher.Observer") as observer_cls:
            watcher = CodeWatcher(builder)
            watcher.watch_directory(str(repo), perform_initial_scan=True)
            handler = watcher.handlers[str(repo.resolve())]
        scheduled_paths = {
            call.args[1]
            for call in observer_cls.return_value.schedule.call_args_list
        }
        assert str(handler.git_metadata_dirs[0]) in scheduled_paths
        assert str(handler.git_metadata_dirs[-1] / "refs") in scheduled_paths

        original = _graph_semantics(driver, repo)
        current_tree = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], text=True
        ).strip()
        _git(repo, "switch", "same-tree")
        assert current_tree == original_tree == expected_tree
        assert original["repository"][0][1] == original_commit
        assert original_commit != expected_commit

        handler.debounce_interval = 0.01
        resync_requested = threading.Event()
        request_resync = handler._require_disk_resync

        def request_and_signal_resync():
            request_resync()
            resync_requested.set()

        handler._require_disk_resync = request_and_signal_resync
        handler.on_modified(
            SimpleNamespace(
                is_directory=False,
                src_path=str(handler.git_metadata_dirs[0] / "HEAD"),
            )
        )
        assert resync_requested.wait(timeout=2.0), (
            "Git HEAD changes must request repository reconciliation"
        )
        assert _wait_until(
            lambda: not handler._resync_required,
            timeout=5.0,
            interval=0.05,
        ), "Git metadata reconciliation did not complete"

        actual = _graph_semantics(driver, repo)
        assert actual["repository"][0][1] == expected_commit
        manager.close_driver()
        expected = _clean_graph_semantics(repo, tmp_path / "same-tree-clean-db")
        manager = KuzuDBManager(str(tmp_path / "same-tree-db"))
        driver = manager.get_driver()
        assert actual == expected, "same-tree branch switch should match a clean rebuild"
    finally:
        if watcher is not None:
            watcher.unwatch_directory(str(repo))
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_watcher_registers_refs_created_after_pack_refs(tmp_path: Path):
    source_repo = tmp_path / "packed-refs-source"
    source_repo.mkdir()
    (source_repo / "app.py").write_text("def stable():\n    return 1\n", encoding="utf-8")
    _git(source_repo, "init", "--initial-branch=main")
    _git(source_repo, "config", "user.name", "Packed Refs Test")
    _git(source_repo, "config", "user.email", "packed-refs-test@example.invalid")
    _git(source_repo, "add", "app.py")
    _git(source_repo, "commit", "-m", "initial")
    _git(source_repo, "branch", "watched")
    _git(source_repo, "pack-refs", "--all", "--prune")
    repo = tmp_path / "packed-refs-worktree"
    _git(source_repo, "worktree", "add", str(repo), "watched")
    initial_commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    refs_dir = source_repo / ".git" / "refs"

    manager = KuzuDBManager(str(tmp_path / "packed-refs-db"))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    watcher = None
    try:
        builder = GraphBuilder(_DBM(driver), _JM(), loop)
        with patch("codegraphcontext.core.watcher.Observer") as observer_cls:
            watcher = CodeWatcher(builder)
            watcher.watch_directory(str(repo), perform_initial_scan=True)
            handler = watcher.handlers[str(repo.resolve())]

        refs_dir = handler.git_metadata_dirs[-1] / "refs"
        assert refs_dir.is_dir()
        scheduled_before = {
            (call.args[1], call.kwargs.get("recursive", False))
            for call in observer_cls.return_value.schedule.call_args_list
        }
        assert (str(handler.git_metadata_dirs[-1]), False) in scheduled_before
        assert (str(refs_dir), True) in scheduled_before
        assert _graph_semantics(driver, repo)["repository"][0][1] == initial_commit

        # Pruning all loose refs can remove the now-empty refs tree. The old
        # registration then points at a deleted directory and cannot observe
        # its later recreation.
        shutil.rmtree(refs_dir)
        with patch.object(handler, "_require_disk_resync") as request_resync:
            handler.on_deleted(SimpleNamespace(is_directory=True, src_path=str(refs_dir)))
            assert not refs_dir.exists()
            refs_dir.mkdir()
            handler.on_created(SimpleNamespace(is_directory=True, src_path=str(refs_dir)))
            request_resync.assert_not_called()

        scheduled_after = {
            (call.args[1], call.kwargs.get("recursive", False))
            for call in observer_cls.return_value.schedule.call_args_list
        }
        refs_watch_calls = [
            call
            for call in observer_cls.return_value.schedule.call_args_list
            if call.args[1] == str(refs_dir) and call.kwargs.get("recursive", False)
        ]
        assert (str(refs_dir), True) in scheduled_after and len(refs_watch_calls) == 2, (
            "recreating .git/refs after pruning must register a fresh recursive ref watch"
        )

        _git(repo, "commit", "--allow-empty", "-m", "recreate loose ref")
        expected_commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        assert expected_commit != initial_commit
        handler.debounce_interval = 0.01
        resync_requested = threading.Event()
        request_resync = handler._require_disk_resync

        def request_and_signal_resync():
            request_resync()
            resync_requested.set()

        handler._require_disk_resync = request_and_signal_resync
        handler.on_modified(
            SimpleNamespace(
                is_directory=False,
                src_path=str(refs_dir / "heads" / "main"),
            )
        )
        assert resync_requested.wait(timeout=2.0), "the new loose branch ref must request repository reconciliation"
        assert _wait_until(
            lambda: not handler._resync_required,
            timeout=5.0,
            interval=0.05,
        ), "Git ref reconciliation did not complete"

        actual = _graph_semantics(driver, repo)
        assert actual["repository"][0][1] == expected_commit
        manager.close_driver()
        expected = _clean_graph_semantics(repo, tmp_path / "packed-refs-clean-db")
        manager = KuzuDBManager(str(tmp_path / "packed-refs-db"))
        driver = manager.get_driver()
        assert actual == expected, "ref recreation should match a clean rebuild"
    finally:
        if watcher is not None:
            watcher.unwatch_directory(str(repo))
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_external_cli_refresh_matches_clean_rebuild(tmp_path: Path):
    repo = tmp_path / "refresh-repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text(
        "def before_refresh():\n    return 1\n", encoding="utf-8"
    )
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Refresh Test")
    _git(repo, "config", "user.email", "refresh-test@example.invalid")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "initial")

    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    (other_repo / "other.py").write_text(
        "def unaffected():\n    return 2\n", encoding="utf-8"
    )

    db_path = tmp_path / "refresh-db"
    manager = KuzuDBManager(str(db_path))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    builder = GraphBuilder(_DBM(driver), JobManager(), loop)
    context = SimpleNamespace(database=None, db_path=None, cgcignore_path=None)
    code_finder = MagicMock()
    code_finder.list_indexed_repositories.return_value = [{"path": str(repo)}]
    source_setting = {"value": "true"}
    original_get_config_value = graph_builder_module.get_config_value

    def config_value(key):
        if key == "INDEX_SOURCE":
            return source_setting["value"]
        return original_get_config_value(key)

    try:
        with patch.object(graph_builder_module, "get_config_value", side_effect=config_value):
            loop.run_until_complete(builder.build_graph_from_path_async(repo, is_dependency=False))
            loop.run_until_complete(builder.build_graph_from_path_async(other_repo, is_dependency=False))
            other_before = _graph_semantics(driver, other_repo)

            source.write_text(
                "def after_refresh():\n    return 3\n", encoding="utf-8"
            )
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "external source update")
            source_setting["value"] = "false"
            with (
                patch.object(cli_helpers, "ensure_first_run_bootstrap"),
                patch.object(cli_helpers, "resolve_context", return_value=context),
                patch.object(
                    cli_helpers,
                    "_initialize_services",
                    return_value=(manager, builder, code_finder, context),
                ),
            ):
                cli_helpers.reindex_helper(str(repo), no_progress=True)

            manager = KuzuDBManager(str(db_path))
            driver = manager.get_driver()
            actual = _graph_semantics(driver, repo)
            other_after = _graph_semantics(driver, other_repo)
            manager.close_driver()

            expected = _clean_graph_semantics(repo, tmp_path / "refresh-clean-db")
            assert actual == expected, (
                "external CLI refresh should remove stale symbols, source properties and commit metadata"
            )
            assert all(row[3] is None for row in actual["functions"]), (
                "the INDEX_SOURCE configuration change should remove source properties"
            )
            assert other_after == other_before, "external CLI refresh must preserve unrelated repositories"
    finally:
        manager.close_driver()
        loop.close()


@pytest.mark.integration
def test_parser_version_change_with_rebuild_matches_clean_graph(tmp_path: Path, monkeypatch):
    repo = tmp_path / "parser-version-repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("pass\n", encoding="utf-8")

    def parser_version(symbol_name: str):
        class VersionedParser:
            def __init__(self, language_name: str):
                self.language_name = language_name

            def parse(self, path: Path, is_dependency=False, **kwargs):
                function = {
                    "name": symbol_name,
                    "line_number": 1,
                    "end_line": 1,
                    "args": [],
                }
                if kwargs.get("index_source"):
                    function["source"] = path.read_text(encoding="utf-8")
                return {
                    "path": str(path),
                    "lang": self.language_name,
                    "functions": [function],
                    "classes": [],
                    "imports": [],
                    "function_calls": [],
                }

        return VersionedParser

    incremental_path = tmp_path / "parser-version-db"
    manager = KuzuDBManager(str(incremental_path))
    driver = manager.get_driver()
    loop = asyncio.new_event_loop()
    try:
        monkeypatch.setattr(
            graph_builder_module, "TreeSitterParser", parser_version("parser_v1_symbol")
        )
        initial_builder = GraphBuilder(_DBM(driver), _JM(), loop)
        initial_builder.add_repository_to_graph(repo)
        initial_builder.update_file_in_graph(source, repo, {})
        assert "parser_v1_symbol" in {row[1] for row in _graph_semantics(driver, repo)["functions"]}

        # A parser/grammar package update takes effect in a fresh indexing
        # process. Model that lifecycle with a new builder and parser cache.
        monkeypatch.setattr(
            graph_builder_module, "TreeSitterParser", parser_version("parser_v2_symbol")
        )
        updated_builder = GraphBuilder(_DBM(driver), _JM(), loop)
        assert updated_builder.delete_repository_from_graph(str(repo))
        updated_builder.add_repository_to_graph(repo)
        updated_builder.update_file_in_graph(source, repo, {})
        actual = _graph_semantics(driver, repo)
        assert "parser_v2_symbol" in {row[1] for row in actual["functions"]}
        assert "parser_v1_symbol" not in {row[1] for row in actual["functions"]}

        manager.close_driver()
        clean_manager = KuzuDBManager(str(tmp_path / "parser-version-clean-db"))
        clean_driver = clean_manager.get_driver()
        clean_builder = GraphBuilder(_DBM(clean_driver), _JM(), loop)
        clean_builder.add_repository_to_graph(repo)
        clean_builder.update_file_in_graph(source, repo, {})
        expected = _graph_semantics(clean_driver, repo)
        clean_manager.close_driver()

        manager = KuzuDBManager(str(incremental_path))
        driver = manager.get_driver()
        assert actual == expected, "updated parser output should match a clean graph rebuild"
    finally:
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
