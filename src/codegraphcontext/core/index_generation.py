"""Atomic completion markers for branch-scoped local index generations."""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Optional
from contextlib import asynccontextmanager, contextmanager


_LOCKS_GUARD = threading.Lock()
_MARKER_LOCKS: dict[str, threading.Lock] = {}


class GenerationUpdate:
    """Control whether an async marker-backed operation may be certified."""

    def __init__(self, *, preserve_complete: bool = False):
        self.complete_on_exit = preserve_complete

    def mark_complete(self) -> None:
        """Certify a successful full index or reconciliation operation."""
        self.complete_on_exit = True

    def leave_incomplete(self) -> None:
        self.complete_on_exit = False


def _acquire_marker_lock(db_path: str | Path):
    marker = generation_marker_path(db_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{marker}.lock")
    key = str(lock_path.resolve())
    with _LOCKS_GUARD:
        thread_lock = _MARKER_LOCKS.setdefault(key, threading.Lock())
    thread_lock.acquire()
    descriptor = None
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor, thread_lock
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        thread_lock.release()
        raise


def _release_marker_lock(lock) -> None:
    descriptor, thread_lock = lock
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
        thread_lock.release()


def _write_generation_state_unlocked(
    db_path: str | Path,
    identity: dict[str, Any],
    *,
    state: str,
    generation_id: Optional[str] = None,
) -> dict[str, Any]:
    if state not in {"building", "incomplete", "complete"}:
        raise ValueError("generation state must be 'building', 'incomplete', or 'complete'")
    document = {
        "format": "cgc-index-generation",
        "format_version": 1,
        "identity": dict(identity),
        "generation_id": generation_id or "",
        "state_id": uuid.uuid4().hex,
        "state": state,
        "complete": state == "complete",
        "current": state == "complete",
    }
    path = generation_marker_path(db_path)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return document


def _has_verified_identity(marker: Optional[dict[str, Any]], identity: dict[str, Any]) -> bool:
    return bool(
        marker
        and marker.get("identity") == identity
        and marker.get("state_id")
        and marker.get("state") == "complete"
        and marker.get("complete") is True
        and marker.get("current") is True
    )


def generation_marker_path(db_path: str | Path) -> Path:
    """Path to the sidecar state for a local DB directory/file."""
    return Path(f"{Path(db_path)}.cgc-generation.json")


def write_generation_state(
    db_path: str | Path,
    identity: dict[str, Any],
    *,
    state: str,
    generation_id: Optional[str] = None,
) -> dict[str, Any]:
    """Atomically publish a marker state under the per-database writer lock."""
    lock = _acquire_marker_lock(db_path)
    try:
        return _write_generation_state_unlocked(
            db_path, identity, state=state, generation_id=generation_id
        )
    finally:
        _release_marker_lock(lock)


@contextmanager
def generation_update(
    db_path: str | Path,
    identity: dict[str, Any],
    *,
    generation_id: Optional[str] = None,
):
    """Serialize a synchronous graph mutation and its marker transitions."""
    lock = _acquire_marker_lock(db_path)
    try:
        preserve_complete = _has_verified_identity(read_generation_state(db_path), identity)
        _write_generation_state_unlocked(
            db_path, identity, state="building", generation_id=generation_id
        )
        update = GenerationUpdate(preserve_complete=preserve_complete)
        try:
            yield update
        except BaseException:
            raise
        else:
            _write_generation_state_unlocked(
                db_path,
                identity,
                state="complete" if update.complete_on_exit else "incomplete",
                generation_id=generation_id,
            )
    finally:
        _release_marker_lock(lock)


@asynccontextmanager
async def async_generation_update(
    db_path: str | Path,
    identity: dict[str, Any],
    *,
    generation_id: Optional[str] = None,
):
    """Async counterpart that waits for the DB lock without blocking its loop."""
    import asyncio

    acquisition = asyncio.create_task(asyncio.to_thread(_acquire_marker_lock, db_path))
    try:
        lock = await asyncio.shield(acquisition)
    except asyncio.CancelledError:
        # A cancelled waiter must release a lock its worker thread may still
        # be acquiring; otherwise later index jobs can block forever.
        lock = await acquisition
        _release_marker_lock(lock)
        raise
    try:
        preserve_complete = _has_verified_identity(read_generation_state(db_path), identity)
        _write_generation_state_unlocked(
            db_path, identity, state="building", generation_id=generation_id
        )
        update = GenerationUpdate(preserve_complete=preserve_complete)
        try:
            yield update
        except BaseException:
            raise
        else:
            _write_generation_state_unlocked(
                db_path,
                identity,
                state="complete" if update.complete_on_exit else "incomplete",
                generation_id=generation_id,
            )
    finally:
        _release_marker_lock(lock)


def read_generation_state(
    db_path: str | Path, identity: Optional[dict[str, Any]] = None
) -> Optional[dict[str, Any]]:
    """Read a marker, returning ``None`` for missing, malformed, or stale state."""
    try:
        data = json.loads(generation_marker_path(db_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("format") != "cgc-index-generation" or data.get("format_version") != 1:
        return None
    if identity is not None and data.get("identity") != identity:
        return None
    if data.get("state") not in {"building", "incomplete", "complete"}:
        return None
    return data


def generation_is_current(db_path: str | Path, identity: dict[str, Any]) -> bool:
    """Whether a DB has a verified complete marker for this exact identity."""
    marker = read_generation_state(db_path)
    return _has_verified_identity(marker, identity)
