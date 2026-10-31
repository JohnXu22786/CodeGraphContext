from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time

import pytest

from codegraphcontext.core.graph_snapshot import (
    Snapshot,
    SnapshotError,
    diff_snapshots,
    build_snapshot,
    generation_metadata,
    digest_properties,
    _graph_digest,
    identity_of,
    normalize_labels,
    props_of,
    _safe_snapshot_properties,
)
from codegraphcontext.core.index_generation import (
    generation_marker_path,
    generation_is_current,
    read_generation_state,
    write_generation_state,
)
from codegraphcontext.core.graph_snapshot import SnapshotUsageError
from codegraphcontext.cli.snapshot_commands import (
    _ensure_generation_unchanged,
    effective_snapshot_identity,
    generation_info,
    snapshot_file,
)
from codegraphcontext.cli.cli_helpers import (
    _generation_update_for_context,
    add_package_helper,
    clean_helper,
)
from codegraphcontext.tools.handlers.indexing_handlers import _track_generation
from types import SimpleNamespace


def _node(identifier, name, line, *, value="same"):
    properties = {"uid": identifier, "name": name, "line_number": line, "value": value}
    return {
        "id": identifier,
        "label": "Function",
        "display": name,
        "path": "src/mod.py",
        "digest": digest_properties(properties),
        "properties": properties,
    }


def _snapshot(name, nodes, edges=(), *, repo="repo-1", generation="g1"):
    storage_key = hashlib.sha256(
        f"{repo}\0wt\0main".encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:20]
    context = {"mode": "per-repo", "name": "", "database": "ladybugdb"}
    identity = {
        "kind": "git", "repository_id": repo, "branch": "main",
        "worktree_id": "wt", "commit": "commit-1", "storage_key": storage_key,
    }
    return Snapshot(
        name=name,
        created_at="2026-10-31T00:00:00Z",
        context=context,
        nodes=list(nodes),
        edges=list(edges),
        identity=identity,
        generation=generation_metadata(
            identity,
            context,
            _graph_digest(list(nodes), list(edges)),
            complete=True,
            index_generation_id=generation,
        ),
    )


def test_graph_diff_is_deterministic_for_unchanged_added_removed_changed_nodes_and_edges():
    before_nodes = [_node("keep", "keep", 10), _node("gone", "gone", 20)]
    after_nodes = [_node("keep", "keep", 10, value="updated"), _node("new", "new", 30)]
    before_edges = [{"id": "gone -> keep", "type": "CALLS", "source": "gone", "target": "keep", "digest": "1", "properties": {}}]
    after_edges = [{"id": "new -> keep", "type": "CALLS", "source": "new", "target": "keep", "digest": "1", "properties": {}}]

    first = diff_snapshots(_snapshot("base", before_nodes, before_edges), _snapshot("current", after_nodes, after_edges))
    second = diff_snapshots(_snapshot("base", before_nodes, before_edges), _snapshot("current", after_nodes, after_edges))

    assert first == second
    assert first["summary"]["nodes"] == {"added": 1, "removed": 1, "changed": 1}
    assert first["summary"]["edges"] == {"added": 1, "removed": 1, "changed": 0}


def test_line_shift_pairs_only_unambiguous_symbol_moves():
    before = _snapshot("before", [_node("function:4", "work", 4)])
    after = _snapshot("after", [_node("function:8", "work", 8)])
    diff = diff_snapshots(before, after)
    assert diff["summary"]["nodes"] == {"added": 0, "removed": 0, "changed": 1}
    assert diff["nodes"]["changed"][0]["reason"] == "moved"

    ambiguous_before = _snapshot("before", [_node("function:4", "work", 4), _node("function:5", "work", 5)])
    ambiguous_after = _snapshot("after", [_node("function:8", "work", 8)])
    ambiguous = diff_snapshots(ambiguous_before, ambiguous_after)
    assert ambiguous["summary"]["nodes"]["added"] == 1
    assert ambiguous["summary"]["nodes"]["removed"] == 2
    assert ambiguous["summary"]["nodes"]["changed"] == 0


