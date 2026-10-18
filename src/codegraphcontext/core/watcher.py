"""
This module implements the live file-watching functionality using the `watchdog` library.
It observes directories for changes and triggers updates to the code graph.
"""

import os
import threading
import time
from pathlib import Path
import typing

from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

if typing.TYPE_CHECKING:
    from pathspec import PathSpec
    from ..tools.graph_builder import GraphBuilder

from .cgcignore import build_ignore_spec
from ..tools.indexing.constants import DEFAULT_IGNORE_PATTERNS
from ..cli.config_manager import get_config_value
from ..utils.debug_log import debug_log, info_logger, error_logger, warning_logger


POLLING_ENV_VAR = "CGC_WATCH_POLLING"
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def should_use_polling_observer(use_polling: typing.Optional[bool] = None) -> bool:
    if use_polling is not None:
        return use_polling
    return os.getenv(POLLING_ENV_VAR, "").strip().lower() in TRUE_ENV_VALUES


class RepositoryEventHandler(FileSystemEventHandler):
    MAX_PENDING_EVENTS = 256
    RESYNC_RETRY_INITIAL_DELAY_SECONDS = 0.1
    RESYNC_RETRY_MAX_DELAY_SECONDS = 30.0

    def __init__(
        self,
        graph_builder: "GraphBuilder",
        repo_path: Path,
        debounce_interval=2.0,
        perform_initial_scan: bool = True,
        cgcignore_path: str = None,
        ignore_spec: "PathSpec" = None,
        sync_on_start: bool = False,
    ):
        super().__init__()
        self.graph_builder = graph_builder
        self.repo_path = repo_path.resolve()
        self.debounce_interval = debounce_interval
        # Keep one pending action per path and process it on one worker. The
        # map is capped so a burst of unique paths cannot create one Timer
        # thread per file or retain an unbounded backlog.
        self._event_condition = threading.Condition()
        self._pending_events = {}
        self._worker_thread = None
        self._stopped = False
        self._resync_required = False
        self._resync_retry_at = 0.0
        self._resync_retry_delay = self.RESYNC_RETRY_INITIAL_DELAY_SECONDS
        self._event_generation = 0
        self._update_lock = threading.RLock()

        self.ignore_root = self.repo_path
        self.ignore_spec = ignore_spec
        self.cgcignore_path = cgcignore_path
        self._load_ignore_spec(cgcignore_path)

        self.all_file_data = []
        self.imports_map = {}

        if sync_on_start:
            self.synchronize_with_disk()
        elif perform_initial_scan:
            self._initial_scan()

    def _load_ignore_spec(self, cgcignore_path: str = None) -> None:
        if self.ignore_spec is not None:
            return
        try:
            self.ignore_spec, resolved = build_ignore_spec(
                ignore_root=self.ignore_root,
                default_patterns=DEFAULT_IGNORE_PATTERNS,
                explicit_path=cgcignore_path,
            )
            if resolved:
                debug_log(f"Watcher using ignore file: {resolved}")
        except OSError as e:
            self.ignore_spec = None
            warning_logger(f"Could not load ignore rules: {e}")

    def _should_ignore(self, path: str | Path) -> bool:
        path_obj = Path(path).resolve()
        ignore_root = getattr(self, "ignore_root", self.repo_path)

        ignore_dirs_str = get_config_value("IGNORE_DIRS") or ""
        if ignore_dirs_str:
            ignore_dirs = {d.strip().lower() for d in ignore_dirs_str.split(",") if d.strip()}
            try:
                parts = {p.lower() for p in path_obj.relative_to(ignore_root).parent.parts}
                if parts.intersection(ignore_dirs):
                    return True
            except ValueError:
                pass

        ignore_spec = getattr(self, "ignore_spec", None)
        if not ignore_spec:
            return False

        try:
            rel = path_obj.relative_to(ignore_root).as_posix()
        except ValueError:
            return False

        return ignore_spec.match_file(rel)

    def _is_supported_code_file(self, path: str | Path) -> bool:
        path_obj = Path(path)
        return (
            path_obj.is_file()
            and path_obj.suffix in self.graph_builder.parsers
            and not self._should_ignore(path_obj)
        )

    def _is_supported_event_path(self, path: str | Path) -> bool:
        """Check a watch event's path without requiring it to still exist."""
        path_obj = Path(path)
        is_indexable = (
            path_obj.suffix in self.graph_builder.parsers
            or path_obj.suffix in getattr(self.graph_builder, "generic_extensions", ())
            or path_obj.name in getattr(self.graph_builder, "generic_filenames", ())
        )
        return is_indexable and not self._should_ignore(path_obj)

    def _is_supported_event_file(self, path: str | Path) -> bool:
        """Check that an existing event target is indexable by the graph."""
        path_obj = Path(path)
        return path_obj.is_file() and self._is_supported_event_path(path_obj)

    def _iter_supported_files(self) -> list[Path]:
        from ..tools.indexing.discovery import discover_files_to_index

        supported = self.graph_builder.parsers.keys()
        # Forward the explicit ignore file and re-apply this handler's own spec.
        # Without them, discovery fell back to the repo-local .cgcignore, so the
        # initial scan and disk sync indexed files that later change events
        # would skip — the graph kept entries the user asked to exclude, and
        # they went stale on edit.
        files, _ = discover_files_to_index(
            self.repo_path,
            cgcignore_path=getattr(self, "cgcignore_path", None),
            supported_extensions=set(supported),
        )
        return [f for f in files if not self._should_ignore(f)]

    def _initial_scan(self):
        info_logger(f"Initial scan: {self.repo_path}")
        all_files = self._iter_supported_files()

        self.imports_map = self.graph_builder.pre_scan_imports(all_files)

        for f in all_files:
            parsed = self.graph_builder.parse_file(self.repo_path, f)
            if "error" not in parsed:
                self.all_file_data.append(parsed)

        repo_name = self.repo_path.name
        repo_path_str = self.repo_path.resolve().as_posix()

        self.graph_builder.add_repository_to_graph(self.repo_path, is_dependency=False)

        for fd in self.all_file_data:
            self.graph_builder.add_file_to_graph(
                fd, repo_name, self.imports_map, repo_path_str=repo_path_str
            )

        self.graph_builder.link_function_calls(self.all_file_data, self.imports_map)
        self.graph_builder.link_inheritance(self.all_file_data, self.imports_map)

        self.all_file_data.clear()
        info_logger("Initial scan complete")

    def synchronize_with_disk(self) -> None:
        info_logger(f"Syncing: {self.repo_path}")

        current_files = self._iter_supported_files()
        current_paths = {p.resolve().as_posix() for p in current_files}
        # Normalize stored paths lexically only: older Windows indexes stored
        # backslash paths, but resolving a foreign-platform path against the
        # local filesystem would prefix cwd and mark every file stale.
        indexed = {
            p.replace("\\", "/")
            for p in self.graph_builder.get_repo_file_paths(self.repo_path)
        }

        self.imports_map = self.graph_builder.pre_scan_imports(current_files)

        for stale in indexed - current_paths:
            self.graph_builder.delete_file_from_graph(stale)

        refreshed = []
        refreshed_paths: list[str] = []
        for p in current_files:
            fd = self.graph_builder.update_file_in_graph(
                p, self.repo_path, self.imports_map
            )
            if fd and "error" not in fd:
                refreshed.append(fd)
                refreshed_paths.append(p.resolve().as_posix())

        if refreshed_paths:
            # Only clear edges originating from the files we touched — do not
            # wipe the entire repo call graph like delete_relationship_links().
            self.graph_builder.delete_outgoing_calls_from_files(refreshed_paths)
            self.graph_builder.delete_inherits_for_files(refreshed_paths)
            self.graph_builder.link_function_calls(refreshed, self.imports_map)
            self.graph_builder.link_inheritance(refreshed, self.imports_map)

        info_logger("Sync complete")

    def _debounce(self, event_path, action, *, coalesce_key=None):
        with self._event_condition:
            if self._stopped:
                return

            self._event_generation += 1
            if self._resync_required:
                # The in-progress disk scan subsumes this event. Avoid even
                # resolving its path while the queue is collapsed to one sync.
                self._event_condition.notify()
                return

            event_key = coalesce_key or Path(event_path).resolve().as_posix()
            if event_key not in self._pending_events and len(self._pending_events) >= self.MAX_PENDING_EVENTS:
                # Reconcile from disk rather than dropping changes or growing
                # the queue when a burst exceeds the cap.
                self._pending_events.clear()
                self._resync_required = True
            else:
                self._pending_events[event_key] = (
                    time.monotonic() + self.debounce_interval,
                    action,
                )

            if self._worker_thread is None:
                self._worker_thread = threading.Thread(
                    target=self._process_pending_events,
                    name=f"cgc-watcher-{id(self):x}",
                    daemon=True,
                )
                self._worker_thread.start()
            self._event_condition.notify()

    def _require_disk_resync(self) -> None:
        """Collapse pending actions and reconcile disk after an event failure."""
        with self._event_condition:
            if self._stopped:
                return
            self._event_generation += 1
            self._pending_events.clear()
            self._resync_required = True
            self._resync_retry_at = 0.0
            self._resync_retry_delay = self.RESYNC_RETRY_INITIAL_DELAY_SECONDS
            self._event_condition.notify_all()

    def _process_pending_events(self) -> None:
        while True:
            action = None
            event_key = None
            resync_generation = None
            with self._event_condition:
                while not self._stopped and not self._resync_required and not self._pending_events:
                    self._event_condition.wait()

                if self._stopped:
                    return

                if self._resync_required:
                    retry_remaining = self._resync_retry_at - time.monotonic()
                    if retry_remaining > 0:
                        self._event_condition.wait(timeout=retry_remaining)
                        continue
                    resync_generation = self._event_generation
                else:
                    event_key, (deadline, action) = min(
                        self._pending_events.items(),
                        key=lambda item: item[1][0],
                    )
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        self._event_condition.wait(timeout=remaining)
                        continue
                    del self._pending_events[event_key]

            if resync_generation is not None:
                try:
                    self.synchronize_with_disk()
                except Exception as exc:  # noqa: BLE001 - watcher must keep serving later events
                    error_logger(f"[WATCH] disk reconciliation failed: {exc}")
                    with self._event_condition:
                        if self._stopped:
                            return
                        if self._resync_required:
                            self._resync_retry_at = (
                                time.monotonic() + self._resync_retry_delay
                            )
                            self._resync_retry_delay = min(
                                self._resync_retry_delay * 2,
                                self.RESYNC_RETRY_MAX_DELAY_SECONDS,
                            )
                        self._event_condition.notify_all()
                    continue
                with self._event_condition:
                    if self._stopped:
                        return
                    if self._event_generation == resync_generation:
                        self._resync_required = False
                    self._resync_retry_at = 0.0
                    self._resync_retry_delay = self.RESYNC_RETRY_INITIAL_DELAY_SECONDS
                    self._event_condition.notify_all()
                continue

            try:
                action()
            except Exception as exc:  # noqa: BLE001 - one bad file must not kill the worker
                error_logger(f"[WATCH] failed to process {event_key}: {exc}")
                self._require_disk_resync()

    def cancel_timers(self):
        """Clear pending work without permanently stopping the handler."""
        with self._event_condition:
            self._pending_events.clear()
            self._resync_required = False
            self._resync_retry_at = 0.0
            self._resync_retry_delay = self.RESYNC_RETRY_INITIAL_DELAY_SECONDS
            self._event_condition.notify_all()

    def stop(self) -> None:
        """Reject new work, discard pending events, and join active work."""
        with self._event_condition:
            self._stopped = True
            self._pending_events.clear()
            self._resync_required = False
            self._resync_retry_at = 0.0
            self._resync_retry_delay = self.RESYNC_RETRY_INITIAL_DELAY_SECONDS
            worker = self._worker_thread
            self._event_condition.notify_all()

        if worker is not None and worker is not threading.current_thread():
            worker.join()

    def _update_imports_map_for_file(self, changed_path: Path):
        """Re-scan one file and return symbol/module names that may gain targets."""
        changed_str = str(changed_path.resolve())
        changed_names = {
            symbol for symbol, paths in self.imports_map.items() if changed_str in paths
        }
        for symbol in list(self.imports_map.keys()):
            old_list = self.imports_map[symbol]
            if changed_str in old_list:
                new_list = [p for p in old_list if p != changed_str]
                if new_list:
                    self.imports_map[symbol] = new_list
                else:
                    del self.imports_map[symbol]
        if changed_path.exists():
            new_map = self.graph_builder.pre_scan_imports([changed_path])
            changed_names.update(new_map)
            for symbol, paths in new_map.items():
                if symbol not in self.imports_map:
                    self.imports_map[symbol] = []
                self.imports_map[symbol].extend(paths)

        # A Module node can exist without a resolvable CALLS edge. Include
        # importers by file/module name so adding a previously missing module
        # also retries their unresolved calls.
        try:
            relative_path = changed_path.resolve().relative_to(self.repo_path)
        except ValueError:
            relative_path = changed_path
        module_path = relative_path.as_posix()
        source_extensions = sorted(
            set(self.graph_builder.parsers) | {".d.ts"}, key=len, reverse=True
        )
        for extension in source_extensions:
            if module_path.endswith(extension):
                module_path = module_path[: -len(extension)]
                break
        if module_path.endswith("/__init__"):
            module_path = module_path[: -len("/__init__")]
        if module_path:
            changed_names.add(Path(module_path).name)
            changed_names.add(module_path.replace("/", "."))
            changed_names.add(module_path)
        return changed_names

    def _incremental_parse_workers(self) -> int:
        """Bounded worker count for re-parsing files affected by one watch event.

        Delegates to the indexing pipeline's helper so ``PARALLEL_WORKERS`` is
        resolved in one place, including the cap of 32. The fallback stays at 4:
        the watcher runs during editing, and should not take a full-index thread
        pool on every save.
        """
        from ..tools.indexing.pipeline import (
            WATCHER_DEFAULT_PARALLEL_WORKERS,
            get_parallel_workers,
        )

        return get_parallel_workers(default=WATCHER_DEFAULT_PARALLEL_WORKERS)

    def _handle_modification(self, event_path_str: str):
        """Incremental update: re-parse and re-link only the changed file and its neighbours."""
        # Serialised: concurrent handlers previously did read-modify-write on
        # the shared imports_map (lost updates) and interleaved
        # delete_file_from_graph / add_file_to_graph /
        # delete_outgoing_calls_from_files for overlapping caller sets, so one
        # could delete edges another had just created.
        with self._update_lock:
            self._handle_modification_locked(event_path_str)

    def _handle_modification_locked(self, event_path_str: str):
        info_logger(f"File change detected (incremental update): {event_path_str}")
        changed_path = Path(event_path_str)
        if self._should_ignore(changed_path):
            debug_log(f"Ignored watcher update based on .cgcignore: {changed_path}")
            return

        changed_path_str = changed_path.resolve().as_posix()
        supported_extensions = self.graph_builder.parsers.keys()

        caller_paths = {
            p
            for p in self.graph_builder.get_caller_file_paths(changed_path_str)
            if p and not self._should_ignore(p)
        }
        inheritor_paths = {
            p
            for p in self.graph_builder.get_inheritance_neighbor_paths(changed_path_str)
            if p and not self._should_ignore(p)
        }
        changed_import_names = self._update_imports_map_for_file(changed_path)
        importer_paths = {
            p
            for p in self.graph_builder.get_importer_file_paths(
                changed_import_names, self.repo_path, changed_path
            )
            if p and p != changed_path_str and not self._should_ignore(p)
        }
        affected_paths = {changed_path_str} | caller_paths | inheritor_paths | importer_paths
        info_logger(
            f"[INCREMENTAL] affected={len(affected_paths)} files "
            f"(callers={len(caller_paths)}, inheritors={len(inheritor_paths)}, "
            f"importers={len(importer_paths)})"
        )

        self.graph_builder.update_file_in_graph(changed_path, self.repo_path, self.imports_map)

        # Every file in affected_paths is re-parsed below and fed back into
        # link_function_calls, so every one of them needs its outgoing CALLS
        # cleared first. Clearing only caller_paths left the inheritance-only
        # neighbours to have their edges re-created on top of the existing ones
        # — and on Neo4j/Nornic the writer uses CREATE, not MERGE, so duplicate
        # CALLS multiplied on every save. (FalkorDB and Kùzu use MERGE, which is
        # why this never showed up there.) The changed file itself is excluded:
        # update_file_in_graph above already deleted and rebuilt it.
        other_callers = list(affected_paths - {changed_path_str})
        other_inheritors = list(inheritor_paths)
        if other_callers:
            self.graph_builder.delete_outgoing_calls_from_files(other_callers)
        if other_inheritors:
            self.graph_builder.delete_inherits_for_files(other_inheritors)

        subset_file_data = []
        for path_str in affected_paths:
            p = Path(path_str)
            if p.exists() and p.suffix in supported_extensions and not self._should_ignore(p):
                parsed = self.graph_builder.parse_file(self.repo_path, p)
                if "error" not in parsed:
                    subset_file_data.append(parsed)

        file_class_lookup = self.graph_builder.get_repo_class_lookup(self.repo_path)

        info_logger(f"[INCREMENTAL] Re-linking {len(subset_file_data)} files...")
        self.graph_builder.link_function_calls(
            subset_file_data, self.imports_map, file_class_lookup
        )
        self.graph_builder.link_inheritance(subset_file_data, self.imports_map)

        try:
            from codegraphcontext.cli.config_manager import get_config_value as _gcv
            _vector_enabled = (_gcv("ENABLE_VECTOR_RESOLVE") or "false").lower() == "true"
            _inherit_enabled = (_gcv("ENABLE_INHERIT_RESOLVE") or "false").lower() == "true"
        except Exception as _cfg_e:
            warning_logger(f"[PHASE4/5] Could not read config flags: {_cfg_e}")
            _vector_enabled = False
            _inherit_enabled = False

        if _vector_enabled:
            # Probe once per handler: with no backend installed, the old code
            # re-attempted (and re-failed) the import on every file event and
            # only said so at a suppressed log level (#1597).
            if not hasattr(self, "_embed_backend_ok"):
                from codegraphcontext.tools.indexing.embeddings import probe_embedding_backend
                self._embed_backend_ok, _detail = probe_embedding_backend()
                if not self._embed_backend_ok:
                    error_logger(
                        f"[EMBED] ENABLE_VECTOR_RESOLVE=true but incremental embeddings "
                        f"cannot run: {_detail} (reported once; further file events skip this)"
                    )
            if self._embed_backend_ok:
                try:
                    from codegraphcontext.tools.indexing.embeddings import EmbeddingPipeline
                    embed_pipeline = EmbeddingPipeline(self.graph_builder.driver)
                    embed_pipeline.invalidate_for_file(changed_path_str)
                    embed_pipeline.run(str(self.repo_path))
                    info_logger(f"[EMBED] Incremental embedding complete for {changed_path_str}")
                except Exception as _e:
                    warning_logger(f"[EMBED] Incremental embedding failed: {_e}")

        if _inherit_enabled:
            try:
                from codegraphcontext.tools.indexing.resolution.post_resolution import run_inheritance_reresolve
                _vector_resolver = None
                if _vector_enabled:
                    try:
                        from codegraphcontext.tools.indexing.vector_resolver import VectorResolver
                        _vector_resolver = VectorResolver(self.graph_builder.driver)
                    except Exception as _ve:
                        warning_logger(f"[VECTOR] Resolver unavailable for watcher: {_ve}")
                n_improved = run_inheritance_reresolve(
                    self.graph_builder.driver, str(self.repo_path), _vector_resolver
                )
                info_logger(f"[INHERIT-RESOLVE] Incremental: {n_improved} edges improved")
            except Exception as _e:
                warning_logger(f"[INHERIT-RESOLVE] Incremental failed: {_e}")

        info_logger(f"[INCREMENTAL] Done. Graph refresh for {event_path_str} complete! ✅")

    def on_created(self, event):
        if not event.is_directory and self._is_supported_event_file(event.src_path):
            self._debounce(event.src_path, lambda: self._handle_modification(event.src_path))

    def on_modified(self, event):
        if not event.is_directory and self._is_supported_event_file(event.src_path):
            self._debounce(event.src_path, lambda: self._handle_modification(event.src_path))

    def on_deleted(self, event):
        if not event.is_directory and self._is_supported_event_path(event.src_path):
            self._debounce(event.src_path, lambda: self._handle_modification(event.src_path))

    def on_moved(self, event):
        if event.is_directory:
            return
        # Both endpoints matter. Only dest_path was handled, so the node for
        # the *old* path and all of its symbols stayed in the graph forever:
        # every rename duplicated every symbol in the file, and a normal
        # refactoring session or a `git checkout` between branches accumulated
        # them indefinitely until find_callers started returning dead paths.
        src_path = getattr(event, "src_path", None)
        dest_path = getattr(event, "dest_path", None)
        source_supported = src_path and self._is_supported_event_path(src_path)
        destination_supported = dest_path and self._is_supported_event_path(dest_path)

        if source_supported and destination_supported:
            # Add the destination first so removing the source can relink its
            # callers against the moved definitions in the same serial job.
            # Use a move-specific key so a follow-up modify event for the
            # destination cannot replace the source cleanup action.
            move_key = (Path(src_path).resolve().as_posix(), Path(dest_path).resolve().as_posix())
            self._debounce(
                dest_path,
                lambda: self._handle_move(src_path, dest_path),
                coalesce_key=move_key,
            )
        elif source_supported:
            self._debounce(src_path, lambda: self._handle_removal(src_path))
        elif destination_supported:
            self._debounce(dest_path, lambda: self._handle_modification(dest_path))

    def _handle_move(self, src_path: str, dest_path: str) -> None:
        """Index the moved file before removing its old path and relinking callers."""
        with self._update_lock:
            self._handle_modification(dest_path)
            self._handle_removal(src_path)

    def _handle_removal(self, path_str):
        """Remove a vanished path and relink files that referenced its symbols."""
        try:
            # The ordinary incremental path queries incoming callers before
            # deleting the missing file, then reparses and relinks them.
            # Deleting only the source node left those callers disconnected
            # after a rename or branch switch.
            self._handle_modification(path_str)
            info_logger(f"[WATCH] reconciled vanished file: {path_str}")
        except Exception as exc:  # noqa: BLE001 - a watcher must not die on one file
            error_logger(f"[WATCH] failed to remove {path_str}: {exc}")
            self._require_disk_resync()


