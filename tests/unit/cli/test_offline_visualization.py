"""Regression tests for the offline visualization fallback."""
import inspect
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codegraphcontext.cli import cli_helpers


class _Session:
    def __init__(self, records, calls=None):
        self._records = records
        self._calls = calls

    def run(self, query, **params):
        if self._calls is not None:
            self._calls.append((query, params))
        return iter(self._records)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Record(list):
    def values(self):
        return list(self)


def _db_manager(records, calls=None):
    manager = MagicMock()
    manager.get_driver.return_value.session.return_value = _Session(records, calls)
    return manager


# --- Kùzu / Ladybug shape: plain dicts with _id / _label / _src / _dst --------

KUZU_RECORDS = [
    _Record([
        {"_id": "0:1", "_label": "File", "path": "/repo/a.py", "name": "a.py"},
        {"_src": "0:1", "_dst": "0:2", "_label": "CONTAINS"},
        {"_id": "0:2", "_label": "Function", "name": "alpha", "path": "/repo/a.py",
         "line_number": 3},
    ])
]

# --- Ladybug shape: same dicts but with UPPERCASE internal keys (#1458) ------

LADYBUG_RECORDS = [
    _Record([
        {"_ID": "0:1", "_LABEL": "File", "path": "/repo/a.py", "name": "a.py"},
        {"_SRC": "0:1", "_DST": "0:2", "_LABEL": "CONTAINS"},
        {"_ID": "0:2", "_LABEL": "Function", "name": "alpha", "path": "/repo/a.py",
         "line_number": 3},
    ])
]

# --- Neo4j / Falkor shape: driver objects ------------------------------------

class _Node(dict):
    def __init__(self, element_id, labels, **props):
        super().__init__(**props)
        self.element_id = element_id
        self.labels = labels


class _Rel:
    def __init__(self, rel_type, start, end):
        self.type = rel_type
        self.start_node = start
        self.end_node = end


_N1 = _Node("4:1", ["File"], path="/repo/a.py", name="a.py")
_N2 = _Node("4:2", ["Function"], name="alpha", path="/repo/a.py", line_number=3)
NEO4J_RECORDS = [_Record([_N1, _Rel("CONTAINS", _N1, _N2), _N2])]

# --- Real FalkorDB shape: driver objects with `.labels` but NOT dict-like,
# properties live in `.properties`, and edges use `.src_node`/`.dest_node`/
# `.relation` instead of Neo4j's `.start_node`/`.end_node`/`.type`. -----------

class _FalkorNode:
    def __init__(self, node_id, labels, properties):
        self.id = node_id
        self.labels = labels
        self.properties = properties


class _FalkorEdge:
    def __init__(self, src_node, relation, dest_node, properties=None):
        # The real falkordb.Edge carries the bare integer node id here, NOT
        # a Node object — confirmed against an actual query result:
        # `r.src_node` is `<class 'int'>`, not `falkordb.node.Node`.
        self.src_node = src_node
        self.relation = relation
        self.dest_node = dest_node
        self.properties = properties or {}


_FN1 = _FalkorNode(1, ["File"], {"path": "/repo/a.py", "name": "a.py"})
_FN2 = _FalkorNode(2, ["Function"], {"name": "alpha", "path": "/repo/a.py", "line_number": 3})
FALKORDB_RECORDS = [_Record([_FN1, _FalkorEdge(_FN1.id, "CONTAINS", _FN2.id), _FN2])]

# --- Real LadybugDB shape: plain dicts, same as Kùzu, but the internal
# metadata fields are UPPERCASE — confirmed against a live query on the real
# `ladybug` package: `MATCH (n)-[r]->(m) RETURN n, r, m` returns dict keys
# ['_ID', '_LABEL', 'name', 'path'] for nodes and
# ['_SRC', '_DST', '_LABEL', '_ID'] for relationships (issue #1458). ---------

LADYBUG_RECORDS = [
    _Record([
        {"_ID": "0:1", "_LABEL": "File", "path": "/repo/a.py", "name": "a.py"},
        {"_SRC": "0:1", "_DST": "0:2", "_LABEL": "CONTAINS"},
        {"_ID": "0:2", "_LABEL": "Function", "name": "alpha", "path": "/repo/a.py",
         "line_number": 3},
    ])
]


