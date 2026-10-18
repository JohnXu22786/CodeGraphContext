"""#1519: CodeWatcher.stop() must clear watch state, not just handlers.

A stopped watchdog Observer cannot be restarted; a surviving watched_paths
entry makes watch_directory answer "Already watching" for a dead observer.
"""
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from codegraphcontext.core.watcher import CodeWatcher, RepositoryEventHandler


def _bare_watcher():
    w = CodeWatcher.__new__(CodeWatcher)
    w.graph_builder = MagicMock()
    w.observer = MagicMock()
    w.observer.is_alive.return_value = True
    w.watched_paths = {"/repo/a"}
    w.watches = {"/repo/a": object()}
    handler = MagicMock()
    w.handlers = {"/repo/a": handler}
    return w, handler


def test_stop_clears_all_watch_state():
    w, handler = _bare_watcher()
    w.observer.is_alive.side_effect = [True, False]
    w.stop()
    w.stop()
    handler.stop.assert_called_once()
    assert w.handlers == {}
    assert w.watched_paths == set()
    assert w.watches == {}
    w.observer.stop.assert_called_once()
    w.observer.join.assert_called_once()


def test_handler_stop_is_idempotent_joins_inflight_work_and_rejects_late_events(tmp_path):
    handler = RepositoryEventHandler.__new__(RepositoryEventHandler)
    handler.repo_path = Path(tmp_path)
    handler.debounce_interval = 0.01
    handler._event_condition = threading.Condition()
    handler._pending_events = {}
    handler._worker_thread = None
    handler._stopped = False
    handler._resync_required = False
    handler._resync_retry_at = 0.0
    handler._resync_retry_delay = handler.RESYNC_RETRY_INITIAL_DELAY_SECONDS
    handler._event_generation = 0

    started = threading.Event()
    allow_write = threading.Event()
    stop_returned = threading.Event()
    writes = []

    def in_flight_write():
        started.set()
        allow_write.wait(timeout=5)
        writes.append("in-flight")

    handler._debounce(str(tmp_path / "queued.py"), in_flight_write)
    assert started.wait(timeout=2), "worker did not start queued work"
    worker = handler._worker_thread

    def stop_handler():
        handler.stop()
        stop_returned.set()

    stopper = threading.Thread(target=stop_handler)
    stopper.start()
    try:
        assert not stop_returned.wait(timeout=0.05), "stop returned while a job could still write"
    finally:
        allow_write.set()

    assert stop_returned.wait(timeout=2), "stop did not join the active worker"
    stopper.join(timeout=2)
    handler.stop()
    handler._debounce(str(tmp_path / "late.py"), lambda: writes.append("late"))
    time.sleep(0.05)

    assert not worker.is_alive()
    assert writes == ["in-flight"]