def test_generation_metadata_is_deterministic_and_path_free():
    identity = {"repository_id": "repo-1", "worktree_id": "wt-1", "branch": "feature", "commit": "abc"}
    first = generation_metadata(identity, {"mode": "per-repo"}, "graph-digest", complete=True)
    second = generation_metadata(identity, {"mode": "per-repo"}, "graph-digest", complete=True)
    changed = generation_metadata(identity, {"mode": "per-repo"}, "other-graph", complete=True)
    assert first == second
    assert first["id"] != changed["id"]
    assert first["complete"] and first["current"]
    assert "/" not in first["id"]


def test_diff_rejects_incomplete_stale_or_cross_repository_snapshots():
    good = _snapshot("good", [_node("a", "a", 1)])
    bad_generation = {"id": "partial", "complete": False, "current": False}
    incomplete = _snapshot("partial", [_node("a", "a", 1)])
    incomplete.generation = bad_generation
    with pytest.raises(SnapshotError, match="complete and current"):
        diff_snapshots(incomplete, good)

    different_repo = _snapshot("other", [_node("a", "a", 1)], repo="repo-2")
    with pytest.raises(SnapshotError, match="different repositories"):
        diff_snapshots(good, different_repo)

    different_context = _snapshot("other-context", [_node("a", "a", 1)])
    different_context.context = {"mode": "named", "name": "other"}
    with pytest.raises(SnapshotError, match="different contexts"):
        diff_snapshots(good, different_context)


def test_snapshot_document_records_identity_and_generation_without_checkout_paths():
    snapshot = _snapshot("named", [_node("a", "a", 1)])
    document = snapshot.to_document()
    assert document["identity"]["repository_id"] == "repo-1"
    assert document["generation"]["complete"] is True
    assert "/workspace/" not in repr(document)


@pytest.mark.parametrize("mutate", [
    lambda document: document["nodes"].pop(),
    lambda document: document["nodes"].append(dict(document["nodes"][0])),
    lambda document: document["nodes"][0].pop("properties"),
    lambda document: document["generation"].update(complete=False),
    lambda document: document["identity"].pop("repository_id"),
    lambda document: document["identity"].pop("worktree_id"),
    lambda document: document["identity"].pop("commit"),
    lambda document: document["identity"].pop("storage_key"),
    lambda document: document.pop("context"),
    lambda document: document["context"].pop("database"),
])
def test_snapshot_loader_rejects_inconsistent_or_incomplete_manifest(mutate):
    snapshot = _snapshot("valid", [_node("n1", "one", 1)])
    document = snapshot.to_document()
    mutate(document)

    with pytest.raises(SnapshotError):
        Snapshot.from_document(document)


@pytest.mark.parametrize("field,value", [("type", []), ("source", None), ("target", {})])
def test_snapshot_loader_rejects_non_string_edge_endpoints_and_type(field, value):
    edge = {
        "id": "a -[CALLS]-> b", "type": "CALLS", "source": "a", "target": "b",
        "digest": digest_properties({}), "properties": {},
    }
    document = _snapshot("valid", [], [edge]).to_document()
    document["edges"][0][field] = value

    with pytest.raises(SnapshotError, match="malformed edge"):
        Snapshot.from_document(document)


def test_snapshot_loader_rejects_edges_with_missing_node_endpoints():
    edge = {
        "id": "missing -[CALLS]-> target", "type": "CALLS",
        "source": "missing", "target": "target", "digest": digest_properties({}),
        "properties": {},
    }
    document = _snapshot("dangling", [], [edge]).to_document()

    with pytest.raises(SnapshotError, match="missing node endpoint"):
        Snapshot.from_document(document)


@pytest.mark.parametrize("kind", ["node", "edge"])
def test_snapshot_loader_rejects_property_data_with_stale_digest(kind):
    nodes = [_node("source", "source", 1)]
    edges = [{
        "id": "source -[CALLS]-> source", "type": "CALLS",
        "source": "source", "target": "source", "digest": digest_properties({}),
        "properties": {},
    }]
    document = _snapshot("tampered", nodes, edges).to_document()
    record = document["nodes"][0] if kind == "node" else document["edges"][0]
    record["properties"]["tampered"] = True

    with pytest.raises(SnapshotError, match=f"malformed {kind}"):
        Snapshot.from_document(document)


