from __future__ import annotations

import subprocess
import threading
import multiprocessing
import time
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from codegraphcontext.utils import branch_identity
from codegraphcontext.utils.branch_identity import repository_identity, branch_database_path
from codegraphcontext.core.watcher import RepositoryEventHandler
from codegraphcontext.core import watcher as watcher_module
from codegraphcontext.cli import config_manager


def _git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=path, text=True).strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "a.py").write_text("x = 1\n")
    _git(path, "add", "a.py")
    _git(path, "commit", "-m", "initial")
    _git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return path


def _resolve_default_branch_process(config, repo, identity, start, results):
    original_replace = branch_identity.os.replace

    def delayed_replace(source, target):
        time.sleep(0.2)
        return original_replace(source, target)

    branch_identity.os.replace = delayed_replace
    try:
        start.wait(timeout=5)
        result = branch_database_path(config, "ladybugdb", repo, identity)
        results.put((identity.branch, str(result), None))
    except BaseException as exc:
        results.put((identity.branch, "", repr(exc)))


def test_branch_indexes_use_distinct_stable_paths_and_main_keeps_legacy_path(tmp_path):
    repo = _repo(tmp_path / "repo")
    config = repo / ".codegraphcontext"
    config.mkdir()
    main_identity = repository_identity(repo)
    main_db = branch_database_path(config, "ladybugdb", repo, main_identity)

    _git(repo, "switch", "-c", "feature")
    feature_identity = repository_identity(repo)
    feature_db = branch_database_path(config, "ladybugdb", repo, feature_identity)

    assert main_db == config / "db" / "ladybugdb"
    assert feature_db != main_db
    assert feature_identity.storage_key != main_identity.storage_key
    assert branch_database_path(config, "ladybugdb", repo, feature_identity) == feature_db


def test_linked_worktree_identity_is_distinct_and_path_safe(tmp_path):
    repo = _repo(tmp_path / "repo")
    linked = tmp_path / "linked checkout with private path"
    _git(repo, "worktree", "add", "-b", "feature", str(linked))

    identity = repository_identity(linked)
    assert identity.branch == "feature"
    assert identity.repository_id == repository_identity(repo).repository_id
    assert identity.worktree_id != repository_identity(repo).worktree_id
    assert str(tmp_path) not in identity.as_dict().__repr__()


def test_worktree_from_bare_repository_does_not_claim_root_checkout(tmp_path):
    source = _repo(tmp_path / "source")
    bare = tmp_path / "repo.git"
    subprocess.check_call(["git", "clone", "--bare", str(source), str(bare)])
    linked = tmp_path / "bare-backed worktree"
    subprocess.check_call(
        ["git", f"--git-dir={bare}", "worktree", "add", str(linked), "main"],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
    )
    mapped_context = tmp_path / "saved-context"

    identity = repository_identity(linked)
    database = branch_database_path(mapped_context, "ladybugdb", linked, identity)

    assert identity.branch == "main"
    assert identity.repository_root is None
    assert database == mapped_context / "db" / "branches" / identity.storage_key / "ladybugdb"


def test_non_git_and_detached_head_are_explicit_and_non_colliding(tmp_path):
    repo = _repo(tmp_path / "repo")
    detached = tmp_path / "detached"
    _git(repo, "worktree", "add", "--detach", str(detached), "HEAD")
    detached_identity = repository_identity(detached)
    plain = repository_identity(tmp_path)
    plain_file = tmp_path / "module.py"
    plain_file.write_text("pass\n", encoding="utf-8")
    plain_from_file = repository_identity(plain_file)

    assert detached_identity.branch.startswith("detached:")
    assert detached_identity.commit
    assert plain.kind == "non-git"
    assert plain_from_file.storage_key == plain.storage_key
    assert plain_from_file.repository_id == plain.repository_id
    assert plain.branch is None
    assert plain.worktree_root == tmp_path.resolve()
    assert detached_identity.storage_key != plain.storage_key


def test_custom_root_default_branch_without_remote_head_keeps_legacy_then_isolates(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "develop")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "initial")
    config = repo / ".codegraphcontext"

    default_db = branch_database_path(config, "ladybugdb", repo)
    _git(repo, "switch", "-c", "feature")
    feature_db = branch_database_path(config, "ladybugdb", repo)

    assert default_db == config / "db" / "ladybugdb"
    assert feature_db != default_db


