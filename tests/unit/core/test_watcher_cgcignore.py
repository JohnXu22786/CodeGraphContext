import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codegraphcontext.core import watcher as watcher_module
from codegraphcontext.core.watcher import CodeWatcher, RepositoryEventHandler


def _graph_builder():
    graph_builder = MagicMock()
    graph_builder.parsers = {".py": "python"}
    graph_builder.get_importer_file_paths.return_value = set()
    graph_builder.generic_extensions = {".md"}
    graph_builder.generic_filenames = {"Dockerfile"}
    graph_builder.pre_scan_imports.return_value = {}
    graph_builder.parse_file.return_value = {"path": "parsed.py"}
    graph_builder.link_function_calls.return_value = None
    graph_builder.link_inheritance.return_value = None
    graph_builder.get_caller_file_paths.return_value = set()
    graph_builder.get_inheritance_neighbor_paths.return_value = set()
    graph_builder.get_repo_class_lookup.return_value = {}
    graph_builder.update_file_in_graph.return_value = None
    return graph_builder


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_watcher_skips_ignored_modified_event(tmp_path: Path):
    repo = tmp_path / "repo"
    ignored_dir = repo / "bin"
    src_dir = repo / "src"
    ignored_dir.mkdir(parents=True)
    src_dir.mkdir()
    (repo / ".cgcignore").write_text("**/bin/\n", encoding="utf-8")
    ignored_file = ignored_dir / "generated.py"
    tracked_file = src_dir / "app.py"
    ignored_file.write_text("print('ignored')\n", encoding="utf-8")
    tracked_file.write_text("print('tracked')\n", encoding="utf-8")

    handler = RepositoryEventHandler(_graph_builder(), repo, perform_initial_scan=False)
    generic_file = repo / "README.md"
    generic_file.write_text("# tracked generic file\n", encoding="utf-8")

    with patch.object(handler, "_debounce") as debounce:
        handler.on_modified(SimpleNamespace(is_directory=False, src_path=str(ignored_file)))
        debounce.assert_not_called()

        handler.on_modified(SimpleNamespace(is_directory=False, src_path=str(tracked_file)))
        debounce.assert_called_once()

        handler.on_created(SimpleNamespace(is_directory=False, src_path=str(generic_file)))
        assert debounce.call_count == 2, "creating an indexed generic file should queue an update"

        handler.on_modified(SimpleNamespace(is_directory=False, src_path=str(generic_file)))
        assert debounce.call_count == 3, "editing an indexed generic file should queue an update"