class CodeWatcher:
    def __init__(
        self,
        graph_builder: "GraphBuilder",
        job_manager="JobManager",
        use_polling: typing.Optional[bool] = None,
    ):
        self.graph_builder = graph_builder
        observer_cls = PollingObserver if should_use_polling_observer(use_polling) else Observer
        self.observer = observer_cls()

        self.watched_paths = set()
        self.watches = {}
        self.handlers = {}

    def watch_directory(
        self,
        path: str,
        perform_initial_scan: bool = True,
        cgcignore_path: str = None,
        sync_on_start: bool = False,
    ):
        path_obj = Path(path).resolve()
        path_str = str(path_obj)

        if path_str in self.watched_paths:
            return {"message": "Already watching"}

        handler = RepositoryEventHandler(
            self.graph_builder,
            path_obj,
            perform_initial_scan=perform_initial_scan,
            sync_on_start=sync_on_start,
            cgcignore_path=cgcignore_path,
        )

        watch = self.observer.schedule(handler, path_str, recursive=True)

        self.watches[path_str] = watch
        self.handlers[path_str] = handler
        self.watched_paths.add(path_str)

        return {"message": f"Watching {path_str}"}

    def unwatch_directory(self, path: str):
        path_str = str(Path(path).resolve())

        watch = self.watches.pop(path_str, None)
        handler = self.handlers.pop(path_str, None)
        try:
            if watch:
                self.observer.unschedule(watch)
        finally:
            try:
                if handler:
                    handler.stop()
            finally:
                self.watched_paths.discard(path_str)

        return {"message": f"Stopped watching {path_str}"}

    def list_watched_paths(self):
        return list(self.watched_paths)

    def start(self):
        if not self.observer.is_alive():
            self.observer.start()

    def stop(self):
        observer_was_alive = self.observer.is_alive()
        if observer_was_alive:
            self.observer.stop()

        for h in self.handlers.values():
            h.stop()
        self.handlers.clear()

        # A stopped watchdog Observer cannot be restarted, so the watch state
        # must not survive it: a stale entry makes watch_directory answer
        # "Already watching" for a dead observer and the graph silently goes
        # stale (#1519).
        self.watched_paths.clear()
        self.watches.clear()

        if observer_was_alive:
            self.observer.join()