@pytest.mark.parametrize(
    "records,label",
    [
        (KUZU_RECORDS, "kuzu-style dicts"),
        (LADYBUG_RECORDS, "ladybug-style uppercase dicts"),
        (NEO4J_RECORDS, "neo4j-style objects"),
        (FALKORDB_RECORDS, "real falkordb-style objects"),
        (LADYBUG_RECORDS, "real ladybugdb-style dicts (uppercase keys)"),
    ],
)
def test_offline_renderer_handles_both_driver_shapes(records, label, tmp_path, monkeypatch):
    """Kùzu returns plain dicts carrying `_label`/`_src`/`_dst` (lowercase);
    LadybugDB returns the same plain-dict shape but UPPERCASE
    (`_LABEL`/`_SRC`/`_DST`). Neo4j returns driver objects with
    `.labels`/`.type` that are dict-convertible. FalkorDB also carries
    `.labels` but is NOT dict-convertible (properties live in `.properties`),
    and its edges use `.src_node`/`.dest_node`/`.relation` rather than
    `.start_node`/`.end_node`/`.type`. The renderer must understand all four
    shapes or it crashes (FalkorDB) or silently produces an empty graph
    (LadybugDB, issue #1458)."""
    monkeypatch.setattr("webbrowser.open", lambda *_a, **_k: True)
    out = tmp_path / "graph.html"

    cli_helpers._render_offline_visualization(
        _db_manager(records), repo_path="/repo", output_path=str(out)
    )

    assert out.exists(), f"no HTML rendered for {label}"
    html = out.read_text(encoding="utf-8")
    assert "alpha" in html, f"node data missing for {label}"
    assert "CONTAINS" in html, f"edge data missing for {label}"


def test_offline_renderer_is_self_contained(tmp_path, monkeypatch):
    """The point of the fallback is that it needs no bundled assets, so the
    page must not reference any external resource."""
    monkeypatch.setattr("webbrowser.open", lambda *_a, **_k: True)
    out = tmp_path / "graph.html"

    cli_helpers._render_offline_visualization(
        _db_manager(KUZU_RECORDS), output_path=str(out), global_scope=True
    )

    html = out.read_text(encoding="utf-8")
    assert "http://" not in html.replace("http://www.w3.org", "")
    assert "https://" not in html.replace("https://www.w3.org", "")


