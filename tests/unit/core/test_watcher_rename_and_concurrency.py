"""Regression tests for watcher rename handling and concurrent updates."""
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from codegraphcontext.core import watcher as watcher_mod
from codegraphcontext.core.watcher import RepositoryEventHandler


def _handler(tmp_path):
    handler = RepositoryEventHandler.__new__(RepositoryEventHandler)
    handler.graph_builder = MagicMock()
    handler.graph_builder.parsers = {".py": "python"}
    handler.graph_builder.get_importer_file_paths.return_value = set()
    handler.repo_path = Path(tmp_path)
    handler.debounce_interval = 0.01
    handler._update_lock = threading.RLock()
    handler._event_condition = threading.Condition()
    handler._pending_events = {}
    handler._worker_thread = None
    handler._stopped = False
    handler._resync_required = False
    handler._resync_retry_at = 0.0
    handler._resync_retry_delay = handler.RESYNC_RETRY_INITIAL_DELAY_SECONDS
    handler._event_generation = 0
    handler.imports_map = {}
    handler.all_file_data = []
    handler.ignore_spec = None
    handler.ignore_root = Path(tmp_path)
    return handler


def test_on_moved_handles_both_endpoints(tmp_path):
    """Only dest_path was processed, so the node for the *old* path and all of
    its symbols stayed in the graph forever. Every rename duplicated every
    symbol in the file; a refactoring session or a `git checkout` between
    branches accumulated them indefinitely."""
    handler = _handler(tmp_path)
    queued = []
    processed = []

    def debounce(path, action, **kwargs):
        queued.append((path, kwargs.get("coalesce_key")))
        action()

    handler._debounce = debounce
    handler._handle_modification = lambda path: processed.append(("modify", path))
    handler._handle_removal = lambda path: processed.append(("remove", path))

    handler.on_moved(SimpleNamespace(
        is_directory=False,
        src_path=str(tmp_path / "oldname.py"),
        dest_path=str(tmp_path / "newname.py"),
    ))

    assert queued == [
        (
            str(tmp_path / "newname.py"),
            (str(tmp_path / "oldname.py"), str(tmp_path / "newname.py")),
        )
    ]
    assert processed == [
        ("modify", str(tmp_path / "newname.py")),
        ("remove", str(tmp_path / "oldname.py")),
    ], "the destination must be indexed before callers of the source are relinked"


def test_on_moved_directory_schedules_disk_resync(tmp_path):
    handler = _handler(tmp_path)
    sync_started = threading.Event()
    finish_sync = threading.Event()

    def synchronize_with_disk():
        sync_started.set()
        finish_sync.wait(timeout=5)

    handler.synchronize_with_disk = synchronize_with_disk
    worker = None
    try:
        handler.on_moved(SimpleNamespace(
            is_directory=True,
            src_path=str(tmp_path / "a"),
            dest_path=str(tmp_path / "b"),
        ))

        assert sync_started.wait(timeout=2), "directory move did not schedule disk reconciliation"
        assert handler._resync_required
        worker = handler._worker_thread
    finally:
        finish_sync.set()
        handler.stop()

    assert worker is not None and not worker.is_alive()


def test_handle_removal_relinks_callers_of_the_removed_file(tmp_path):
    handler = _handler(tmp_path)
    old = tmp_path / "oldname.py"
    caller = tmp_path / "caller.py"
    caller.write_text("def call_old():\n    return oldname()\n", encoding="utf-8")
    caller_path = caller.resolve().as_posix()
    handler.graph_builder.parsers = {".py": "python"}
    handler.graph_builder.get_caller_file_paths.return_value = {caller_path}
    handler.graph_builder.get_inheritance_neighbor_paths.return_value = set()
    handler.graph_builder.get_repo_class_lookup.return_value = {}
    handler.graph_builder.update_file_in_graph.return_value = {"deleted": True}
    handler.graph_builder.parse_file.return_value = {"path": caller_path, "functions": []}

    handler._handle_removal(str(old))

    handler.graph_builder.update_file_in_graph.assert_called_once()
    handler.graph_builder.delete_outgoing_calls_from_files.assert_called_once_with([caller_path])
    handler.graph_builder.link_function_calls.assert_called_once()


def test_modification_relinks_importers_of_a_new_definition(tmp_path):
    handler = _handler(tmp_path)
    changed = tmp_path / "new_module.py"
    caller = tmp_path / "client.py"
    changed.write_text("def added_symbol():\n    return 1\n", encoding="utf-8")
    caller.write_text(
        "from new_module import added_symbol\n\n"
        "def run():\n    return added_symbol()\n",
        encoding="utf-8",
    )
    changed_path = changed.resolve().as_posix()
    caller_path = caller.resolve().as_posix()
    handler.graph_builder.get_caller_file_paths.return_value = set()
    handler.graph_builder.get_inheritance_neighbor_paths.return_value = set()
    handler.graph_builder.get_importer_file_paths.return_value = {caller_path}
    handler.graph_builder.pre_scan_imports.return_value = {
        "added_symbol": [changed_path]
    }
    handler.graph_builder.get_repo_class_lookup.return_value = {}
    handler.graph_builder.update_file_in_graph.return_value = {
        "path": changed_path,
        "functions": [],
    }
    handler.graph_builder.parse_file.side_effect = lambda _repo, path: {
        "path": Path(path).resolve().as_posix(),
        "functions": [],
    }

    handler._handle_modification(changed_path)

    import_names = handler.graph_builder.get_importer_file_paths.call_args.args[0]
    assert {"added_symbol", "new_module"} <= import_names
    handler.graph_builder.delete_outgoing_calls_from_files.assert_called_once_with(
        [caller_path]
    )
    parsed_paths = {
        call.args[1].resolve().as_posix()
        for call in handler.graph_builder.parse_file.call_args_list
    }
    assert parsed_paths == {changed_path, caller_path}