@pytest.mark.parametrize("tool_name,handler_name", [
    ("delete_repository_tool", "delete_repository"),
    ("load_bundle_tool", "load_bundle"),
])
def test_mcp_management_mutations_track_generation(monkeypatch, tmp_path, tool_name, handler_name):
    from codegraphcontext import server as server_module
    from codegraphcontext.server import MCPServer

    server = object.__new__(MCPServer)
    server._context_transition_lock = threading.RLock()
    server.graph_builder = object()
    server.code_finder = object()
    db_path = tmp_path / "shared-db"
    identity = {"repository_id": "repo", "storage_key": "main"}
    server._generation_tracking_kwargs = lambda: {
        "generation_db_path": db_path,
        "generation_identity": identity,
    }
    observed = []

    def mutate(_manager, **_args):
        observed.append(read_generation_state(db_path)["state"])
        return {"success": True}

    monkeypatch.setattr(server_module.management_handlers, handler_name, mutate)
    getattr(server, tool_name)()

    assert observed == ["building"]
    assert read_generation_state(db_path, identity)["state"] == "incomplete"
    assert generation_is_current(db_path, identity) is False


def test_diff_allows_explicit_branch_comparison_within_the_same_repository():
    before = _snapshot("main", [_node("old", "old", 1)])
    after = _snapshot("feature", [_node("new", "new", 1)])
    after.identity = {**after.identity, "branch": "feature", "worktree_id": "wt-feature"}

    diff = diff_snapshots(before, after)

    assert diff["base"]["identity"]["branch"] == "main"
    assert diff["current"]["identity"]["branch"] == "feature"
    assert diff["summary"]["total"] == 2


def test_generation_state_is_atomic_and_rejects_wrong_identity(tmp_path):
    db_path = tmp_path / "db" / "ladybugdb"
    identity = {"repository_id": "repo", "storage_key": "branch-main", "commit": "abc"}
    assert generation_is_current(db_path, identity) is False
    building = write_generation_state(db_path, identity, state="building")
    assert building["complete"] is False
    assert read_generation_state(db_path, identity)["state"] == "building"
    assert generation_is_current(db_path, identity) is False

    complete = write_generation_state(db_path, identity, state="complete", generation_id="gen-1")
    assert complete["complete"] is True
    assert complete["current"] is True
    assert read_generation_state(db_path, identity)["generation_id"] == "gen-1"
    assert generation_is_current(db_path, identity) is True
    assert read_generation_state(db_path, {**identity, "storage_key": "another-branch"}) is None
    assert generation_is_current(db_path, {**identity, "storage_key": "another-branch"}) is False
    assert not list(db_path.parent.glob("*.tmp"))


@pytest.mark.parametrize("malformed_json", ["null", "[]", '"not-a-marker"'])
def test_generation_state_rejects_non_object_json_markers(tmp_path, malformed_json):
    db_path = tmp_path / "db" / "ladybugdb"
    marker = generation_marker_path(db_path)
    marker.parent.mkdir(parents=True)
    marker.write_text(malformed_json, encoding="utf-8")

    assert read_generation_state(db_path) is None


def test_generation_updates_serialize_mutation_windows(tmp_path):
    from codegraphcontext.core.index_generation import generation_update

    db_path = tmp_path / "index"
    identity = {"repository_id": "repo", "storage_key": "main"}
    first_entered = threading.Event()
    second_entered = threading.Event()

    def first_update():
        with generation_update(db_path, identity) as update:
            first_entered.set()
            time.sleep(0.05)
            assert read_generation_state(db_path)["state"] == "building"
            update.mark_complete()

    def second_update():
        first_entered.wait()
        with generation_update(db_path, identity):
            second_entered.set()
            assert read_generation_state(db_path)["state"] == "building"

    first = threading.Thread(target=first_update)
    second = threading.Thread(target=second_update)
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert second_entered.is_set()
    assert generation_is_current(db_path, identity)


