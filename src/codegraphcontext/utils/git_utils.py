# src/codegraphcontext/utils/git_utils.py
"""Lightweight git helpers used across the indexing pipeline."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from typing import Optional


def get_repo_commit_hash(repo_path: Path) -> Optional[str]:
    """Return the full HEAD commit SHA for *repo_path*, or ``None`` if the path
    is not inside a git working tree or the ``git`` binary is unavailable.

    This function is intentionally silent — it never raises; callers that need
    the hash for non-critical enrichment can safely ignore a ``None`` return.
    """
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return sha if sha else None
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def has_unborn_git_head(repo_path: Path) -> bool:
    """Return whether HEAD names a branch ref that does not exist yet.

    A failed ``git rev-parse HEAD`` alone is ambiguous: it can mean an unborn
    branch, a missing Git worktree, or a command/repository error. Confirm an
    active worktree and symbolic HEAD, then treat only ``show-ref``'s exact
    missing-ref result as an unborn branch.
    """
    try:
        inside_worktree = subprocess.check_output(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if inside_worktree != "true":
            return False

        head_ref = subprocess.check_output(
            ["git", "symbolic-ref", "--quiet", "HEAD"],
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if not head_ref:
            return False

        ref_result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", head_ref],
            cwd=repo_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return ref_result.returncode == 1
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


def has_git_admin_entry(repo_path: Path) -> bool:
    """Return whether this path or an ancestor has usable Git admin metadata.

    Filesystem errors are treated as indeterminate metadata, so a temporary
    access failure cannot cause callers to erase a previously stored commit.
    A linked-worktree `.git` pointer only counts when its target still exists.
    """
    try:
        current = Path(repo_path).resolve()
    except (OSError, RuntimeError):
        return True

    while True:
        git_entry = current / ".git"
        try:
            entry_stat = git_entry.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            return True
        else:
            if stat.S_ISDIR(entry_stat.st_mode):
                return True
            if stat.S_ISREG(entry_stat.st_mode):
                try:
                    pointer = git_entry.read_text(encoding="utf-8").strip()
                except (OSError, UnicodeError):
                    return True
                prefix, separator, target = pointer.partition(":")
                if prefix.strip() != "gitdir" or not separator or not target.strip():
                    return True

                git_dir = Path(target.strip())
                if not git_dir.is_absolute():
                    git_dir = git_entry.parent / git_dir
                try:
                    target_stat = git_dir.stat()
                except FileNotFoundError:
                    return False
                except OSError:
                    return True
                else:
                    if stat.S_ISDIR(target_stat.st_mode):
                        return True
                    return False
            else:
                # Preserve metadata on unusual .git entries unless absence is
                # certain; this helper controls whether a stored SHA is cleared.
                return True

        if current.parent == current:
            return False
        current = current.parent


def get_repo_branch_name(repo_path: Path) -> Optional[str]:
    """Return the active git branch name for *repo_path*, or ``None``."""
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return branch if branch else None
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def get_repo_remote_url(repo_path: Path) -> Optional[str]:
    """Return the configured origin URL, or ``None`` without an origin/git.

    This only reads local git configuration; it does not contact the remote.
    The value is returned verbatim and is not logged or persisted here.
    """
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return url if url else None
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