def test_handle_removal_survives_a_failing_delete(tmp_path):
    handler = _handler(tmp_path)
    handler.graph_builder.update_file_in_graph.side_effect = RuntimeError("boom")
    sync_started = threading.Event()
    finish_sync = threading.Event()

    def synchronize_with_disk():
        sync_started.set()
        finish_sync.wait(timeout=5)

    handler.synchronize_with_disk = synchronize_with_disk

    try:
        handler._handle_removal(str(tmp_path / "x.py"))   # must not raise
        assert sync_started.wait(timeout=2), "failed removal did not start a recovery worker"
        assert handler._resync_required, "a failed removal must request disk reconciliation"
    finally:
        finish_sync.set()
        handler.stop()


def test_move_destination_failure_requests_disk_resync(tmp_path):
    handler = _handler(tmp_path)
    sync_started = threading.Event()
    finish_sync = threading.Event()
    removed = []

    def fail_modification(_path):
        raise RuntimeError("boom")

    handler._handle_modification = fail_modification
    handler._handle_removal = lambda path: removed.append(path)

    def synchronize_with_disk():
        sync_started.set()
        finish_sync.wait(timeout=5)

    handler.synchronize_with_disk = synchronize_with_disk
    try:
        handler.on_moved(SimpleNamespace(
            is_directory=False,
            src_path=str(tmp_path / "old.py"),
            dest_path=str(tmp_path / "new.py"),
        ))

        assert sync_started.wait(timeout=2), "failed move did not trigger disk reconciliation"
        assert handler._resync_required
        assert removed == [], "source removal must wait for successful destination indexing"
    finally:
        finish_sync.set()
        worker = handler._worker_thread
        handler.stop()

    assert worker is not None and not worker.is_alive()


def test_large_event_burst_uses_bounded_pending_work_and_one_worker(tmp_path, monkeypatch):
    """A burst of unique paths must not create one timer thread per path."""
    handler = _handler(tmp_path)
    handler.debounce_interval = 3600
    handler.MAX_PENDING_EVENTS = 32
    sync_started = threading.Event()
    retry_sync_started = threading.Event()
    allow_sync_to_finish = threading.Event()
    sync_call_lock = threading.Lock()
    sync_calls = 0

    def synchronize_with_disk():
        nonlocal sync_calls
        with sync_call_lock:
            sync_calls += 1
            call_number = sync_calls
        if call_number == 1:
            sync_started.set()
            raise RuntimeError("temporary reconciliation failure")
        retry_sync_started.set()
        allow_sync_to_finish.wait(timeout=5)

    handler.synchronize_with_disk = synchronize_with_disk

    created_timers = []

    class FakeTimer:
        def __init__(self, *args, **kwargs):
            created_timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(watcher_mod.threading, "Timer", FakeTimer)
    worker = None
    try:
        for _ in range(5000):
            handler._debounce(f"{tmp_path}/repeated.py", lambda: None)
        assert len(handler._pending_events) == 1, "repeated edits should coalesce by path"

        for index in range(handler.MAX_PENDING_EVENTS + 1):
            handler._debounce(f"{tmp_path}/first_{index}.py", lambda: None)

        assert len(created_timers) == 0, "event burst created per-path timer objects"
        assert sync_started.wait(timeout=2), "overflow did not schedule one disk reconciliation"
        assert retry_sync_started.wait(timeout=2), "failed reconciliation was not retried"
        assert handler._resync_required, "failed reconciliation must keep the resync pending"

        # While reconciliation is blocked, thousands of new unique paths must
        # be represented by the one pending resync flag, not retained work.
        for index in range(5000):
            handler._debounce(f"{tmp_path}/storm_{index}.py", lambda: None)

        assert len(handler._pending_events) <= handler.MAX_PENDING_EVENTS
        assert handler._resync_required
        worker = handler._worker_thread
        assert worker is not None and worker.is_alive()
        assert sum(t.name == worker.name for t in threading.enumerate()) == 1
    finally:
        allow_sync_to_finish.set()
        handler.stop()

    assert worker is not None and not worker.is_alive()
    assert len(created_timers) == 0


def test_debounce_replaces_a_pending_timer(tmp_path):
    handler = _handler(tmp_path)
    calls = []
    fired = threading.Event()
    handler._debounce("a.py", lambda: calls.append(1))
    handler._debounce("a.py", lambda: (calls.append(2), fired.set()))

    assert fired.wait(timeout=2), "latest debounced action did not run"
    handler.stop()
    assert calls == [2], "the superseded action should not run"


def test_modification_handler_is_serialised(tmp_path):
    """Debounce is keyed per path, so N files changed inside the window fire N
    handler threads. They did read-modify-write on the shared imports_map and
    interleaved delete/add for overlapping caller sets, so one could delete
    edges another had just created."""
    handler = _handler(tmp_path)
    concurrent = []
    active = []
    lock = threading.Lock()

    def _slow(_path):
        with lock:
            active.append(1)
            concurrent.append(len(active))
        time.sleep(0.05)
        with lock:
            active.pop()

    handler._handle_modification_locked = _slow
    threads = [
        threading.Thread(target=handler._handle_modification, args=(f"f{i}.py",))
        for i in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max(concurrent) == 1, f"handlers ran concurrently (max {max(concurrent)})"


def test_cancel_pending_events_discards_delayed_actions(tmp_path):
    handler = _handler(tmp_path)
    handler.debounce_interval = 3600
    fired = threading.Event()

    handler._debounce("a.py", fired.set)
    handler.cancel_timers()
    handler.stop()

    assert not fired.is_set()