def test_partial_generation_update_does_not_certify_unverified_database(tmp_path):
    from codegraphcontext.core.index_generation import generation_update

    db_path = tmp_path / "unverified-index"
    identity = {"repository_id": "repo", "storage_key": "main"}
    with generation_update(db_path, identity):
        assert read_generation_state(db_path)["state"] == "building"

    assert read_generation_state(db_path)["state"] == "incomplete"
    assert generation_is_current(db_path, identity) is False


def test_partial_generation_update_preserves_verified_database(tmp_path):
    from codegraphcontext.core.index_generation import generation_update

    db_path = tmp_path / "verified-index"
    identity = {"repository_id": "repo", "storage_key": "main"}
    write_generation_state(db_path, identity, state="complete", generation_id="initial")

    with generation_update(db_path, identity):
        assert read_generation_state(db_path)["state"] == "building"

    assert generation_is_current(db_path, identity) is True


def test_full_reindex_can_certify_previously_unverified_database(tmp_path):
    from codegraphcontext.core.index_generation import generation_update

    db_path = tmp_path / "reindexed"
    identity = {"repository_id": "repo", "storage_key": "main"}
    with generation_update(db_path, identity) as update:
        update.mark_complete()

    assert generation_is_current(db_path, identity) is True


def test_cli_mutation_scope_does_not_certify_unknown_generation(tmp_path):
    db_path = tmp_path / "cli-db"
    db_path_value = str(db_path)
    identity = {"kind": "git", "repository_id": "repo", "storage_key": "main"}

    class Database:
        db_path = db_path_value

        def get_backend_type(self):
            return "ladybugdb"

    context = SimpleNamespace(is_local=True, repository_identity=identity)
    with _generation_update_for_context(Database(), context):
        assert read_generation_state(db_path)["state"] == "building"

    assert read_generation_state(db_path, identity)["state"] == "incomplete"
    assert generation_is_current(db_path, identity) is False


def test_clean_helper_tracks_orphan_deletion_generation(monkeypatch, tmp_path):
    db_path = tmp_path / "clean-db"
    db_path_value = str(db_path)
    identity = {"kind": "git", "repository_id": "repo", "storage_key": "main"}
    context = SimpleNamespace(is_local=True, repository_identity=identity)
    observed_states = []

    class Session:
        remaining = iter((2, 0))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, *_args, **_kwargs):
            observed_states.append(read_generation_state(db_path)["state"])
            deleted = next(self.remaining)
            return SimpleNamespace(single=lambda: {"deleted": deleted})

    class Database:
        db_path = db_path_value

        def get_backend_type(self):
            return "ladybugdb"

        def get_driver(self):
            return SimpleNamespace(session=Session)

        def close_driver(self):
            pass

    manager = Database()
    monkeypatch.setattr("codegraphcontext.cli.cli_helpers.is_db_deletion_allowed", lambda: True)
    monkeypatch.setattr(
        "codegraphcontext.cli.cli_helpers._initialize_services",
        lambda _context: (manager, object(), object(), context),
    )

    clean_helper()

    assert observed_states == ["building", "building"]
    assert read_generation_state(db_path, identity)["state"] == "incomplete"
    assert generation_is_current(db_path, identity) is False