def test_offline_default_query_uses_the_selected_repository_scope(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("webbrowser.open", lambda *_a, **_k: True)
    calls = []

    cli_helpers._render_offline_visualization(
        _db_manager(KUZU_RECORDS, calls),
        repo_path="/repo",
        output_path=str(tmp_path / "scoped.html"),
    )

    query, params = calls[0]
    assert "MATCH (r:Repository {path: $repo_path})" in query
    assert "EXISTS { MATCH (r)-[:CONTAINS*0..]->(target) }" in query
    assert "repo_nodes" not in query
    assert query.rstrip().endswith("SKIP 0 LIMIT 51")
    assert params == {"repo_path": "/repo"}


def test_offline_visualization_caps_database_rows_without_collecting_the_repo(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("webbrowser.open", lambda *_a, **_k: True)

    default_calls = []
    cli_helpers._render_offline_visualization(
        _db_manager(KUZU_RECORDS, default_calls),
        repo_path="/repo",
        output_path=str(tmp_path / "default-capped.html"),
    )
    default_query, _ = default_calls[0]
    assert "COLLECT(DISTINCT n) AS repo_nodes" not in default_query
    assert "repo_nodes" not in default_query
    assert default_query.rstrip().endswith("LIMIT 51")

    custom_calls = []
    cli_helpers._render_offline_visualization(
        _db_manager(KUZU_RECORDS, custom_calls),
        cypher_query="MATCH (f:Function) RETURN f AS n, null AS rel, null AS m",
        global_scope=True,
        output_path=str(tmp_path / "custom-capped.html"),
    )
    custom_query, _ = custom_calls[0]
    assert custom_query.rstrip().endswith("LIMIT 51")

    oversized_calls = []
    cli_helpers._render_offline_visualization(
        _db_manager(KUZU_RECORDS, oversized_calls),
        cypher_query=(
            "MATCH (f:Function) RETURN f AS n, null AS rel, null AS m LIMIT 1000"
        ),
        global_scope=True,
        output_path=str(tmp_path / "oversized-custom-capped.html"),
    )
    oversized_query, _ = oversized_calls[0]
    assert oversized_query.rstrip().endswith("LIMIT 51")


def test_offline_custom_query_is_scoped_and_explicit_global_scope_is_preserved(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("webbrowser.open", lambda *_a, **_k: True)
    query = "MATCH (f:Function) RETURN f AS n, null AS rel, null AS m LIMIT 10"
    scoped_calls = []
    cli_helpers._render_offline_visualization(
        _db_manager(KUZU_RECORDS, scoped_calls),
        repo_path="/repo",
        cypher_query=query,
        output_path=str(tmp_path / "custom-scoped.html"),
    )

    scoped_query, scoped_params = scoped_calls[0]
    assert "f.path STARTS WITH $_cgc_repo_prefix" in scoped_query
    assert scoped_params == {"_cgc_repo_root": "/repo", "_cgc_repo_prefix": "/repo/"}

    global_calls = []
    cli_helpers._render_offline_visualization(
        _db_manager(KUZU_RECORDS, global_calls),
        cypher_query=query,
        global_scope=True,
        output_path=str(tmp_path / "custom-global.html"),
    )
    global_query, global_params = global_calls[0]
    assert global_params == {}
    assert "_cgc_repo_prefix" not in global_query
    assert global_query.rstrip().endswith(
        "ORDER BY _cgc_sort_n SKIP 0 LIMIT 10"
    )


def test_offline_call_chain_visualization_bounds_distinct_entity_projection(
    monkeypatch, tmp_path
):
    from codegraphcontext.cli import visualizer

    monkeypatch.setattr("webbrowser.open", lambda *_a, **_k: True)
    visualizer_calls = []
    monkeypatch.setattr(
        visualizer,
        "visualize_helper",
        lambda **kwargs: visualizer_calls.append(kwargs),
    )
    visualizer.visualize_call_chain(
        [{
            "function_chain": [
                {"name": "start", "path": "/repo/start.py"},
                {"name": "finish", "path": "/repo/finish.py"},
            ],
            "call_details": [{"call_line": 12}],
        }],
        "start",
        "finish",
        repo_path="/repo",
    )
    node_start = {
        "_id": "0:start",
        "_label": "Function",
        "uid": "start",
        "name": "start",
        "path": "/repo/start.py",
        "source": "源" * 10_000,
    }
    node_finish = {
        "_id": "0:finish",
        "_label": "Function",
        "uid": "finish",
        "name": "finish",
        "path": "/repo/finish.py",
        "source": "源" * 10_000,
    }
    calls = []
    manager = _db_manager(
        [_Record([node_start, None, node_finish])], calls
    )
    manager.get_backend_type.return_value = "neo4j"

    cli_helpers._render_offline_visualization(
        manager,
        repo_path="/repo",
        cypher_query=visualizer_calls[0]["cypher_query"],
        output_path=str(tmp_path / "call-chain.html"),
        page_size=10,
    )

    executed_query = calls[0][0]
    assert "RETURN DISTINCT" in visualizer_calls[0]["cypher_query"]
    assert "RETURN DISTINCT" in executed_query
    assert "substring(start.source" in executed_query
    assert executed_query.rstrip().endswith("LIMIT 11")


def test_call_chain_visualization_uses_uids_when_paths_are_truncated(monkeypatch):
    from codegraphcontext.cli import visualizer

    calls = []
    monkeypatch.setattr("webbrowser.open", lambda *_a, **_k: True)
    monkeypatch.setattr(
        visualizer,
        "visualize_helper",
        lambda **kwargs: calls.append(kwargs),
    )

    visualizer.visualize_call_chain(
        [{
            "function_chain": [
                {
                    "uid": "start-uid",
                    "name": "start",
                    "path": "/repo/" + "s" * 1_018,
                    "path_truncated": True,
                },
                {
                    "uid": "end-uid",
                    "name": "finish",
                    "path": "/repo/" + "e" * 1_018,
                    "path_truncated": True,
                },
            ],
            "call_details": [{"call_line": 12}],
        }],
        "start",
        "finish",
        repo_path="/repo",
    )

    query = calls[0]["cypher_query"]
    assert "start.uid = 'start-uid'" in query
    assert "end.uid = 'end-uid'" in query
    assert "start.path =" not in query
    assert "end.path =" not in query


def test_offline_custom_alias_node_projection_is_bounded_before_database_read(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("webbrowser.open", lambda *_a, **_k: True)
    calls = []
    manager = _db_manager(
        [_Record([{
            "_id": "0:large-function",
            "_label": "Function",
            "uid": "large-function",
            "name": "large_function",
            "path": "/repo/large.py",
            "source": "源" * 10_000,
        }])],
        calls,
    )
    manager.get_backend_type.return_value = "neo4j"

    cli_helpers._render_offline_visualization(
        manager,
        cypher_query=(
            "MATCH (f:Function) RETURN f AS result ORDER BY f.uid"
        ),
        global_scope=True,
        output_path=str(tmp_path / "bounded-alias.html"),
    )

    executed_query = calls[0][0]
    assert "substring(f.source" in executed_query
    assert "RETURN f AS result" not in executed_query
    assert executed_query.rstrip().endswith("LIMIT 51")


def test_offline_custom_query_rejects_nested_raw_node_fallback_before_read(
    monkeypatch, tmp_path
):
    calls = []
    manager = _db_manager(KUZU_RECORDS, calls)
    manager.get_backend_type.return_value = "neo4j"

    with pytest.raises(ValueError, match="properties can be bounded"):
        cli_helpers._render_offline_visualization(
            manager,
            cypher_query="MATCH (f:Function) RETURN [f]",
            global_scope=True,
            output_path=str(tmp_path / "unsupported-raw-node.html"),
        )

    assert calls == []


def test_offline_custom_visualization_stops_at_a_bounded_row_window(
    monkeypatch, tmp_path
):
    consumed = []
    messages = []

    def records():
        for index in range(75):
            consumed.append(index)
            yield _Record([
                {
                    "_id": f"0:{index}",
                    "_label": "Function",
                    "name": f"function_{index}",
                    "path": f"/repo/{index}.py",
                    "line_number": index,
                }
            ])

    monkeypatch.setattr("webbrowser.open", lambda *_a, **_k: True)
    monkeypatch.setattr(
        cli_helpers.console, "print", lambda message, *_a, **_k: messages.append(str(message))
    )
    cli_helpers._render_offline_visualization(
        _db_manager(records()),
        cypher_query=(
            "MATCH (f:Function) RETURN f AS n, null AS rel, null AS m"
        ),
        global_scope=True,
        output_path=str(tmp_path / "bounded.html"),
    )

    assert len(consumed) == 51
    assert any("truncated" in message.lower() for message in messages)


def test_offline_custom_query_without_stable_paging_has_no_next_offset(
    monkeypatch, tmp_path
):
    from codegraphcontext.utils import visualize_graph

    messages = []
    rendered = []
    records = [
        _Record([
            {
                "_id": f"0:{index}",
                "_label": "Function",
                "name": f"function_{index}",
                "path": f"/repo/{index}.py",
                "line_number": index,
            }
        ])
        for index in range(75)
    ]

    class _Rows:
        def __iter__(self):
            return iter(records)

    class _PagingSession:
        def run(self, *_args, **_kwargs):
            return _Rows()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    manager = MagicMock()
    manager.get_backend_type.return_value = "kuzudb"
    manager.get_driver.return_value.session.return_value = _PagingSession()
    monkeypatch.setattr(
        visualize_graph,
        "open_in_browser",
        lambda data, **_kwargs: rendered.append(data) or str(tmp_path / "custom.html"),
    )
    monkeypatch.setattr(
        cli_helpers.console,
        "print",
        lambda message, *_a, **_k: messages.append(str(message)),
    )

    result = cli_helpers._render_offline_visualization(
        manager,
        cypher_query=(
            "CALL db.labels() YIELD label "
            "MATCH (f:Function) "
            "RETURN f AS n, null AS rel, null AS m LIMIT 200"
        ),
        global_scope=True,
        output_path=str(tmp_path / "custom.html"),
    )

    assert result["truncated"] is True
    assert result["continuation_supported"] is False
    assert result["next_offset"] is None
    assert result["has_more"] is False
    assert rendered[0]["continuation_supported"] is False
    assert rendered[0]["next_offset"] is None
    assert rendered[0]["has_more"] is False
    assert any("cannot be paged safely" in message for message in messages)
    assert not any("--offset" in message for message in messages)


def test_offline_visualization_offset_returns_a_usable_next_page(
    monkeypatch, tmp_path
):
    from codegraphcontext.utils import visualize_graph

    calls = []
    messages = []
    rendered = []
    records = [
        _Record([
            {
                "_id": f"0:{index}",
                "_label": "Function",
                "name": f"function_{index}",
                "path": f"/repo/{index}.py",
                "line_number": index,
            }
        ])
        for index in range(75)
    ]

    class _PagingSession:
        def run(self, query, **_params):
            calls.append((query, _params))
            page_clause = query.rsplit("SKIP", 1)[1].split()
            start, limit = int(page_clause[0]), int(page_clause[2])
            return iter(records[start:start + limit])

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    manager = MagicMock()
    manager.get_backend_type.return_value = "kuzudb"
    manager.get_driver.return_value.session.return_value = _PagingSession()

    monkeypatch.setattr(
        visualize_graph,
        "open_in_browser",
        lambda data, **_kwargs: rendered.append(data) or str(tmp_path / "page.html"),
    )
    monkeypatch.setattr(
        cli_helpers.console,
        "print",
        lambda message, *_a, **_k: messages.append(str(message)),
    )
    first_page = cli_helpers._render_offline_visualization(
        manager,
        cypher_query=(
            "MATCH (f:Function) RETURN f AS n, null AS rel, null AS m LIMIT 200"
        ),
        global_scope=True,
        output_path=str(tmp_path / "page.html"),
    )

    first_query, _params = calls[0]
    assert first_query.rstrip().endswith("SKIP 0 LIMIT 51")
    assert first_page["record_count"] == 50
    assert first_page["next_offset"] == 50
    assert rendered[0]["offset"] == 0
    assert rendered[0]["page_size"] == 50
    assert rendered[0]["next_offset"] == 50
    second_page = cli_helpers._render_offline_visualization(
        manager,
        cypher_query=(
            "MATCH (f:Function) RETURN f AS n, null AS rel, null AS m LIMIT 200"
        ),
        global_scope=True,
        output_path=str(tmp_path / "next-page.html"),
        offset=first_page["next_offset"],
    )

    second_query, _params = calls[1]
    assert second_query.rstrip().endswith("SKIP 50 LIMIT 51")
    assert second_page["record_count"] == 25
    assert rendered[1]["offset"] == 50
    assert second_page["next_offset"] is None
    assert second_page["has_more"] is False
    first_ids = {node["id"] for node in rendered[0]["nodes"]}
    second_ids = {node["id"] for node in rendered[1]["nodes"]}
    assert first_ids.isdisjoint(second_ids)
    assert len(first_ids) == 50
    assert len(second_ids) == 25
    assert any("--offset 50" in message for message in messages)


def test_visualize_cli_forwards_offset_to_offline_fallback(monkeypatch):
    from typer.testing import CliRunner

    from codegraphcontext.cli import main as cli_main

    captured = {}
    monkeypatch.setattr(cli_main, "_load_credentials", lambda: None)
    monkeypatch.setattr(
        cli_main, "visualize_helper", lambda **kwargs: captured.update(kwargs)
    )

    result = CliRunner().invoke(
        cli_main.app, ["visualize", "--global-scope", "--offset", "50"]
    )

    assert result.exit_code == 0, result.output
    assert captured["offset"] == 50


def test_offline_renderer_reports_an_empty_graph(monkeypatch, tmp_path):
    monkeypatch.setattr("webbrowser.open", lambda *_a, **_k: True)

    with pytest.raises(Exception):  # typer.Exit
        cli_helpers._render_offline_visualization(
            _db_manager([]), output_path=str(tmp_path / "x.html")
        )


def test_visualize_falls_back_instead_of_exiting():
    """`cgc visualize` used to `raise SystemExit(1)` when viz/dist was absent —
    which it always is, because the wheel never shipped it."""
    source = inspect.getsource(cli_helpers.visualize_helper)
    assert "_render_offline_visualization" in source, (
        "missing assets must degrade to the offline renderer, not exit"
    )


def test_sync_viz_dist_script_exists_and_is_executable():
    """The CLI error message told users to run ./scripts/sync_viz_dist.sh,
    which did not exist."""
    repo_root = Path(cli_helpers.__file__).resolve().parents[3]
    script = repo_root / "scripts" / "sync_viz_dist.sh"

    assert script.is_file(), f"{script} is referenced by the CLI but missing"
    assert os.access(script, os.X_OK), f"{script} is not executable"
    assert "viz/dist" in script.read_text(encoding="utf-8")
