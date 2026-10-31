"""
This module implements the live file-watching functionality using the `watchdog` library.
It observes directories for changes and triggers updates to the code graph.
"""

import os
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
import typing

from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler

if typing.TYPE_CHECKING:
    from pathspec import PathSpec
    from ..tools.graph_builder import GraphBuilder

from .cgcignore import (
    CGCIgnoreMatcher,
    build_ignore_spec,
    find_cgcignore,
    find_gitignore,
    parse_cgcignore_lines,
)
from ..tools.indexing.constants import DEFAULT_IGNORE_PATTERNS
from ..cli.config_manager import DEFAULT_CGCIGNORE_PATTERNS, get_config_value
from ..utils.debug_log import debug_log, info_logger, error_logger, warning_logger
from ..utils.git_utils import (
    get_repo_branch_name,
    get_repo_commit_hash,
    has_unborn_git_head,
)
from ..utils.branch_identity import repository_identity


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
        generation_db_path: str = None,
        generation_identity: dict = None,
    ):
        super().__init__()
        self.graph_builder = graph_builder
        self.repo_path = repo_path.resolve()
        self._bound_repository_identity = (
            repository_identity(self.repo_path)
            if generation_db_path and generation_identity
            else None
        )
        self._bound_identity_key = (
            self._bound_repository_identity.storage_key
            if self._bound_repository_identity is not None
            else None
        )
        if (
            generation_identity
            and generation_identity.get("kind") in {"git", "non-git"}
            and generation_identity.get("storage_key") != self._bound_identity_key
        ):
            raise RuntimeError(
                "Watcher database identity changed while resolving the context; "
                "reopen the watcher for the current branch/worktree."
            )
        self._generation_db_path = generation_db_path
        self._generation_identity = generation_identity
        self._generation_update_depth = 0
        self._generation_update_failed = False
        self._identity_mismatch_reported = False
        self._watch_update_lock = threading.RLock()
        self.git_metadata_dirs = self._discover_git_metadata_dirs()
        self._git_head_snapshot = self._read_git_head_snapshot()
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
        self._provided_ignore_spec = ignore_spec is not None
        self.cgcignore_path = cgcignore_path
        self.configured_ignore_path = None
        self.explicit_ignore_path = None
        self.local_cgcignore_path = None
        self.local_gitignore_path = None
        self.local_ignore_targets = ()
        if cgcignore_path:
            explicit_path = Path(cgcignore_path)
            if not explicit_path.is_absolute():
                explicit_path = self.repo_path / explicit_path
            self.configured_ignore_path = Path(os.path.abspath(explicit_path))
            self._refresh_explicit_ignore_path()
        self._load_ignore_spec(cgcignore_path)

        self.all_file_data = []
        self.imports_map = {}

        if sync_on_start:
            self.synchronize_with_disk()
        elif perform_initial_scan:
            self._initial_scan()

    def _discover_git_metadata_dirs(self) -> tuple[Path, ...]:
        """Return this worktree's Git directory and shared common directory."""
        try:
            git_dir = Path(
                subprocess.check_output(
                    ["git", "rev-parse", "--absolute-git-dir"],
                    cwd=self.repo_path,
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip()
            ).resolve()
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return ()

        common_dir = git_dir
        commondir_file = git_dir / "commondir"
        if commondir_file.is_file():
            try:
                common_path = Path(commondir_file.read_text(encoding="utf-8").strip())
                common_dir = (
                    common_path if common_path.is_absolute() else git_dir / common_path
                ).resolve()
            except (OSError, RuntimeError):
                common_dir = git_dir
        return tuple(dict.fromkeys((git_dir, common_dir)))

    def _is_git_refs_directory_event(self, path: str | Path | None) -> bool:
        """Whether *path* is the shared refs directory watched for linked worktrees."""
        if not path:
            return False
        try:
            candidate = Path(os.path.abspath(path))
        except (OSError, RuntimeError):
            return False
        metadata_dirs = getattr(self, "git_metadata_dirs", ())
        return bool(metadata_dirs) and candidate == metadata_dirs[-1] / "refs"

    def _refresh_git_metadata_for_event(self, path: str | Path | None) -> bool:
        """Discover Git metadata when its entry is created or removed."""
        if not path:
            return False
        try:
            candidate = Path(os.path.abspath(path))
            git_entry = self.repo_path / ".git"
            try:
                candidate.relative_to(git_entry)
                in_repository_git_entry = True
            except ValueError:
                in_repository_git_entry = False
            cached_git_dirs = getattr(self, "git_metadata_dirs", ())
            refs_directory_event = bool(cached_git_dirs) and candidate == cached_git_dirs[-1] / "refs"
            removed_git_dir = False
            for git_dir in cached_git_dirs:
                if candidate != git_dir:
                    continue
                try:
                    git_dir.stat()
                except FileNotFoundError:
                    removed_git_dir = True
                except OSError:
                    pass
                break
        except (OSError, RuntimeError):
            return False
        if not in_repository_git_entry and not removed_git_dir and not refs_directory_event:
            return False

        # Once Git metadata has been discovered, child events use the cached
        # directories below. Re-discover only for the .git entry itself or a
        # removed cached metadata directory; while the repository is not
        # initialized, any first child event can reveal initialization.
        if candidate != git_entry and cached_git_dirs and not removed_git_dir and not refs_directory_event:
            return False

        with self._watch_update_lock:
            if self._stopped:
                return False
            previous_dirs = self.git_metadata_dirs
            previous_snapshot = self._git_head_snapshot
            if refs_directory_event and not candidate.is_dir():
                # A missing refs root makes Git commands fail until it is
                # recreated. Keep the known admin dirs/snapshot so the create
                # event can restore the recursive watch without losing scope.
                current_dirs = previous_dirs
                current_snapshot = previous_snapshot
            else:
                current_dirs = self._discover_git_metadata_dirs()
                current_snapshot = self._read_git_head_snapshot()
                if refs_directory_event and not current_dirs:
                    current_dirs = previous_dirs
                    current_snapshot = previous_snapshot
            state_changed = current_dirs != previous_dirs or current_snapshot != previous_snapshot

            self.git_metadata_dirs = current_dirs
            self._git_head_snapshot = current_snapshot
            refresh_watches = getattr(self, "_refresh_git_metadata_watches", None)
        if callable(refresh_watches) and (state_changed or refs_directory_event):
            refresh_watches()
        return state_changed

    def _load_ignore_spec(self, cgcignore_path: str = None, *, force: bool = False) -> None:
        if self.ignore_spec is not None and (not force or getattr(self, "_provided_ignore_spec", False)):
            return

        def build_spec(*, raise_on_gitignore_error: bool, raise_on_cgcignore_error: bool = True):
            return build_ignore_spec(
                ignore_root=self.ignore_root,
                default_patterns=DEFAULT_IGNORE_PATTERNS,
                explicit_path=cgcignore_path,
                missing_explicit_defaults=parse_cgcignore_lines(DEFAULT_CGCIGNORE_PATTERNS.splitlines()),
                raise_on_gitignore_error=raise_on_gitignore_error,
                raise_on_cgcignore_error=raise_on_cgcignore_error,
            )

        try:
            self.ignore_spec, resolved = build_spec(raise_on_gitignore_error=True)
        except (OSError, UnicodeError) as e:
            # Keep the last successful rules so an I/O error cannot broaden
            # a later repository scan to include previously ignored files.
            warning_logger(f"Could not load ignore rules: {e}")
            if self.ignore_spec is not None or self._provided_ignore_spec:
                return
            try:
                # At startup there is no previous matcher to preserve. Build a
                # safe default/.cgcignore matcher while skipping only unreadable
                # .gitignore rules, rather than accepting every file.
                self.ignore_spec, resolved = build_spec(
                    raise_on_gitignore_error=False,
                    raise_on_cgcignore_error=False,
                )
            except (OSError, UnicodeError) as fallback_error:
                warning_logger(f"Could not load fallback ignore rules: {fallback_error}")
                default_patterns = DEFAULT_IGNORE_PATTERNS + parse_cgcignore_lines(
                    DEFAULT_CGCIGNORE_PATTERNS.splitlines()
                )
                self.ignore_spec = CGCIgnoreMatcher(default_patterns, self.ignore_root)
                resolved = find_cgcignore(self.ignore_root, explicit_path=cgcignore_path)

        self.local_cgcignore_path = resolved
        self.local_gitignore_path = find_gitignore(self.ignore_root)
        self._refresh_ignore_metadata_targets()
        if resolved:
            debug_log(f"Watcher using ignore file: {resolved}")

    def _refresh_explicit_ignore_path(self) -> None:
        """Refresh the configured file's current symlink target, if any."""
        configured_path = getattr(self, "configured_ignore_path", None)
        if configured_path is None:
            return
        try:
            self.explicit_ignore_path = configured_path.resolve()
        except (OSError, RuntimeError):
            self.explicit_ignore_path = configured_path

    def _refresh_ignore_metadata_targets(self) -> None:
        """Refresh resolved targets for local and explicitly configured rules."""
        self._refresh_explicit_ignore_path()
        targets = []
        for path in (
            getattr(self, "local_cgcignore_path", None),
            getattr(self, "local_gitignore_path", None),
        ):
            if path is None:
                continue
            try:
                targets.append(path.resolve())
            except (OSError, RuntimeError):
                targets.append(path)
        self.local_ignore_targets = tuple(dict.fromkeys(targets))

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
        try:
            path_obj.resolve().relative_to(self.repo_path)
        except (OSError, RuntimeError, ValueError):
            return False

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

    @contextmanager
    def _generation_update(self):
        """Mark the index incomplete for the full outermost watcher mutation."""
        db_path = getattr(self, "_generation_db_path", None)
        identity = getattr(self, "_generation_identity", None)
        if not db_path or not identity:
            yield
            return

        from .index_generation import generation_update

        depth = getattr(self, "_generation_update_depth", 0)
        if depth == 0:
            self._generation_update_failed = False
            if identity.get("kind") == "git":
                current_identity = repository_identity(self.repo_path).as_dict()
                if current_identity.get("storage_key") != self._bound_identity_key:
                    raise RuntimeError(
                        "Watcher repository identity changed before the graph update; "
                        "reopen the watcher for the current branch/worktree."
                    )
                identity = current_identity
                self._generation_identity = current_identity
            generation_id = (
                f"{identity.get('storage_key', '')}:"
                f"{identity.get('commit') or 'uncommitted'}"
            )
            marker_update = generation_update(
                db_path, identity, generation_id=generation_id
            )
            self._active_generation_marker_update = marker_update
            self._active_generation_update_state = marker_update.__enter__()
        self._generation_update_depth = depth + 1
        try:
            yield
        except BaseException:
            self._generation_update_failed = True
            raise
        finally:
            self._generation_update_depth -= 1
            if self._generation_update_depth == 0:
                if self._generation_update_failed:
                    self._active_generation_update_state.leave_incomplete()
                self._active_generation_marker_update.__exit__(None, None, None)

    def _initial_scan(self):
        with self._update_lock, self._generation_update():
            return self._initial_scan_locked()

    def _initial_scan_locked(self):
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
        if not self._identity_matches_bound_index():
            return
        with self._update_lock, self._generation_update():
            return self._synchronize_with_disk_locked()

    def _synchronize_with_disk_locked(self) -> None:
        if not self._identity_matches_bound_index():
            return
        info_logger(f"Syncing: {self.repo_path}")

        # Ignore files define which files belong in the graph. Re-read them
        # before reconciling the repository so newly ignored files are pruned
        # and newly included files are indexed with the same rules as a clean
        # build. A caller-supplied matcher remains authoritative.
        self._refresh_explicit_ignore_path()
        self._load_ignore_spec(self.cgcignore_path, force=True)
        refresh_watches = getattr(self, "_refresh_ignore_watches", None)
        if callable(refresh_watches):
            refresh_watches()

        current_files = self._iter_supported_files()
        current_paths = {p.resolve().as_posix() for p in current_files}
        # Normalize stored paths lexically only: older Windows indexes stored
        # backslash paths, but resolving a foreign-platform path against the
        # local filesystem would prefix cwd and mark every file stale.
        indexed = {p.replace("\\", "/") for p in self.graph_builder.get_repo_file_paths(self.repo_path)}

        self.imports_map = self.graph_builder.pre_scan_imports(current_files)

        stale_paths = sorted(indexed - current_paths)
        # update_file_in_graph replaces every current file, which detaches
        # incoming CALLS edges as well as edges for files removed from disk.
        # Save outside-repository callers before either kind of replacement.
        replaced_paths = sorted(set(stale_paths) | current_paths)
        callers_of_replaced_files = self.graph_builder.get_caller_file_paths_for_files(replaced_paths)

        for stale in stale_paths:
            self.graph_builder.delete_file_from_graph(stale)

        refreshed = []
        refreshed_paths: list[str] = []
        for p in current_files:
            fd = self.graph_builder.update_file_in_graph(p, self.repo_path, self.imports_map)
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

        self._relink_external_callers(caller_paths=callers_of_replaced_files)

        # A disk reconciliation may follow a branch/commit switch even when
        # the repository's files are otherwise identical. Keep Repository
        # metadata aligned with the state represented by the refreshed files.
        self.graph_builder.add_repository_to_graph(self.repo_path, is_dependency=None)
        self._git_head_snapshot = self._read_git_head_snapshot()
        info_logger("Sync complete")

    def _relink_external_callers(self, caller_paths: set[str]) -> None:
        """Refresh callers outside this repository whose callees were removed."""
        callers_by_repo: dict[Path, set[str]] = {}
        for caller_path in caller_paths:
            path = Path(caller_path)
            if not path.is_file():
                continue
            try:
                path.resolve().relative_to(self.repo_path)
                continue
            except (OSError, RuntimeError, ValueError):
                pass

            repo_path = self.graph_builder.get_repository_path_for_file(caller_path)
            if repo_path:
                callers_by_repo.setdefault(Path(repo_path), set()).add(path.resolve().as_posix())

        for repo_path, affected_paths in callers_by_repo.items():
            if not repo_path.is_dir():
                continue
            repo_files = sorted(
                Path(path) for path in self.graph_builder.get_repo_file_paths(repo_path) if Path(path).is_file()
            )
            imports_map = self.graph_builder.pre_scan_imports(repo_files)
            refreshed_callers = []
            refreshed_paths = []
            for caller_path in sorted(affected_paths):
                parsed = self.graph_builder.parse_file(repo_path, Path(caller_path))
                if parsed and "error" not in parsed:
                    refreshed_callers.append(parsed)
                    refreshed_paths.append(caller_path)
            if not refreshed_callers:
                continue

            self.graph_builder.delete_outgoing_calls_from_files(refreshed_paths)
            self.graph_builder.link_function_calls(
                refreshed_callers,
                imports_map,
                self.graph_builder.get_repo_class_lookup(repo_path),
            )

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

            self._ensure_worker_locked()
            self._event_condition.notify()

    def _ensure_worker_locked(self) -> None:
        if self._worker_thread is None:
            self._worker_thread = threading.Thread(
                target=self._process_pending_events,
                name=f"cgc-watcher-{id(self):x}",
                daemon=True,
            )
            self._worker_thread.start()

    def _require_disk_resync(self) -> None:
        """Collapse pending actions and reconcile disk after an event failure."""
        with self._event_condition:
            if self._stopped or not self._identity_matches_bound_index():
                return
            self._event_generation += 1
            self._pending_events.clear()
            self._resync_required = True
            self._resync_retry_at = 0.0
            self._resync_retry_delay = self.RESYNC_RETRY_INITIAL_DELAY_SECONDS
            self._ensure_worker_locked()
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
                if not self._identity_matches_bound_index():
                    continue
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
                if not self._identity_matches_bound_index():
                    continue
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
        if not self._identity_matches_bound_index():
            return
        # Serialised: concurrent handlers previously did read-modify-write on
        # the shared imports_map (lost updates) and interleaved
        # delete_file_from_graph / add_file_to_graph /
        # delete_outgoing_calls_from_files for overlapping caller sets, so one
        # could delete edges another had just created.
        with self._update_lock:
            # The lock may wait behind another event while Git switches
            # branches; validate again at the actual mutation boundary.
            if not self._identity_matches_bound_index():
                return
            with self._generation_update():
                self._handle_modification_locked(event_path_str)

    def _identity_matches_bound_index(self) -> bool:
        """Reject events after this checkout switches to another index identity."""
        # A few lightweight callers construct handlers with __new__ and seed
        # only the fields under test; production handlers always bind in init.
        bound_key = getattr(self, "_bound_identity_key", None)
        if bound_key is None:
            return True
        if getattr(self, "_bound_repository_identity", None) is None:
            return True
        matches = repository_identity(self.repo_path).storage_key == bound_key
        if matches:
            return True
        if not self._identity_mismatch_reported:
            warning_logger(
                "Watcher paused after branch/worktree identity changed; restart CGC "
                "to select the matching isolated index."
            )
            self._identity_mismatch_reported = True
        return False

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
        if self._refresh_git_metadata_for_event(getattr(event, "src_path", None)):
            self._require_disk_resync()
            return
        if self._is_git_metadata_path(getattr(event, "src_path", None)):
            self._debounce(event.src_path, self._require_disk_resync)
            return
        if event.is_directory and self._is_external_ignore_parent_directory(event.src_path):
            self._queue_ignore_metadata_resync(event.src_path)
            return
        if not event.is_directory and self._is_ignore_metadata_path(event.src_path):
            self._queue_ignore_metadata_resync(event.src_path)
            return
        if not event.is_directory and self._is_supported_event_file(event.src_path):
            self._debounce(event.src_path, lambda: self._handle_modification(event.src_path))

    def on_modified(self, event):
        if self._refresh_git_metadata_for_event(getattr(event, "src_path", None)):
            self._require_disk_resync()
            return
        if self._is_git_metadata_path(getattr(event, "src_path", None)):
            self._debounce(event.src_path, self._require_disk_resync)
            return
        if not event.is_directory and self._is_ignore_metadata_path(event.src_path):
            self._queue_ignore_metadata_resync(event.src_path)
            return
        if not event.is_directory and self._is_supported_event_file(event.src_path):
            self._debounce(event.src_path, lambda: self._handle_modification(event.src_path))

    def on_deleted(self, event):
        if self._refresh_git_metadata_for_event(getattr(event, "src_path", None)):
            self._require_disk_resync()
            return
        if self._is_git_refs_directory_event(getattr(event, "src_path", None)):
            return
        if self._is_git_metadata_path(getattr(event, "src_path", None)):
            self._debounce(event.src_path, self._require_disk_resync)
            return
        if event.is_directory:
            deleted_path = Path(getattr(event, "src_path", ""))
            try:
                deleted_path.resolve().relative_to(self.repo_path)
            except (OSError, RuntimeError, ValueError):
                if self._is_external_ignore_parent_directory(deleted_path):
                    self._require_disk_resync()
                return
            if not self._should_ignore(deleted_path):
                self._require_disk_resync()
            return
        if not event.is_directory and self._is_ignore_metadata_path(event.src_path):
            self._queue_ignore_metadata_resync(event.src_path)
            return
        if not event.is_directory and self._is_supported_event_path(event.src_path):
            self._debounce(event.src_path, lambda: self._handle_modification(event.src_path))

    def on_moved(self, event):
        src_path = getattr(event, "src_path", None)
        dest_path = getattr(event, "dest_path", None)
        if self._refresh_git_metadata_for_event(src_path) or self._refresh_git_metadata_for_event(dest_path):
            self._require_disk_resync()
            return
        if self._is_git_metadata_path(src_path) or self._is_git_metadata_path(dest_path):
            metadata_path = src_path or dest_path
            self._debounce(
                metadata_path,
                self._require_disk_resync,
                coalesce_key=(src_path, dest_path),
            )
            return
        if event.is_directory:
            endpoints = [getattr(event, name, None) for name in ("src_path", "dest_path")]
            for path in endpoints:
                if not path:
                    continue
                try:
                    Path(path).resolve().relative_to(self.repo_path)
                except (OSError, RuntimeError, ValueError):
                    if self._is_external_ignore_parent_directory(path):
                        self._require_disk_resync()
                        break
                    continue
                if not self._should_ignore(path):
                    self._require_disk_resync()
                    break
            return
        ignore_metadata_path = next(
            (
                path
                for path in (src_path, dest_path)
                if self._is_ignore_metadata_path(path)
            ),
            None,
        )
        if ignore_metadata_path:
            self._queue_ignore_metadata_resync(
                ignore_metadata_path, coalesce_key=(src_path, dest_path)
            )
            return
        # Both endpoints matter. Only dest_path was handled, so the node for
        # the *old* path and all of its symbols stayed in the graph forever:
        # every rename duplicated every symbol in the file, and a normal
        # refactoring session or a `git checkout` between branches accumulated
        # them indefinitely until find_callers started returning dead paths.
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

    def _is_git_metadata_path(self, path: str | Path | None) -> bool:
        """Whether a Git event changed the commit indexed for this repository."""
        if not path:
            return False
        try:
            candidate = Path(path).resolve()
        except (OSError, RuntimeError):
            return False
        for git_dir in getattr(self, "git_metadata_dirs", ()):
            try:
                relative = candidate.relative_to(git_dir)
            except (OSError, RuntimeError, ValueError):
                continue
            parts = relative.parts
            if (
                not parts
                or parts in (("HEAD",), ("packed-refs",))
                or parts[:2] in (("refs", "heads"), ("reftable", "tables.list"))
            ):
                current_snapshot = self._read_git_head_snapshot()
                if current_snapshot is None:
                    changed = self._git_head_snapshot is not None
                    self._git_head_snapshot = None
                    return changed
                changed = current_snapshot != self._git_head_snapshot
                self._git_head_snapshot = current_snapshot
                return changed
        return False

    def _read_git_head_snapshot(self):
        branch_name = get_repo_branch_name(self.repo_path)
        commit_hash = get_repo_commit_hash(self.repo_path)
        if commit_hash:
            return ("commit", commit_hash, branch_name)
        if has_unborn_git_head(self.repo_path):
            return ("unborn", branch_name)
        return None

    def _queue_ignore_metadata_resync(
        self, path: str | Path, *, coalesce_key=None
    ) -> None:
        """Refresh the explicit file target and watches before reconciliation."""
        self._refresh_ignore_metadata_targets()
        refresh_watches = getattr(self, "_refresh_ignore_watches", None)
        if callable(refresh_watches):
            refresh_watches()
        self._debounce(
            path, self._require_disk_resync, coalesce_key=coalesce_key
        )

    def _is_ignore_metadata_path(self, path: str | Path | None) -> bool:
        """Whether a path can change this repository's ignore rules."""
        if not path:
            return False
        try:
            configured_candidate = Path(os.path.abspath(path))
            candidate = Path(path).resolve()
        except (OSError, RuntimeError):
            return False
        local_paths = (
            getattr(self, "local_cgcignore_path", None),
            getattr(self, "local_gitignore_path", None),
        )
        if configured_candidate in local_paths:
            return True
        configured_path = getattr(self, "configured_ignore_path", None)
        if configured_path is not None and configured_candidate == configured_path:
            return True
        explicit_path = getattr(self, "explicit_ignore_path", None)
        local_targets = getattr(self, "local_ignore_targets", ())
        if (explicit_path is not None and candidate == explicit_path) or candidate in local_targets:
            return True
        if configured_candidate.name not in {".cgcignore", ".gitignore"}:
            return False
        try:
            configured_candidate.relative_to(self.repo_path)
        except ValueError:
            return False
        return True

    def _is_external_ignore_parent_directory(self, path: str | Path | None) -> bool:
        """Whether *path* is an external ignore parent or its ancestor."""
        if not path:
            return False
        try:
            candidate = Path(path).resolve()
        except (OSError, RuntimeError):
            return False

        ignore_paths = (
            getattr(self, "configured_ignore_path", None),
            getattr(self, "explicit_ignore_path", None),
            getattr(self, "local_cgcignore_path", None),
            getattr(self, "local_gitignore_path", None),
            *getattr(self, "local_ignore_targets", ()),
        )
        for ignore_path in ignore_paths:
            if ignore_path is None:
                continue
            try:
                parent = Path(ignore_path).parent.resolve()
                parent.relative_to(self.repo_path)
            except ValueError:
                if parent == candidate or parent.is_relative_to(candidate):
                    return True
            except (OSError, RuntimeError):
                continue
        return False

    def _handle_move(self, src_path: str, dest_path: str) -> None:
        """Index the moved file before removing its old path and relinking callers."""
        with self._update_lock, self._generation_update():
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

        self._watch_lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._stopping = False
        self.watched_paths = set()
        self.watches = {}
        self.handlers = {}

    def _unschedule_if_unshared(self, handler, watch) -> None:
        """Detach one handler and stop a Watchdog registration after its final owner."""
        with self._watch_lock:
            self.observer.remove_handler_for_watch(handler, watch)
            for owner_watches in self.watches.values():
                if isinstance(owner_watches, (list, tuple)):
                    if watch in owner_watches:
                        return
                elif owner_watches == watch:
                    return
            self.observer.unschedule(watch)

    def watch_directory(
        self,
        path: str,
        perform_initial_scan: bool = True,
        cgcignore_path: str = None,
        sync_on_start: bool = False,
        generation_db_path: str = None,
        generation_identity: dict = None,
    ):
        path_obj = Path(path).resolve()
        path_str = str(path_obj)

        with self._watch_lock:
            if self._stopping:
                return {"message": "Watcher is stopping"}
            if path_str in self.watched_paths:
                return {"message": "Already watching"}

        generation_kwargs = {}
        if generation_db_path and generation_identity:
            generation_kwargs = {
                "generation_db_path": generation_db_path,
                "generation_identity": generation_identity,
            }
        handler = RepositoryEventHandler(
            self.graph_builder,
            path_obj,
            perform_initial_scan=perform_initial_scan,
            sync_on_start=sync_on_start,
            cgcignore_path=cgcignore_path,
            **generation_kwargs,
        )

        return self._register_watch_handler(path_obj, path_str, handler)

    def _register_watch_handler(self, path_obj: Path, path_str: str, handler: RepositoryEventHandler):
        with self._watch_lock:
            if self._stopping:
                handler.stop()
                return {"message": "Watcher is stopping"}
            if path_str in self.watched_paths:
                handler.stop()
                return {"message": "Already watching"}
            return self._register_watch_handler_locked(path_obj, path_str, handler)

    def _register_watch_handler_locked(self, path_obj: Path, path_str: str, handler: RepositoryEventHandler):
        watches = [self.observer.schedule(handler, path_str, recursive=True)]
        external_watches = {}

        def is_within(path: Path, parent: Path) -> bool:
            try:
                path.relative_to(parent)
                return True
            except ValueError:
                return False

        def refresh_external_watches() -> None:
            with self._watch_lock:
                refresh_external_watches_locked()

        def refresh_external_watches_locked() -> None:
            with handler._watch_update_lock:
                if getattr(handler, "_stopped", False):
                    return

                desired_watches = set()
                git_metadata_dirs = getattr(handler, "git_metadata_dirs", ())
                if isinstance(git_metadata_dirs, (list, tuple)):
                    for metadata_dir in git_metadata_dirs:
                        if not is_within(metadata_dir, path_obj):
                            desired_watches.add((metadata_dir, False))

                    if git_metadata_dirs:
                        refs_dir = git_metadata_dirs[-1] / "refs"
                        if refs_dir.is_dir() and not is_within(refs_dir, path_obj):
                            desired_watches.add((refs_dir, True))
                        reftable_dir = git_metadata_dirs[-1] / "reftable"
                        if reftable_dir.is_dir() and not is_within(reftable_dir, path_obj):
                            desired_watches.add((reftable_dir, False))

                ignore_paths = (
                    getattr(handler, "configured_ignore_path", None),
                    getattr(handler, "explicit_ignore_path", None),
                    getattr(handler, "local_cgcignore_path", None),
                    getattr(handler, "local_gitignore_path", None),
                    *getattr(handler, "local_ignore_targets", ()),
                )
                for ignore_path in ignore_paths:
                    if ignore_path is None:
                        continue
                    watch_path = Path(ignore_path).parent.resolve()
                    if is_within(watch_path, path_obj):
                        continue
                    while not watch_path.is_dir():
                        existing_parent = watch_path.parent
                        if existing_parent == watch_path:
                            break
                        watch_path = existing_parent
                    if watch_path.is_dir():
                        desired_watches.add((watch_path, False))

                for watch_key in set(external_watches) - desired_watches:
                    watch = external_watches.pop(watch_key)
                    watches.remove(watch)
                    self._unschedule_if_unshared(handler, watch)

                for watch_key in sorted(
                    desired_watches - set(external_watches),
                    key=lambda item: (str(item[0]), item[1]),
                ):
                    watch_path, recursive = watch_key
                    watch = self.observer.schedule(handler, str(watch_path), recursive=recursive)
                    external_watches[watch_key] = watch
                    watches.append(watch)

        handler._refresh_ignore_watches = refresh_external_watches
        handler._refresh_git_metadata_watches = refresh_external_watches
        refresh_external_watches()

        self.watches[path_str] = watches
        self.handlers[path_str] = handler
        self.watched_paths.add(path_str)

        return {"message": f"Watching {path_str}"}

    def unwatch_directory(self, path: str):
        path_str = str(Path(path).resolve())

        with self._watch_lock:
            handler = self.handlers.get(path_str)
        if handler:
            handler.stop()

        def release_watches() -> None:
            watch = self.watches.pop(path_str, None)
            registered_handler = self.handlers.pop(path_str, None)
            try:
                watch_list = watch if isinstance(watch, (list, tuple)) else [watch]
                for scheduled_watch in watch_list:
                    if scheduled_watch:
                        self._unschedule_if_unshared(registered_handler, scheduled_watch)
            finally:
                self.watched_paths.discard(path_str)

        if handler:
            with self._watch_lock:
                with handler._watch_update_lock:
                    release_watches()
        else:
            with self._watch_lock:
                release_watches()

        return {"message": f"Stopped watching {path_str}"}

    def list_watched_paths(self):
        return list(self.watched_paths)

    def start(self):
        with self._watch_lock:
            if not self._stopping and not self.observer.is_alive():
                self.observer.start()

    def stop(self):
        with self._stop_lock:
            with self._watch_lock:
                if self._stopping:
                    return
                self._stopping = True
                handlers = list(self.handlers.values())

            # Let active reconciliations finish their dynamic watch updates
            # before the Observer stops accepting registrations.
            for handler in handlers:
                handler.stop()

            observer_was_alive = self.observer.is_alive()
            with self._watch_lock:
                for handler in handlers:
                    handler._watch_update_lock.acquire()
                try:
                    if observer_was_alive:
                        self.observer.stop()

                    self.handlers.clear()
                    # A stopped watchdog Observer cannot be restarted, so the
                    # watch state must not survive it: a stale entry makes
                    # watch_directory answer "Already watching" for a dead
                    # observer and the graph silently goes stale (#1519).
                    self.watched_paths.clear()
                    self.watches.clear()
                finally:
                    for handler in reversed(handlers):
                        handler._watch_update_lock.release()

            if observer_was_alive:
                self.observer.join()
