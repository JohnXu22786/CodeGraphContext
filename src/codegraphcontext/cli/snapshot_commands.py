# src/codegraphcontext/cli/snapshot_commands.py
"""`cgc snapshot save|list` and `cgc diff` (#1312).

This module owns the operational half of the feature: where snapshots live,
which one `--against` resolves to, and what each command prints. The Typer
surface (option parsing, service init, stream rebinding, exit codes) stays in
``cli/main.py``, and all document/diff/render logic stays in
``core/graph_snapshot.py``.

Stream contract (#1725): the *product* of every command here is written with
plain ``print()`` so it lands on stdout — `cgc diff --json > changes.json` must
yield a parseable file. Diagnostics are raised as exceptions and rendered on
the stderr console by the caller.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
from typing import Any, Dict, Optional

from ..core.graph_snapshot import (
    Snapshot,
    SnapshotError,
    build_snapshot,
    diff_snapshots,
    dump_json,
    list_snapshots,
    load_snapshot,
    render_diff_text,
    render_snapshot_list_text,
    save_result_document,
    snapshot_list_document,
    validate_name,
)
from ..core.graph_snapshot import save_snapshot as persist_snapshot
from ..core.index_generation import read_generation_state
from . import config_manager

#: Snapshots live beside the rest of the per-user configuration (#1312).
SNAPSHOT_DIR_NAME = "snapshots"


def effective_snapshot_identity(ctx: Any, cwd: Path) -> tuple[Dict[str, Any], Optional[Path]]:
    """Resolve the same repository or shared-context identity for every command."""
    from ..utils.branch_identity import repository_identity

    resolved = repository_identity(cwd)
    if getattr(ctx, "is_local", False) and not os.getenv("CGC_RUNTIME_DB_PATH"):
        selected_identity = getattr(ctx, "repository_identity", None) or resolved.as_dict()
        return selected_identity, resolved.worktree_root

    selected_db_path = os.getenv("CGC_RUNTIME_DB_PATH") or getattr(ctx, "db_path", "")
    scope = {
        "mode": str(getattr(ctx, "mode", "") or ""),
        "name": str(getattr(ctx, "context_name", "") or ""),
        "database": str(getattr(ctx, "database", "") or ""),
        "db_path": hashlib.sha256(str(selected_db_path).encode()).hexdigest(),
    }
    repository_id = hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return {
        "kind": "shared-context",
        "repository_id": repository_id,
        "worktree_id": "shared",
        "branch": None,
        "commit": None,
        "storage_key": repository_id,
    }, resolved.worktree_root


def snapshot_directory(
    identity: Dict[str, Any], context_data: Optional[Dict[str, Any]] = None
) -> Path:
    """Store snapshots under a safe identity hash, never a checkout path."""
    # Repository-level namespace permits an explicitly named snapshot from a
    # sibling branch/worktree to serve as a diff base.
    key = str(identity.get("repository_id") or "")
    if not key:
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
    context_key = hashlib.sha256(
        json.dumps(context_data or {}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    return Path(config_manager.CONFIG_DIR) / SNAPSHOT_DIR_NAME / key / context_key


def snapshot_file(
    name: str, identity: Dict[str, Any], context_data: Optional[Dict[str, Any]] = None
) -> Path:
    return snapshot_directory(identity, context_data) / f"{validate_name(name)}.json"


def context_info(ctx: Any) -> Dict[str, Any]:
    """The slice of a resolved context worth stamping into a snapshot.

    ``db_path`` is left out on purpose: it is machine-specific, and a snapshot
    shared between two checkouts should still diff cleanly.
    """
    return {
        "mode": str(getattr(ctx, "mode", "") or ""),
        "name": str(getattr(ctx, "context_name", "") or ""),
        "database": str(getattr(ctx, "database", "") or ""),
    }


def generation_info(
    ctx: Any,
    identity: Dict[str, Any],
    db_path: Optional[str] = None,
    backend: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the captured completion state for the resolved index."""
    if db_path and (backend or getattr(ctx, "database", "")) in {
        "falkordb", "kuzudb", "ladybugdb"
    }:
        # Read the marker even when it belongs to another identity: an active
        # writer can mutate this physical DB regardless of the selected
        # snapshot identity, so capture must still refuse its `building` state.
        marker = read_generation_state(db_path or ctx.db_path)
        if marker is None:
            return {"id": "", "complete": False, "current": False}
        same_identity = marker.get("identity") == identity
        verified = bool(
            same_identity
            and marker.get("state_id")
            and marker.get("state") == "complete"
            and marker.get("complete") is True
            and marker.get("current") is True
        )
        return {
            "id": marker.get("generation_id", "") if same_identity else "",
            "complete": verified,
            "current": verified,
            "state_id": marker.get("state_id", "") if same_identity else "",
            "state": marker.get("state", ""),
        }
    # Without a durable completion marker, a shared or remote database may be
    # empty, partially indexed, or changing concurrently. Preserve that unknown
    # state so diff validation cannot certify it as complete.
    return {"id": "", "complete": False, "current": False}