def test_watcher_rejects_supported_file_symlink_events_outside_repository(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.py"
    secret.write_text("pass\n", encoding="utf-8")
    alias = repo / "external.py"
    try:
        alias.symlink_to(secret)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")

    handler = RepositoryEventHandler(_graph_builder(), repo, perform_initial_scan=False)
    with patch.object(handler, "_debounce") as debounce:
        handler.on_created(SimpleNamespace(is_directory=False, src_path=str(alias)))
        handler.on_modified(SimpleNamespace(is_directory=False, src_path=str(alias)))

    debounce.assert_not_called()


def test_watcher_filters_large_ignored_deletion_batch_before_queueing(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / ".cgcignore").write_text("**/bin/\n", encoding="utf-8")
    graph_builder = _graph_builder()
    graph_builder.generic_extensions = {".md"}
    graph_builder.generic_filenames = {"Dockerfile"}
    handler = RepositoryEventHandler(graph_builder, repo, perform_initial_scan=False)

    # Deleted paths no longer exist, so filtering must use the path and ignore
    # rules before adding any work, not rely on is_file().
    with patch.object(handler, "_debounce") as debounce:
        for index in range(1000):
            handler.on_deleted(
                SimpleNamespace(
                    is_directory=False,
                    src_path=str(repo / "bin" / f"generated_{index}.py"),
                )
            )

        debounce.assert_not_called()
        handler.on_deleted(SimpleNamespace(is_directory=False, src_path=str(repo / "README.md")))
        assert debounce.call_count == 1, "deleting a graph-indexed generic file should remain supported"


def test_watcher_resyncs_deleted_repository_directory_but_not_external_directory(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    deleted_dir = repo / "src"
    deleted_dir.mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    handler = RepositoryEventHandler(_graph_builder(), repo, perform_initial_scan=False)

    deleted_dir.rmdir()
    with patch.object(handler, "_require_disk_resync") as resync:
        handler.on_deleted(SimpleNamespace(is_directory=True, src_path=str(deleted_dir)))
        resync.assert_called_once_with()

        handler.on_deleted(SimpleNamespace(is_directory=True, src_path=str(outside_dir)))
        resync.assert_called_once_with()


def test_watcher_ignores_external_directory_moves_but_resyncs_repository_moves(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    config_dir = tmp_path / "global-config"
    config_dir.mkdir()
    explicit_ignore = config_dir / ".cgcignore"
    explicit_ignore.write_text("ignored.py\n", encoding="utf-8")
    handler = RepositoryEventHandler(
        _graph_builder(),
        repo,
        perform_initial_scan=False,
        cgcignore_path=str(explicit_ignore),
    )

    external_src = config_dir / "unrelated-before"
    external_src.mkdir()
    external_dest = config_dir / "unrelated-after"
    external_src.rename(external_dest)

    with patch.object(handler, "_require_disk_resync") as resync:
        handler.on_moved(
            SimpleNamespace(
                is_directory=True,
                src_path=str(external_src),
                dest_path=str(external_dest),
            )
        )
        resync.assert_not_called()

        repository_src = repo / "src-before"
        repository_dest = repo / "src-after"
        handler.on_moved(
            SimpleNamespace(
                is_directory=True,
                src_path=str(repository_src),
                dest_path=str(repository_dest),
            )
        )
        resync.assert_called_once_with()


@pytest.mark.parametrize("event_kind", ["deleted", "moved"])
def test_watcher_reconciles_external_ignore_target_parent_events(tmp_path: Path, event_kind: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    config_dir = tmp_path / "global-config"
    config_dir.mkdir()
    target_dir = tmp_path / "external-rules"
    target_dir.mkdir()
    target_file = target_dir / ".cgcignore"
    target_file.write_text("ignored.py\n", encoding="utf-8")
    configured_ignore = config_dir / ".cgcignore"
    try:
        configured_ignore.symlink_to(target_file)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")

    handler = RepositoryEventHandler(
        _graph_builder(),
        repo,
        perform_initial_scan=False,
        cgcignore_path=str(configured_ignore),
    )
    assert handler.explicit_ignore_path == target_file.resolve()

    if event_kind == "deleted":
        target_file.unlink()
        target_dir.rmdir()
        event = SimpleNamespace(is_directory=True, src_path=str(target_dir))
        dispatch = handler.on_deleted
    else:
        moved_target = tmp_path / "external-rules-moved"
        target_dir.rename(moved_target)
        event = SimpleNamespace(
            is_directory=True,
            src_path=str(target_dir),
            dest_path=str(moved_target),
        )
        dispatch = handler.on_moved

    with patch.object(handler, "_require_disk_resync") as resync:
        dispatch(event)
        resync.assert_called_once_with()


def test_watcher_restores_external_ignore_target_watch_after_parent_recreation(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    config_dir = tmp_path / "global-config"
    config_dir.mkdir()
    target_dir = tmp_path / "external-rules"
    target_dir.mkdir()
    target_file = target_dir / ".cgcignore"
    target_file.write_text("ignored.py\n", encoding="utf-8")
    configured_ignore = config_dir / ".cgcignore"
    try:
        configured_ignore.symlink_to(target_file)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")

    observer = _SharedObserver()
    monkeypatch.setattr(watcher_module, "Observer", lambda: observer)
    graph_builder = _graph_builder()
    graph_builder.get_repo_file_paths.return_value = []
    graph_builder.update_file_in_graph.return_value = None
    watcher = CodeWatcher(graph_builder, use_polling=False)
    watcher.watch_directory(str(repo), perform_initial_scan=False, cgcignore_path=str(configured_ignore))
    handler = watcher.handlers[str(repo.resolve())]

    target_file.unlink()
    target_dir.rmdir()
    with patch.object(handler, "_require_disk_resync") as resync:
        handler.on_deleted(SimpleNamespace(is_directory=True, src_path=str(target_dir)))
        resync.assert_called_once_with()

    # Disk reconciliation drops the direct watch when its target parent is gone.
    handler.synchronize_with_disk()
    fallback_watch = observer.watches.get((str(tmp_path.resolve()), False))
    assert fallback_watch is not None and observer.handlers[fallback_watch] == {handler}, (
        "the nearest existing ancestor must remain watched while the configured target parent is absent"
    )

    unrelated_dir = tmp_path / "unrelated-external"
    unrelated_dir.mkdir()
    with patch.object(handler, "_debounce") as debounce:
        handler.on_created(SimpleNamespace(is_directory=True, src_path=str(unrelated_dir)))
        debounce.assert_not_called()

    target_dir.mkdir()
    target_file.write_text("tracked.py\n", encoding="utf-8")
    with patch.object(handler, "_debounce") as debounce:
        handler.on_created(SimpleNamespace(is_directory=True, src_path=str(target_dir)))
        debounce.assert_called_once_with(str(target_dir), handler._require_disk_resync, coalesce_key=None)

    target_watch = observer.watches.get((str(target_dir.resolve()), False))
    assert target_watch is not None and observer.handlers[target_watch] == {handler}, (
        "recreating the target parent must restore its direct ignore watch"
    )
    watcher.unwatch_directory(str(repo))


def test_watcher_ignores_shared_inactive_branch_ref_updates(tmp_path: Path):
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    _git(source_repo, "init", "--initial-branch=main")
    _git(source_repo, "config", "user.name", "Watcher Test")
    _git(source_repo, "config", "user.email", "watcher-test@example.invalid")
    _git(source_repo, "commit", "--allow-empty", "-m", "initial")
    initial_commit = subprocess.check_output(["git", "-C", str(source_repo), "rev-parse", "HEAD"], text=True).strip()
    other_commit = subprocess.check_output(
        [
            "git",
            "-C",
            str(source_repo),
            "commit-tree",
            f"{initial_commit}^{{tree}}",
            "-p",
            initial_commit,
            "-m",
            "inactive branch commit",
        ],
        text=True,
    ).strip()
    _git(source_repo, "update-ref", "refs/heads/inactive", other_commit)
    repo = tmp_path / "linked-worktree"
    _git(source_repo, "worktree", "add", "--detach", str(repo), "main")
    handler = RepositoryEventHandler(_graph_builder(), repo, perform_initial_scan=False)
    refs_dir = handler.git_metadata_dirs[-1] / "refs" / "heads"

    with patch.object(handler, "_debounce") as debounce:
        handler.on_modified(
            SimpleNamespace(
                is_directory=False,
                src_path=str(refs_dir / "inactive"),
            )
        )
        debounce.assert_not_called()

        _git(repo, "checkout", other_commit)
        handler.on_modified(
            SimpleNamespace(
                is_directory=False,
                src_path=str(handler.git_metadata_dirs[0] / "HEAD"),
            )
        )
        debounce.assert_called_once()


def test_watcher_resyncs_after_switching_to_same_commit_branch(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Watcher Test")
    _git(repo, "config", "user.email", "watcher-test@example.invalid")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    _git(repo, "branch", "same-commit")
    handler = RepositoryEventHandler(_graph_builder(), repo, perform_initial_scan=False)
    previous_snapshot = handler._git_head_snapshot

    _git(repo, "switch", "same-commit")
    head_path = str(handler.git_metadata_dirs[0] / "HEAD")
    with patch.object(handler, "_debounce") as debounce:
        handler.on_modified(SimpleNamespace(is_directory=False, src_path=head_path))

    assert handler._git_head_snapshot != previous_snapshot
    debounce.assert_called_once_with(head_path, handler._require_disk_resync)


def test_watcher_resyncs_after_reftable_active_branch_update(tmp_path: Path):
    repo = tmp_path / "reftable-repo"
    repo.mkdir()
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "init",
            "--ref-format=reftable",
            "--initial-branch=main",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.skip(f"installed Git does not support reftable refs: {result.stderr.strip()}")

    _git(repo, "config", "user.name", "Reftable Watcher Test")
    _git(repo, "config", "user.email", "reftable-watcher-test@example.invalid")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    handler = RepositoryEventHandler(_graph_builder(), repo, perform_initial_scan=False)
    initial_snapshot = handler._git_head_snapshot
    tables_list = handler.git_metadata_dirs[0] / "reftable" / "tables.list"
    assert tables_list.is_file()
    assert initial_snapshot[0] == "commit"

    _git(repo, "commit", "--allow-empty", "-m", "active branch update")
    expected_commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    assert expected_commit != initial_snapshot[1]
    with patch.object(handler, "_debounce") as debounce:
        handler.on_modified(SimpleNamespace(is_directory=False, src_path=str(tables_list)))

    assert handler._git_head_snapshot == ("commit", expected_commit, "main")
    debounce.assert_called_once_with(str(tables_list), handler._require_disk_resync)


def test_watcher_initial_scan_filters_cgcignore(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / ".cgcignore").write_text("**/bin/\n", encoding="utf-8")
    ignored_file = repo / "bin" / "generated.py"
    tracked_file = repo / "src" / "app.py"
    ignored_file.write_text("print('ignored')\n", encoding="utf-8")
    tracked_file.write_text("print('tracked')\n", encoding="utf-8")

    graph_builder = _graph_builder()
    RepositoryEventHandler(graph_builder, repo, perform_initial_scan=True)

    parsed_paths = [call.args[1] for call in graph_builder.parse_file.call_args_list]
    assert parsed_paths == [tracked_file]
    pre_scan_paths = graph_builder.pre_scan_imports.call_args.args[0]
    assert pre_scan_paths == [tracked_file]


def test_watcher_incremental_reparse_filters_ignored_affected_files(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "obj").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / ".cgcignore").write_text("**/obj/\n", encoding="utf-8")
    changed_file = repo / "src" / "module.py"
    ignored_caller = repo / "obj" / "generated.py"
    changed_file.write_text("def module(): pass\n", encoding="utf-8")
    ignored_caller.write_text("def generated(): pass\n", encoding="utf-8")

    graph_builder = _graph_builder()
    graph_builder.get_caller_file_paths.return_value = {str(ignored_caller.resolve())}
    handler = RepositoryEventHandler(graph_builder, repo, perform_initial_scan=False)

    handler._handle_modification(str(changed_file))

    graph_builder.update_file_in_graph.assert_called_once()
    assert graph_builder.update_file_in_graph.call_args.args[0] == changed_file
    # Callers under cgcignore must not be pulled into incremental relink.
    graph_builder.delete_outgoing_calls_from_files.assert_not_called()


def test_watcher_keeps_last_valid_ignore_spec_when_reload_fails(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".cgcignore").write_text("ignored.py\n", encoding="utf-8")
    ignored_file = repo / "ignored.py"
    ignored_file.write_text("def ignored(): pass\n", encoding="utf-8")
    included_file = repo / "included.py"
    included_file.write_text("def included(): pass\n", encoding="utf-8")

    graph_builder = _graph_builder()
    graph_builder.get_repo_file_paths.return_value = []
    graph_builder.update_file_in_graph.return_value = None
    handler = RepositoryEventHandler(graph_builder, repo, perform_initial_scan=False)
    previous_spec = handler.ignore_spec
    assert previous_spec is not None

    with patch.object(watcher_module, "build_ignore_spec", side_effect=OSError("unreadable rules")):
        handler.synchronize_with_disk()

    assert handler.ignore_spec is previous_spec
    assert handler._should_ignore(ignored_file)
    updated_paths = {Path(call.args[0]).resolve() for call in graph_builder.update_file_in_graph.call_args_list}
    assert ignored_file.resolve() not in updated_paths
    assert included_file.resolve() in updated_paths


def test_watcher_keeps_last_valid_ignore_spec_when_gitignore_read_fails(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    gitignore = repo / ".gitignore"
    gitignore.write_text("ignored.py\n", encoding="utf-8")
    ignored_file = repo / "ignored.py"
    ignored_file.write_text("def ignored(): pass\n", encoding="utf-8")
    included_file = repo / "included.py"
    included_file.write_text("def included(): pass\n", encoding="utf-8")

    graph_builder = _graph_builder()
    graph_builder.get_repo_file_paths.return_value = []
    graph_builder.update_file_in_graph.return_value = None
    handler = RepositoryEventHandler(graph_builder, repo, perform_initial_scan=False)
    previous_spec = handler.ignore_spec
    assert previous_spec is not None
    assert handler._should_ignore(ignored_file)
    original_read_text = Path.read_text

    def fail_gitignore_read(path, *args, **kwargs):
        if path == gitignore:
            raise OSError("unreadable gitignore")
        return original_read_text(path, *args, **kwargs)

    with patch.object(Path, "read_text", new=fail_gitignore_read):
        handler.synchronize_with_disk()

    assert handler.ignore_spec is previous_spec
    assert handler._should_ignore(ignored_file)
    updated_paths = {Path(call.args[0]).resolve() for call in graph_builder.update_file_in_graph.call_args_list}
    assert ignored_file.resolve() not in updated_paths
    assert included_file.resolve() in updated_paths


def test_watcher_keeps_last_valid_ignore_spec_when_gitignore_is_invalid_utf8(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    gitignore = repo / ".gitignore"
    gitignore.write_text("ignored.py\n", encoding="utf-8")
    ignored_file = repo / "ignored.py"
    ignored_file.write_text("def ignored(): pass\n", encoding="utf-8")
    included_file = repo / "included.py"
    included_file.write_text("def included(): pass\n", encoding="utf-8")

    graph_builder = _graph_builder()
    graph_builder.get_repo_file_paths.return_value = []
    graph_builder.update_file_in_graph.return_value = None
    handler = RepositoryEventHandler(graph_builder, repo, perform_initial_scan=False)
    previous_spec = handler.ignore_spec
    assert previous_spec is not None

    gitignore.write_bytes(b"\xff\xfeinvalid utf-8\n")
    handler.synchronize_with_disk()

    assert handler.ignore_spec is previous_spec
    assert handler._should_ignore(ignored_file)
    updated_paths = {Path(call.args[0]).resolve() for call in graph_builder.update_file_in_graph.call_args_list}
    assert ignored_file.resolve() not in updated_paths
    assert included_file.resolve() in updated_paths


def test_watcher_uses_fallback_ignore_rules_when_gitignore_is_unreadable_at_startup(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".cgcignore").write_text("local-ignored.py\n", encoding="utf-8")
    gitignore = repo / ".gitignore"
    gitignore.write_text("git-ignored.py\n", encoding="utf-8")
    local_ignored = repo / "local-ignored.py"
    default_ignored = repo / "obj" / "generated.py"
    local_ignored.write_text("def local_ignored(): pass\n", encoding="utf-8")
    default_ignored.parent.mkdir()
    default_ignored.write_text("def generated(): pass\n", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_gitignore_read(path, *args, **kwargs):
        if path == gitignore:
            raise OSError("unreadable gitignore")
        return original_read_text(path, *args, **kwargs)

    graph_builder = _graph_builder()
    with patch.object(Path, "read_text", new=fail_gitignore_read):
        handler = RepositoryEventHandler(
            graph_builder,
            repo,
            perform_initial_scan=False,
        )
        with patch.object(handler, "_debounce") as debounce:
            handler.on_created(SimpleNamespace(is_directory=False, src_path=str(local_ignored)))
            handler.on_created(SimpleNamespace(is_directory=False, src_path=str(default_ignored)))

    assert handler.ignore_spec is not None
    debounce.assert_not_called()


def test_watcher_keeps_default_ignore_rules_when_cgcignore_is_unreadable_at_startup(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    cgcignore = repo / ".cgcignore"
    cgcignore.write_text("custom-ignored.py\n", encoding="utf-8")
    gitignore = repo / ".gitignore"
    gitignore.write_text("git-ignored.py\n", encoding="utf-8")
    ignored_file = repo / "obj" / "generated.py"
    ignored_file.parent.mkdir()
    ignored_file.write_text("def generated(): pass\n", encoding="utf-8")
    git_ignored_file = repo / "git-ignored.py"
    git_ignored_file.write_text("def git_ignored(): pass\n", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_cgcignore_read(path, *args, **kwargs):
        if path == cgcignore:
            raise OSError("unreadable cgcignore")
        return original_read_text(path, *args, **kwargs)

    handler = None
    with patch.object(Path, "read_text", new=fail_cgcignore_read):
        handler = RepositoryEventHandler(
            _graph_builder(),
            repo,
            perform_initial_scan=False,
        )
        with patch.object(handler, "_debounce") as debounce:
            handler.on_created(SimpleNamespace(is_directory=False, src_path=str(ignored_file)))

    assert handler.ignore_spec is not None
    assert handler._should_ignore(ignored_file)
    assert handler._should_ignore(git_ignored_file), (
        "an unreadable .cgcignore must not cause the startup fallback to drop readable .gitignore rules"
    )
    debounce.assert_not_called()


def test_watcher_modification_does_not_rewrite_repository_metadata(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    changed_file = repo / "module.py"
    changed_file.write_text("def module(): pass\n", encoding="utf-8")
    graph_builder = _graph_builder()
    handler = RepositoryEventHandler(graph_builder, repo, perform_initial_scan=False)

    handler._handle_modification(str(changed_file))

    graph_builder.add_repository_to_graph.assert_not_called()


class _SharedObserver:
    def __init__(self):
        self.watches = {}
        self.handlers = {}
        self.removed_handlers = []
        self.unscheduled = []

    def schedule(self, handler, path, recursive=False):
        key = (str(Path(path).resolve()), recursive)
        watch = self.watches.setdefault(key, object())
        self.handlers.setdefault(watch, set()).add(handler)
        return watch

    def remove_handler_for_watch(self, handler, watch):
        self.handlers[watch].remove(handler)
        self.removed_handlers.append((handler, watch))

    def unschedule(self, watch):
        self.unscheduled.append(watch)


def test_unwatch_keeps_shared_external_ignore_parent_watch_until_last_owner(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "global-config"
    config_dir.mkdir()
    explicit_ignore = config_dir / ".cgcignore"
    explicit_ignore.write_text("*.tmp\n", encoding="utf-8")
    repos = [tmp_path / "repo-a", tmp_path / "repo-b"]
    for repo in repos:
        repo.mkdir()

    observer = _SharedObserver()
    monkeypatch.setattr(watcher_module, "Observer", lambda: observer)
    watcher = CodeWatcher(_graph_builder(), use_polling=False)
    for repo in repos:
        watcher.watch_directory(str(repo), perform_initial_scan=False, cgcignore_path=str(explicit_ignore))
    handlers = [watcher.handlers[str(repo.resolve())] for repo in repos]
    shared_watch = observer.watches[(str(config_dir.resolve()), False)]

    watcher.unwatch_directory(str(repos[0]))

    assert shared_watch not in observer.unscheduled, (
        "removing one repository must preserve a global ignore-parent watch used by another"
    )
    assert observer.handlers[shared_watch] == {handlers[1]}, (
        "the shared watch must stop dispatching events to the repository that was removed"
    )

    watcher.unwatch_directory(str(repos[1]))

    assert observer.unscheduled.count(shared_watch) == 1, (
        "the shared ignore-parent watch should be unscheduled after its last owner stops"
    )
    assert observer.handlers[shared_watch] == set()


def test_concurrent_unwatch_unschedules_a_shared_external_watch_once(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "global-config"
    config_dir.mkdir()
    explicit_ignore = config_dir / ".cgcignore"
    explicit_ignore.write_text("*.tmp\n", encoding="utf-8")
    repos = [tmp_path / "repo-a", tmp_path / "repo-b"]
    for repo in repos:
        repo.mkdir()

    observer = _SharedObserver()
    monkeypatch.setattr(watcher_module, "Observer", lambda: observer)
    watcher = CodeWatcher(_graph_builder(), use_polling=False)
    for repo in repos:
        watcher.watch_directory(str(repo), perform_initial_scan=False, cgcignore_path=str(explicit_ignore))
    shared_watch = observer.watches[(str(config_dir.resolve()), False)]

    first_unwatch_at_shared_watch = threading.Event()
    release_first_unwatch = threading.Event()
    second_unwatch_at_shared_watch = threading.Event()
    original_remove = observer.remove_handler_for_watch

    def coordinate_shared_removal(handler_arg, watch):
        if watch is shared_watch:
            if threading.current_thread().name == "unwatch-repo-a":
                first_unwatch_at_shared_watch.set()
                assert release_first_unwatch.wait(timeout=5)
            else:
                second_unwatch_at_shared_watch.set()
        original_remove(handler_arg, watch)

    monkeypatch.setattr(observer, "remove_handler_for_watch", coordinate_shared_removal)
    errors = []

    def unwatch_repo(repo):
        try:
            watcher.unwatch_directory(str(repo))
        except Exception as exc:  # noqa: BLE001 - capture concurrent cleanup failures
            errors.append(exc)

    first = threading.Thread(target=unwatch_repo, args=(repos[0],), name="unwatch-repo-a")
    first.start()
    assert first_unwatch_at_shared_watch.wait(timeout=2)
    second = threading.Thread(target=unwatch_repo, args=(repos[1],), name="unwatch-repo-b")
    second.start()
    try:
        assert not second_unwatch_at_shared_watch.wait(timeout=0.2), (
            "shared registration ownership cleanup must serialize across handlers"
        )
    finally:
        release_first_unwatch.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert observer.unscheduled.count(shared_watch) == 1
    assert observer.handlers[shared_watch] == set()


def test_watcher_discovers_git_metadata_created_after_watch(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    observer = _SharedObserver()
    monkeypatch.setattr(watcher_module, "Observer", lambda: observer)
    watcher = CodeWatcher(_graph_builder(), use_polling=False)
    watcher.watch_directory(str(repo), perform_initial_scan=False)
    handler = watcher.handlers[str(repo.resolve())]

    assert handler.git_metadata_dirs == ()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Watcher Test")
    _git(repo, "config", "user.email", "watcher-test@example.invalid")
    _git(repo, "commit", "--allow-empty", "-m", "created after watch")

    with (
        patch.object(handler, "_require_disk_resync") as resync,
        patch.object(handler, "_debounce") as debounce,
    ):
        handler.on_created(SimpleNamespace(is_directory=True, src_path=str(repo / ".git")))

        assert handler.git_metadata_dirs == handler._discover_git_metadata_dirs()
        assert handler._git_head_snapshot == (
            "commit",
            subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(),
            "main",
        )
        resync.assert_called_once_with()

        _git(repo, "commit", "--allow-empty", "-m", "active commit update")
        handler.on_modified(
            SimpleNamespace(
                is_directory=False,
                src_path=str(repo / ".git" / "refs" / "heads" / "main"),
            )
        )
        debounce.assert_called_once()

    watcher.unwatch_directory(str(repo))


def test_watcher_registers_worktree_git_paths_after_git_entry_appears(tmp_path: Path, monkeypatch):
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    _git(source_repo, "init", "--initial-branch=main")
    _git(source_repo, "config", "user.name", "Watcher Test")
    _git(source_repo, "config", "user.email", "watcher-test@example.invalid")
    _git(source_repo, "commit", "--allow-empty", "-m", "initial")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    observer = _SharedObserver()
    monkeypatch.setattr(watcher_module, "Observer", lambda: observer)
    watcher = CodeWatcher(_graph_builder(), use_polling=False)
    watcher.watch_directory(str(worktree), perform_initial_scan=False)
    handler = watcher.handlers[str(worktree.resolve())]
    assert handler.git_metadata_dirs == ()
    (worktree / ".cgcignore").unlink()

    _git(source_repo, "worktree", "add", "--detach", str(worktree), "main")
    handler.on_created(SimpleNamespace(is_directory=False, src_path=str(worktree / ".git")))

    assert handler.git_metadata_dirs == handler._discover_git_metadata_dirs()
    for metadata_dir in handler.git_metadata_dirs:
        assert (str(metadata_dir), False) in observer.watches
    refs_dir = handler.git_metadata_dirs[-1] / "refs"
    assert (str(refs_dir), True) in observer.watches

    watcher.unwatch_directory(str(worktree))


def test_watcher_registers_external_reftable_path_for_linked_worktree(
    tmp_path: Path, monkeypatch
):
    source_repo = tmp_path / "reftable-source"
    source_repo.mkdir()
    result = subprocess.run(
        [
            "git",
            "-C",
            str(source_repo),
            "init",
            "--ref-format=reftable",
            "--initial-branch=main",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.skip(f"installed Git does not support reftable refs: {result.stderr.strip()}")

    _git(source_repo, "config", "user.name", "Reftable Worktree Test")
    _git(source_repo, "config", "user.email", "reftable-worktree-test@example.invalid")
    _git(source_repo, "commit", "--allow-empty", "-m", "initial")
    worktree = tmp_path / "reftable-worktree"
    _git(source_repo, "worktree", "add", "-b", "watched", str(worktree), "main")

    observer = _SharedObserver()
    monkeypatch.setattr(watcher_module, "Observer", lambda: observer)
    watcher = CodeWatcher(_graph_builder(), use_polling=False)
    watcher.watch_directory(str(worktree), perform_initial_scan=False)
    handler = watcher.handlers[str(worktree.resolve())]
    reftable_dir = handler.git_metadata_dirs[-1] / "reftable"

    assert reftable_dir.is_dir()
    assert (str(reftable_dir), False) in observer.watches, (
        "linked worktree watchers must observe common reftable tables.list changes"
    )

    watcher.unwatch_directory(str(worktree))


def test_unwatch_serializes_with_in_progress_ignore_watch_refresh(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    rules_a = tmp_path / "rules-a"
    rules_a.mkdir()
    rules_b = tmp_path / "rules-b"
    rules_b.mkdir()
    (rules_a / ".cgcignore").write_text("*.tmp\n", encoding="utf-8")
    (rules_b / ".cgcignore").write_text("*.log\n", encoding="utf-8")
    configured_path = config_dir / ".cgcignore"
    configured_path.symlink_to(rules_a / ".cgcignore")

    observer = _SharedObserver()
    monkeypatch.setattr(watcher_module, "Observer", lambda: observer)
    watcher = CodeWatcher(_graph_builder(), use_polling=False)
    watcher.watch_directory(str(repo), perform_initial_scan=False, cgcignore_path=str(configured_path))
    repo_key = str(repo.resolve())
    handler = watcher.handlers[repo_key]

    configured_path.unlink()
    configured_path.symlink_to(rules_b / ".cgcignore")
    expected_total_removals = len(watcher.watches[repo_key])
    schedule_entered = threading.Event()
    release_schedule = threading.Event()
    removed_lock = threading.Lock()
    removed_count = 0
    all_current_watches_removed = threading.Event()
    original_schedule = observer.schedule
    original_remove = observer.remove_handler_for_watch

    def block_new_target_schedule(handler_arg, path, recursive=False):
        if Path(path).resolve() == rules_b.resolve():
            schedule_entered.set()
            assert release_schedule.wait(timeout=5)
        return original_schedule(handler_arg, path, recursive)

    def count_removed_handlers(handler_arg, watch):
        nonlocal removed_count
        original_remove(handler_arg, watch)
        with removed_lock:
            removed_count += 1
            if removed_count >= expected_total_removals:
                all_current_watches_removed.set()

    monkeypatch.setattr(observer, "schedule", block_new_target_schedule)
    monkeypatch.setattr(observer, "remove_handler_for_watch", count_removed_handlers)

    def refresh_ignore_target():
        handler._refresh_ignore_metadata_targets()
        handler._refresh_ignore_watches()

    refresh_thread = threading.Thread(target=refresh_ignore_target)
    refresh_thread.start()
    assert schedule_entered.wait(timeout=2), "refresh should schedule the new target parent"

    # The refresh has already removed the old target watch and is paused before
    # registering the replacement. Teardown must wait for this in-flight update.
    unwatch_started = threading.Event()
    unwatch_done = threading.Event()

    def unwatch():
        unwatch_started.set()
        watcher.unwatch_directory(str(repo))
        unwatch_done.set()

    unwatch_thread = threading.Thread(target=unwatch)
    unwatch_thread.start()
    try:
        assert unwatch_started.wait(timeout=2)
        assert not all_current_watches_removed.wait(timeout=0.2), (
            "teardown must serialize with the in-progress watch registration"
        )
        assert not unwatch_done.is_set()
    finally:
        release_schedule.set()
        refresh_thread.join(timeout=5)
        unwatch_thread.join(timeout=5)

    assert not refresh_thread.is_alive()
    assert not unwatch_thread.is_alive()
    assert unwatch_done.is_set()
    assert all(handler not in attached_handlers for attached_handlers in observer.handlers.values()), (
        "unwatch must detach the handler from watches registered during refresh"
    )


def test_stop_waits_for_external_watch_refresh_before_stopping_observer(tmp_path: Path, monkeypatch):
    class StoppableObserver(_SharedObserver):
        def __init__(self):
            super().__init__()
            self.stopped = False
            self.stop_called = threading.Event()
            self.scheduled_after_stop = []

        def is_alive(self):
            return True

        def stop(self):
            self.stopped = True
            self.stop_called.set()

        def join(self):
            return None

        def schedule(self, handler, path, recursive=False):
            if self.stopped:
                self.scheduled_after_stop.append(str(path))
            return super().schedule(handler, path, recursive)

    repo = tmp_path / "repo"
    repo.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    rules_a = tmp_path / "rules-a"
    rules_a.mkdir()
    rules_b = tmp_path / "rules-b"
    rules_b.mkdir()
    (rules_a / ".cgcignore").write_text("*.tmp\n", encoding="utf-8")
    (rules_b / ".cgcignore").write_text("*.log\n", encoding="utf-8")
    configured_path = config_dir / ".cgcignore"
    configured_path.symlink_to(rules_a / ".cgcignore")

    observer = StoppableObserver()
    monkeypatch.setattr(watcher_module, "Observer", lambda: observer)
    watcher = CodeWatcher(_graph_builder(), use_polling=False)
    watcher.watch_directory(str(repo), perform_initial_scan=False, cgcignore_path=str(configured_path))
    handler = watcher.handlers[str(repo.resolve())]

    configured_path.unlink()
    configured_path.symlink_to(rules_b / ".cgcignore")
    schedule_entered = threading.Event()
    release_schedule = threading.Event()
    original_schedule = observer.schedule

    def block_new_target_schedule(handler_arg, path, recursive=False):
        if Path(path).resolve() == rules_b.resolve():
            schedule_entered.set()
            assert release_schedule.wait(timeout=5)
        return original_schedule(handler_arg, path, recursive)

    monkeypatch.setattr(observer, "schedule", block_new_target_schedule)
    handler._require_disk_resync()
    assert schedule_entered.wait(timeout=2), "disk reconciliation should register the new ignore parent"

    stop_started = threading.Event()
    stop_errors = []

    def stop_watcher():
        stop_started.set()
        try:
            watcher.stop()
        except Exception as exc:  # noqa: BLE001 - capture shutdown failures
            stop_errors.append(exc)

    stop_thread = threading.Thread(target=stop_watcher)
    stop_thread.start()
    assert stop_started.wait(timeout=2)
    try:
        assert not observer.stop_called.wait(timeout=0.2), (
            "observer shutdown must wait until in-flight reconciliation finishes watch registration"
        )
    finally:
        release_schedule.set()
        stop_thread.join(timeout=5)

    assert not stop_thread.is_alive()
    assert stop_errors == []
    assert observer.scheduled_after_stop == []