def test_concurrent_default_branch_resolution_does_not_race_marker_tempfile(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "develop")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "initial")
    config = tmp_path / "mapped-context"
    identity = repository_identity(repo)
    original_replace = branch_identity.os.replace
    first_replace_entered = threading.Event()

    def synchronized_replace(source, target):
        if not first_replace_entered.is_set():
            first_replace_entered.set()
            threading.Event().wait(timeout=0.1)
        return original_replace(source, target)

    monkeypatch.setattr(branch_identity.os, "replace", synchronized_replace)

    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(
            lambda _index: branch_database_path(config, "ladybugdb", repo, identity),
            range(2),
        ))

    assert paths == [config / "db" / "ladybugdb"] * 2


def test_concurrent_processes_cannot_replace_default_branch_marker(tmp_path):
    repo = _repo(tmp_path / "repo")
    config = tmp_path / "mapped-context"
    base_identity = repository_identity(repo)
    develop = replace(
        base_identity, branch="develop", is_default_branch=False, default_branch=None
    )
    feature = replace(develop, branch="feature")
    context = multiprocessing.get_context("fork")
    start = context.Barrier(2)
    results = context.Queue()
    workers = [
        context.Process(
            target=_resolve_default_branch_process,
            args=(config, repo, identity, start, results),
        )
        for identity in (develop, feature)
    ]
    for worker in workers:
        worker.start()
    reports = [results.get(timeout=8) for _ in workers]
    for worker in workers:
        worker.join(timeout=8)

    assert all(not worker.is_alive() and worker.exitcode == 0 for worker in workers)
    assert all(error is None for _branch, _path, error in reports)
    assert sum(path == str(config / "db" / "ladybugdb") for _branch, path, _error in reports) == 1, reports


def test_detached_root_checkout_without_remote_head_uses_hashed_db(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "develop")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "switch", "--detach", "HEAD")
    config = repo / ".codegraphcontext"

    identity = repository_identity(repo)
    detached_db = branch_database_path(config, "ladybugdb", repo, identity)

    assert identity.branch.startswith("detached:")
    assert detached_db == config / "db" / "branches" / identity.storage_key / "ladybugdb"
    assert not (config / "branch-default.json").exists()


def test_watcher_stops_routing_events_after_branch_identity_changes(tmp_path):
    repo = _repo(tmp_path / "repo")

    class Builder:
        parsers = {".py": object()}
        generic_extensions = ()
        generic_filenames = ()

        def update_file_in_graph(self, *_args, **_kwargs):
            raise AssertionError("stale watcher must not write to its old index")

    handler = RepositoryEventHandler(Builder(), repo, perform_initial_scan=False)
    _git(repo, "switch", "-c", "feature")
    handler._handle_modification(str(repo / "a.py"))

    assert handler._identity_mismatch_reported
    assert handler._identity_matches_bound_index() is False