@pytest.mark.parametrize("fail", [False, True])
def test_add_package_helper_tracks_index_generation(monkeypatch, tmp_path, fail):
    import typer

    db_path = tmp_path / "package-db"
    db_path_value = str(db_path)
    identity = {"kind": "git", "repository_id": "repo", "storage_key": "main"}
    context = SimpleNamespace(is_local=True, repository_identity=identity, cgcignore_path=None)

    class Database:
        db_path = db_path_value

        def get_backend_type(self):
            return "ladybugdb"

        def close_driver(self):
            pass

    manager = Database()
    monkeypatch.setattr(
        "codegraphcontext.cli.cli_helpers._initialize_services",
        lambda _context: (manager, object(), SimpleNamespace(list_indexed_repositories=lambda: []), context),
    )
    monkeypatch.setattr(
        "codegraphcontext.cli.cli_helpers.get_local_package_path",
        lambda *_args: str(tmp_path / "package"),
    )

    async def run_index(*_args, **_kwargs):
        assert read_generation_state(db_path)["state"] == "building"
        if fail:
            raise RuntimeError("index failed")

    monkeypatch.setattr("codegraphcontext.cli.cli_helpers._run_index_with_progress", run_index)
    monkeypatch.setattr("codegraphcontext.cli.cli_helpers._print_call_resolution_diagnostics", lambda *_args: None)
    monkeypatch.setattr("codegraphcontext.cli.cli_helpers._print_index_execution_summary", lambda *_args: None)

    if fail:
        with pytest.raises(typer.Exit):
            add_package_helper("pkg", "python")
        assert read_generation_state(db_path)["state"] == "building"
    else:
        add_package_helper("pkg", "python")
        assert read_generation_state(db_path, identity)["state"] == "incomplete"
        assert generation_is_current(db_path, identity) is False


def test_backend_property_and_label_shapes_produce_the_same_identity_and_digest():
    neo4j_shape = {"uid": "fn:work", "name": "work", "indexed_at": "one", "optional": None}
    embedded_shape = {"uid": "fn:work", "name": "work", "indexed_at": "two", "optional": None, "_id": 91}

    assert normalize_labels({"Function"}) == ["Function"]
    assert normalize_labels(["Function"]) == ["Function"]
    assert props_of(neo4j_shape) == props_of(embedded_shape) == {"uid": "fn:work", "name": "work"}
    assert identity_of(["Function"], props_of(neo4j_shape)) == identity_of(
        ["Function"], props_of(embedded_shape)
    )
    assert digest_properties(props_of(neo4j_shape)) == digest_properties(props_of(embedded_shape))


def test_snapshot_path_properties_never_store_checkout_absolute_paths(tmp_path):
    root = tmp_path / "private checkout"
    root.mkdir()
    windows_path = r"C:\Users\private\repo\src\main.py"
    safe = _safe_snapshot_properties(
        {
            "path": str(root / "src" / "main.py"),
            "dependency_path": str(tmp_path / "other" / "secret"),
            "windows_path": windows_path,
        },
        root,
    )
    assert safe["path"] == "src/main.py"
    assert safe["dependency_path"].startswith("external:")
    assert safe["windows_path"].startswith("external:")
    assert windows_path not in repr(safe)
    assert str(tmp_path) not in repr(safe)


def test_build_snapshot_records_git_context_generation_and_complete_state(tmp_path):
    root = tmp_path / "private checkout"
    root.mkdir()

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, query):
            if query.startswith("MATCH (n)"):
                return [{"labels": ["File"], "n": {"path": str(root / "src" / "main.py")}}]
            return []

    class Driver:
        def session(self):
            return Session()

    class Database:
        def get_driver(self, _graph_name=None):
            return Driver()

    identity = {
        "kind": "git",
        "repository_id": "repo-safe-id",
        "worktree_id": "worktree-safe-id",
        "storage_key": "branch-safe-id",
        "branch": "feature",
        "commit": "abc123",
    }
    generation = {"id": "gen-1", "complete": True, "current": True}
    snapshot = build_snapshot(
        Database(), name="branch-snapshot", context={"mode": "per-repo"},
        identity=identity, generation=generation, repository_root=root,
    )

    document = snapshot.to_document()
    assert document["identity"] == identity
    assert document["generation"]["complete"] is True
    assert document["nodes"][0]["path"] == "src/main.py"
    assert str(tmp_path) not in repr(document)


