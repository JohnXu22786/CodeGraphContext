"""Safe repository, branch, and worktree identities for isolated indexes.

Only hashes derived from local paths are exposed in serialized metadata; raw
checkout paths stay inside the process and never become public identifiers.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_DEFAULT_MARKER_LOCKS_GUARD = threading.Lock()
_DEFAULT_MARKER_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def _default_marker_lock(path: Path):
    lock_path = Path(f"{path}.lock")
    key = str(lock_path.resolve())
    with _DEFAULT_MARKER_LOCKS_GUARD:
        thread_lock = _DEFAULT_MARKER_LOCKS.setdefault(key, threading.Lock())
    thread_lock.acquire()
    descriptor = None
    os_locked = False
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
        os_locked = True
        yield
    finally:
        if descriptor is not None:
            if os_locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        thread_lock.release()


def _read_default_branch(marker: Path) -> Optional[str]:
    try:
        return json.loads(marker.read_text(encoding="utf-8")).get("branch")
    except (OSError, UnicodeError, ValueError, AttributeError):
        return None


def _git(path: Path, *args: str) -> Optional[str]:
    try:
        value = subprocess.check_output(
            ["git", *args], cwd=path, stderr=subprocess.DEVNULL, text=True
        ).strip()
        return value or None
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()[:20]


@dataclass(frozen=True)
class RepositoryIdentity:
    """Git identity for one checkout, with safe serialized IDs."""

    kind: str
    repository_id: str
    worktree_id: str
    branch: Optional[str]
    commit: Optional[str]
    worktree_root: Optional[Path] = None
    repository_root: Optional[Path] = None
    is_default_branch: bool = False
    default_branch: Optional[str] = None

    @property
    def storage_key(self) -> str:
        """Stable key for one branch in one checkout (no path disclosure)."""
        return _hash(f"{self.repository_id}\0{self.worktree_id}\0{self.branch or ''}")

    def as_dict(self) -> dict[str, object]:
        """Return path-free identity metadata suitable for documents."""
        return {
            "kind": self.kind,
            "repository_id": self.repository_id,
            "worktree_id": self.worktree_id,
            "branch": self.branch,
            "commit": self.commit,
            "storage_key": self.storage_key,
        }


def repository_identity(repo_path: str | Path) -> RepositoryIdentity:
    """Resolve the repository/worktree/branch at *repo_path* without raising.

    Detached HEADs use their commit as an explicit branch identity. Non-Git
    paths receive a path-derived private identity and are marked ``non-git``.
    """
    path = Path(repo_path).expanduser().resolve()
    probe = path if path.is_dir() else path.parent
    root_raw = _git(probe, "rev-parse", "--show-toplevel")
    if not root_raw:
        root = probe.resolve()
        normalized = root.as_posix().casefold()
        return RepositoryIdentity(
            kind="non-git",
            repository_id=_hash(f"non-git\0{normalized}"),
            worktree_id=_hash(f"worktree\0{normalized}"),
            branch=None,
            commit=None,
            worktree_root=root,
            repository_root=root,
        )

    root = Path(root_raw).resolve()
    common_raw = _git(root, "rev-parse", "--git-common-dir")
    common = Path(common_raw or ".git")
    if not common.is_absolute():
        common = root / common
    common = common.resolve()
    remote = _git(root, "remote", "get-url", "origin")
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    commit = _git(root, "rev-parse", "HEAD")
    bare_repository = _git(root, "config", "--bool", "core.bare") == "true"
    if branch is None:
        branch = f"detached:{commit}" if commit else "detached:unborn"
    elif not commit:
        branch = f"unborn:{branch}"

    try:
        common_stat = common.stat()
        repository_instance = f"{common_stat.st_dev}:{common_stat.st_ino}"
    except OSError:
        repository_instance = common.as_posix().casefold()
    worktree_id = _hash(
        f"worktree\0{root.as_posix().casefold()}\0{repository_instance}"
    )
    default_ref = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    default_branch = default_ref.removeprefix("refs/remotes/origin/") if default_ref else None
    if default_branch is None and branch in {"main", "master"}:
        # Fresh local repositories often have no remote-tracking HEAD yet.
        default_branch = branch
    return RepositoryIdentity(
        kind="git",
        repository_id=_hash(
            f"remote\0{remote}" if remote else f"repo\0{common.as_posix().casefold()}"
        ),
        worktree_id=worktree_id,
        branch=branch,
        commit=commit,
        worktree_root=root,
        # A linked checkout from a bare repository has no root working tree
        # corresponding to the common directory, regardless of its name.
        repository_root=(
            None if bare_repository else common.parent if common.name == ".git" else root
        ),
        is_default_branch=bool(default_branch and branch == default_branch),
        default_branch=default_branch,
    )


def branch_database_path(
    context_dir: str | Path,
    backend: str,
    repo_path: str | Path,
    identity: Optional[RepositoryIdentity] = None,
    *,
    persist_default: bool = True,
) -> Path:
    """Return the DB path for a default per-repository context.

    Keep the root checkout's default branch at its legacy path. Other branches,
    detached HEADs, and linked worktrees use a safe hashed directory. Non-Git
    use retains its legacy location.
    """
    context_dir = Path(context_dir)
    legacy_path = context_dir / "db" / backend
    identity = identity or repository_identity(repo_path)
    if identity.kind != "git":
        return legacy_path
    is_root_checkout = identity.worktree_root == identity.repository_root
    if identity.is_default_branch and is_root_checkout:
        return legacy_path
    if (
        is_root_checkout
        and identity.default_branch is None
        and not (identity.branch or "").startswith("detached:")
    ):
        # Repositories without a remote HEAD have no authoritative default-ref
        # name. Remember the first root checkout branch so later branch switches
        # still isolate, while preserving the established legacy DB path.
        marker = context_dir / "branch-default.json"
        if persist_default:
            context_dir.mkdir(parents=True, exist_ok=True)
            with _default_marker_lock(marker):
                default_branch = _read_default_branch(marker)
                if default_branch is None:
                    temporary = marker.with_name(
                        f"{marker.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                    )
                    try:
                        temporary.write_text(
                            json.dumps({"branch": identity.branch}, sort_keys=True) + "\n",
                            encoding="utf-8",
                        )
                        os.replace(temporary, marker)
                        default_branch = identity.branch
                    finally:
                        try:
                            temporary.unlink()
                        except FileNotFoundError:
                            pass
        else:
            default_branch = _read_default_branch(marker)
        if default_branch is None or default_branch == identity.branch:
            return legacy_path
    return context_dir / "db" / "branches" / identity.storage_key / backend