def test_watcher_rejects_a_different_repository_on_the_same_branch(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    other = _repo(tmp_path / "other")
    handler = RepositoryEventHandler(object(), repo, perform_initial_scan=False)
    other_identity = repository_identity(other)
    monkeypatch.setattr(watcher_module, "repository_identity", lambda _path: other_identity)

    assert other_identity.branch == repository_identity(repo).branch == "main"
    assert handler._identity_matches_bound_index() is False


def test_watcher_rejects_stale_identity_selected_before_branch_switch(tmp_path):
    repo = _repo(tmp_path / "repo")
    selected_identity = repository_identity(repo).as_dict()
    _git(repo, "switch", "-c", "feature")

    import pytest

    with pytest.raises(RuntimeError, match="identity changed while resolving"):
        RepositoryEventHandler(
            object(), repo, perform_initial_scan=False,
            generation_db_path=tmp_path / "old-branch-db",
            generation_identity=selected_identity,
        )


def test_watcher_refuses_generation_update_after_branch_switch(tmp_path):
    repo = _repo(tmp_path / "repo")
    identity = repository_identity(repo).as_dict()
    handler = RepositoryEventHandler(
        object(), repo, perform_initial_scan=False,
        generation_db_path=tmp_path / "main-db",
        generation_identity=identity,
    )
    _git(repo, "switch", "-c", "feature")

    import pytest

    with pytest.raises(RuntimeError, match="changed before the graph update"):
        with handler._generation_update():
            pass


def test_watcher_rechecks_identity_after_waiting_for_update_lock(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")

    class Builder:
        parsers = {".py": object()}
        generic_extensions = ()
        generic_filenames = ()

        def update_file_in_graph(self, *_args, **_kwargs):
            raise AssertionError("identity changed while this event waited for the lock")

    handler = RepositoryEventHandler(Builder(), repo, perform_initial_scan=False)
    checks = iter((True, False))
    monkeypatch.setattr(handler, "_identity_matches_bound_index", lambda: next(checks))

    handler._handle_modification(str(repo / "a.py"))


def test_default_per_repo_context_resolves_to_branch_scoped_storage(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    monkeypatch.setattr(
        config_manager, "load_context_config",
        lambda **_kwargs: config_manager.ContextConfig(mode="per-repo"),
    )
    monkeypatch.setattr(config_manager, "find_local_cgc_dir", lambda _cwd: None)
    monkeypatch.setattr(config_manager, "get_workspace_mapping", lambda _cwd: None)
    monkeypatch.setattr(config_manager, "load_config", lambda **_kwargs: {"DEFAULT_DATABASE": "ladybugdb"})
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)

    main = config_manager.resolve_context(cwd=repo, create_local_context=False)
    _git(repo, "switch", "-c", "feature")
    feature = config_manager.resolve_context(cwd=repo, create_local_context=False)

    assert Path(main.db_path) == repo / ".codegraphcontext" / "db" / "ladybugdb"
    assert Path(feature.db_path) != Path(main.db_path)
    assert Path(feature.db_path).parent.name == repository_identity(repo).storage_key
    assert main.repository_identity["storage_key"] != feature.repository_identity["storage_key"]

    from codegraphcontext.cli.snapshot_commands import effective_snapshot_identity

    selected_identity, _root = effective_snapshot_identity(main, repo)
    assert selected_identity == main.repository_identity
    import pytest

    with pytest.raises(RuntimeError, match="identity changed while resolving"):
        RepositoryEventHandler(
            object(), repo, perform_initial_scan=False,
            generation_db_path=main.db_path,
            generation_identity=selected_identity,
        )


def test_saved_workspace_mapping_resolves_branch_scoped_storage(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    mapped_context = tmp_path / "mapped-context"
    mapped_context.mkdir()
    monkeypatch.setattr(
        config_manager, "load_context_config",
        lambda **_kwargs: config_manager.ContextConfig(mode="per-repo"),
    )
    monkeypatch.setattr(config_manager, "find_local_cgc_dir", lambda _cwd: None)
    monkeypatch.setattr(
        config_manager, "get_workspace_mapping",
        lambda _cwd: {"context_path": str(mapped_context), "database": "ladybugdb"},
    )

    main = config_manager.resolve_context(cwd=repo, create_local_context=False)
    _git(repo, "switch", "-c", "feature")
    feature = config_manager.resolve_context(cwd=repo, create_local_context=False)

    assert Path(main.db_path) == mapped_context / "db" / "ladybugdb"
    assert Path(feature.db_path) != Path(main.db_path)
    assert Path(feature.db_path).parent.name == repository_identity(repo).storage_key
    assert Path(feature.cgcignore_path) == mapped_context / ".cgcignore"


def test_explicit_named_context_db_path_is_unchanged_on_feature_branch(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    configured = tmp_path / "shared named database"
    cfg = config_manager.ContextConfig(
        mode="named",
        default_context="shared",
        contexts={
            "shared": config_manager.ContextInfo(
                name="shared", database="ladybugdb", db_path=str(configured)
            )
        },
    )
    monkeypatch.setattr(config_manager, "load_context_config", lambda **_kwargs: cfg)
    _git(repo, "switch", "-c", "feature")

    resolved = config_manager.resolve_context("shared", cwd=repo, read_only=True)

    assert Path(resolved.db_path) == configured