def test_build_snapshot_redacts_absolute_paths_embedded_in_symbol_uids(tmp_path):
    root = tmp_path / "private checkout"
    root.mkdir()
    absolute_source = str(root / "src" / "main.py")

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, query):
            if query.startswith("MATCH (n)"):
                return [{
                    "labels": ["Function"],
                    "n": {
                        "name": "run",
                        "path": absolute_source,
                        "line_number": 10,
                        "occurrence_index": 0,
                        "uid": f"run{absolute_source}100",
                    },
                }]
            return []

    class Driver:
        def session(self):
            return Session()

    class Database:
        def get_driver(self, _graph_name=None):
            return Driver()

    for repository_root in (root, None):
        snapshot = build_snapshot(
            Database(), name="path-safe", identity={"repository_id": "repo"},
            repository_root=repository_root,
        )
        document = snapshot.to_document()
        assert str(tmp_path) not in repr(document)

        expected_uid = (
            "runsrc/main.py100" if repository_root else
            "runexternal:" + hashlib.sha256(absolute_source.encode()).hexdigest()[:16] + "100"
        )
        assert document["nodes"][0]["id"] == f"Function:uid:{expected_uid}"
        assert document["nodes"][0]["properties"]["uid"] == expected_uid


def test_snapshot_name_cannot_escape_its_identity_directory():
    with pytest.raises(SnapshotUsageError, match="Invalid snapshot name"):
        snapshot_file("../../outside", {"repository_id": "repo-safe"})


def test_shared_snapshot_commands_use_context_identity_not_cwd_repository(tmp_path):
    context = SimpleNamespace(
        is_local=False, mode="named", context_name="shared", database="ladybugdb",
        db_path=str(tmp_path / "shared-db"),
    )
    identity, _root = effective_snapshot_identity(context, tmp_path)
    repeated, _root = effective_snapshot_identity(context, tmp_path)

    assert identity == repeated
    assert identity["kind"] == "shared-context"
    assert identity["branch"] is None
    assert str(tmp_path) not in repr(identity)


def test_non_git_snapshot_identity_provides_root_for_path_redaction(tmp_path):
    context = SimpleNamespace(
        is_local=True, mode="per-repo", context_name="", database="ladybugdb",
        db_path=str(tmp_path / ".codegraphcontext" / "db" / "ladybugdb"),
    )
    identity, root = effective_snapshot_identity(context, tmp_path)
    properties = _safe_snapshot_properties({"path": str(tmp_path / "src" / "app.py")}, root)

    assert identity["kind"] == "non-git"
    assert root == tmp_path.resolve()
    assert properties["path"] == "src/app.py"
    assert str(tmp_path) not in repr(properties)


def test_shared_database_generation_is_unknown_until_indexed_successfully(tmp_path):
    db_path = tmp_path / "named-db"
    identity = {"kind": "shared-context", "repository_id": "ctx-safe", "storage_key": "ctx-safe"}
    ctx = SimpleNamespace(is_local=False, mode="named", database="ladybugdb", db_path=str(db_path))

    assert generation_info(ctx, identity, str(db_path), "ladybugdb") == {
        "id": "", "complete": False, "current": False
    }
    write_generation_state(db_path, identity, state="building")
    assert generation_info(ctx, identity, str(db_path), "ladybugdb")["complete"] is False
    write_generation_state(db_path, identity, state="complete", generation_id="done")
    assert generation_info(ctx, identity, str(db_path), "ladybugdb")["complete"] is True

    assert generation_info(ctx, identity, None, "neo4j")["complete"] is False


def test_snapshot_capture_rejects_a_generation_that_changed_during_read(tmp_path):
    db_path = tmp_path / "shared-db"
    identity = {"kind": "shared-context", "repository_id": "ctx", "storage_key": "ctx"}
    ctx = SimpleNamespace(is_local=False, mode="named", database="ladybugdb", db_path=str(db_path))
    db_manager = SimpleNamespace(db_path=str(db_path), get_backend_type=lambda: "ladybugdb")
    write_generation_state(db_path, identity, state="complete", generation_id="before")
    captured_generation = generation_info(ctx, identity, str(db_path), "ladybugdb")

    _ensure_generation_unchanged(db_manager, identity, captured_generation)
    write_generation_state(db_path, identity, state="building", generation_id="during")

    with pytest.raises(SnapshotError, match="changed while capturing"):
        _ensure_generation_unchanged(db_manager, identity, captured_generation)