def _ensure_generation_unchanged(
    db_manager: Any, identity: Dict[str, Any], generation: Dict[str, Any]
) -> None:
    """Reject a snapshot read that overlapped a marker-backed graph update."""
    if not generation.get("complete") or not generation.get("current"):
        if generation.get("state") == "building":
            raise SnapshotError("Index generation update is already in progress; retry after indexing")
        return
    state_id = generation.get("state_id")
    db_path = getattr(db_manager, "db_path", None)
    backend = getattr(db_manager, "get_backend_type", lambda: "")()
    if not state_id or not db_path or backend not in {"falkordb", "kuzudb", "ladybugdb"}:
        raise SnapshotError("Index generation changed while capturing the snapshot; retry after indexing")
    marker = read_generation_state(db_path, identity)
    if (
        marker is None
        or marker.get("state") != "complete"
        or marker.get("complete") is not True
        or marker.get("current") is not True
        or marker.get("state_id") != state_id
    ):
        raise SnapshotError("Index generation changed while capturing the snapshot; retry after indexing")


def run_snapshot_save(
    db_manager: Any,
    *,
    name: str,
    force: bool,
    ctx: Any,
    identity: Dict[str, Any],
    generation: Dict[str, Any],
    repository_root: Optional[Path] = None,
    as_json: bool = False,
) -> Snapshot:
    """Serialise the live index and write it to the snapshot directory."""
    name = validate_name(name)
    snapshot = build_snapshot(
        db_manager, name=name, context=context_info(ctx),
        identity=identity, generation=generation, repository_root=repository_root,
    )
    _ensure_generation_unchanged(db_manager, identity, generation)
    path = persist_snapshot(
        snapshot, snapshot_file(snapshot.name, identity, context_info(ctx)), force=force
    )
    if as_json:
        print(dump_json(save_result_document(snapshot, path)))
    else:
        counts = snapshot.counts
        print(
            f'Saved snapshot "{snapshot.name}" '
            f"({counts['nodes']} nodes, {counts['edges']} edges) in CGC snapshot storage"
        )
    return snapshot


def run_snapshot_list(
    identity: Dict[str, Any], ctx: Any, as_json: bool = False
) -> None:
    """List saved snapshots; never touches the database."""
    entries = list_snapshots(snapshot_directory(identity, context_info(ctx)))
    if as_json:
        print(dump_json(snapshot_list_document(entries)))
    else:
        print("\n".join(render_snapshot_list_text(entries)))


def load_base_snapshot(
    against: Optional[str], identity: Dict[str, Any], ctx: Any
) -> Snapshot:
    """Resolve ``--against`` to a snapshot: an explicit name, else the newest."""
    context_data = context_info(ctx)
    directory = snapshot_directory(identity, context_data)
    if against:
        path = snapshot_file(against, identity, context_data)
        if not path.is_file():
            available = [entry.name for entry in list_snapshots(directory) if entry.snapshot]
            hint = f" Available: {', '.join(available)}" if available else " No snapshots saved yet."
            raise SnapshotError(f"Snapshot {against!r} not found for this repository/context.{hint}")
        return load_snapshot(path)

    entries = [entry for entry in list_snapshots(directory) if entry.snapshot]
    if not entries:
        raise SnapshotError(
            "No snapshots saved for this repository/context. Run `cgc snapshot save --name <name>` first."
        )
    return entries[0].snapshot


def run_diff(
    db_manager: Any,
    *,
    base: Snapshot,
    ctx: Any,
    identity: Dict[str, Any],
    generation: Dict[str, Any],
    repository_root: Optional[Path] = None,
    as_json: bool = False,
    limit: Optional[int] = None,
) -> bool:
    """Diff the live index against *base*. Returns True when anything changed.

    *base* is resolved by the caller first so a bad ``--against`` fails before
    a database connection is opened.
    """
    current = build_snapshot(
        db_manager, name="current", context=context_info(ctx),
        identity=identity, generation=generation, repository_root=repository_root,
    )
    _ensure_generation_unchanged(db_manager, identity, generation)
    diff = diff_snapshots(base, current)
    if as_json:
        print(dump_json(diff))
    else:
        print("\n".join(render_diff_text(diff, limit)))
    return diff["summary"]["total"] > 0