def test_snapshot_capture_rejects_generation_already_building(tmp_path):
    db_path = tmp_path / "shared-db"
    identity = {"kind": "shared-context", "repository_id": "ctx", "storage_key": "ctx"}
    ctx = SimpleNamespace(is_local=False, mode="named", database="ladybugdb", db_path=str(db_path))
    db_manager = SimpleNamespace(db_path=str(db_path), get_backend_type=lambda: "ladybugdb")
    write_generation_state(db_path, identity, state="building", generation_id="active")
    captured_generation = generation_info(ctx, identity, str(db_path), "ladybugdb")

    with pytest.raises(SnapshotError, match="already in progress"):
        _ensure_generation_unchanged(db_manager, identity, captured_generation)


def test_snapshot_capture_rejects_other_identity_building_same_database(tmp_path):
    db_path = tmp_path / "shared-db"
    identity = {"kind": "shared-context", "repository_id": "ctx", "storage_key": "ctx"}
    other_identity = {**identity, "repository_id": "other"}
    ctx = SimpleNamespace(is_local=False, mode="named", database="ladybugdb", db_path=str(db_path))
    db_manager = SimpleNamespace(db_path=str(db_path), get_backend_type=lambda: "ladybugdb")
    write_generation_state(db_path, other_identity, state="building", generation_id="active")
    captured_generation = generation_info(ctx, identity, str(db_path), "ladybugdb")

    with pytest.raises(SnapshotError, match="already in progress"):
        _ensure_generation_unchanged(db_manager, identity, captured_generation)


def test_snapshot_loader_rejects_stale_aggregate_graph_digest():
    snapshot = _snapshot("valid", [_node("n1", "one", 1)])
    document = snapshot.to_document()
    document["nodes"][0]["properties"]["name"] = "tampered"
    document["nodes"][0]["digest"] = digest_properties(document["nodes"][0]["properties"])

    with pytest.raises(SnapshotError, match="graph digest"):
        Snapshot.from_document(document)


def test_snapshot_loader_rejects_generation_id_not_bound_to_identity():
    snapshot = _snapshot("valid", [_node("n1", "one", 1)])
    document = snapshot.to_document()
    document["identity"]["commit"] = "different-commit"

    with pytest.raises(SnapshotError, match="generation ID"):
        Snapshot.from_document(document)


def test_snapshot_loader_rejects_repository_id_not_bound_to_storage_key():
    snapshot = _snapshot("valid", [_node("n1", "one", 1)])
    document = snapshot.to_document()
    document["identity"]["repository_id"] = "different-repository"

    with pytest.raises(SnapshotError, match="storage key"):
        Snapshot.from_document(document)


def test_legacy_complete_marker_without_state_token_is_not_certified(tmp_path):
    db_path = tmp_path / "legacy-db"
    identity = {"kind": "shared-context", "repository_id": "legacy", "storage_key": "legacy"}
    ctx = SimpleNamespace(is_local=False, mode="named", database="ladybugdb", db_path=str(db_path))
    marker_path = generation_marker_path(db_path)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps({
        "format": "cgc-index-generation",
        "format_version": 1,
        "identity": identity,
        "generation_id": "old",
        "state": "complete",
        "complete": True,
        "current": True,
    }), encoding="utf-8")

    assert generation_is_current(db_path, identity) is False
    assert generation_info(ctx, identity, str(db_path), "ladybugdb")["complete"] is False


def test_mcp_index_job_marks_generation_building_until_job_finishes(tmp_path):
    db_path = tmp_path / "mcp-db"
    identity = {"kind": "shared-context", "repository_id": "ctx", "storage_key": "ctx"}
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def build_graph():
        entered.set()
        await finish.wait()

    class Jobs:
        @staticmethod
        def get_job(_job_id):
            return SimpleNamespace(status=SimpleNamespace(value="completed"))

    async def exercise():
        task = asyncio.create_task(
            _track_generation(build_graph(), Jobs(), "job", db_path, identity)
        )
        await entered.wait()
        assert read_generation_state(db_path, identity)["state"] == "building"
        finish.set()
        await task

    asyncio.run(exercise())
    assert read_generation_state(db_path, identity)["state"] == "incomplete"
