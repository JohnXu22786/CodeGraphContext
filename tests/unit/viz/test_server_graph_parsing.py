"""Regression tests for viz/server.py's driver-shape parsing.

KùzuDB and LadybugDB share the same embedded-graph wire format (plain dict
records), but LadybugDB spells its internal metadata fields uppercase
(``_ID``/``_LABEL``/``_SRC``/``_DST``) where KùzuDB spells them lowercase
(``_id``/``_label``/``_src``/``_dst``). Confirmed by querying each backend's
real Python package directly — this is not a guess:

    >>> # real ladybug package, MATCH (n)-[r]->(m) RETURN n, r, m
    >>> list(node_row.keys())
    ['_ID', '_LABEL', 'name', 'path']
    >>> list(rel_row.keys())
    ['_SRC', '_DST', '_LABEL', '_ID']

    >>> # real kuzu package, same query
    >>> list(node_row.keys())
    ['_id', '_label', 'name', 'path']
    >>> list(rel_row.keys())
    ['_src', '_dst', '_label', '_id']

`_get_eid`/`parse_node`/`parse_rel`/`parse_element` only checked the
lowercase spelling, so every LadybugDB row was silently dropped and the
live web graph rendered empty (issue #1458).
"""
from fastapi.testclient import TestClient
import re
import pytest

from codegraphcontext.viz import server


def _install_graph_db(monkeypatch, records):
    calls = []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            match = re.search(r"\bSKIP\s+(\d+)\s+LIMIT\s+(\d+)\s*$", query, re.IGNORECASE)
            if match:
                offset, limit = map(int, match.groups())
                available = records(query, params) if callable(records) else records
                return available[offset:offset + limit]
            return records(query, params) if callable(records) else records

    class _Driver:
        def session(self, **kwargs):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self):
            return _Driver()

    monkeypatch.setattr(server, "db_manager", _DB())
    return calls


def _call_chain_query(monkeypatch):
    from codegraphcontext.cli import visualizer

    calls = []
    monkeypatch.setattr(
        visualizer, "visualize_helper", lambda **kwargs: calls.append(kwargs)
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
    return calls[0]["cypher_query"]


def test_custom_visualization_cypher_is_scoped_to_the_repository(monkeypatch):
    calls = []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            return []

    class _Driver:
        def session(self, **kwargs):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self):
            return _Driver()

    monkeypatch.setattr(server, "db_manager", _DB())

    import asyncio
    asyncio.run(server.get_graph(
        repo_path="/work/repo",
        cypher_query="MATCH (f:Function) RETURN f",
    ))

    assert len(calls) == 1
    assert "f.path = $_cgc_repo_root" in calls[0][0]
    assert "f.path STARTS WITH $_cgc_repo_prefix" in calls[0][0]
    assert calls[0][1]["_cgc_repo_root"] == "/work/repo"
    assert calls[0][1]["_cgc_repo_prefix"] == "/work/repo/"


@pytest.mark.parametrize(
    "order_by", ["", " ORDER BY f.uid", " ORDER BY id(n)"]
)
def test_custom_visualization_bounds_raw_entity_properties_in_database(
    monkeypatch, order_by
):
    node = {
        "_id": "0:large-function",
        "_label": "Function",
        "uid": "large-function",
        "name": "large_function",
        "path": "/repo/large.py",
        "source": "源" * (server.MAX_GRAPH_PROPERTY_CHARS * 10),
    }
    calls = _install_graph_db(monkeypatch, [{"n": node, "rel": None, "m": None}])
    query = (
        "MATCH (f:Function) "
        f"RETURN f AS n, NULL AS rel, NULL AS m{order_by} LIMIT 10"
    )

    response = TestClient(server.app).get(
        "/api/graph",
        params={"cypher_query": query, "global_scope": "true", "page_size": 1},
    )

    assert response.status_code == 200, response.text
    executed_query = calls[0][0]
    assert "substring(f.source" in executed_query
    assert "f AS n" not in executed_query
    if "id(n)" in order_by:
        assert "ORDER BY n._id" in executed_query
    graph_node = response.json()["nodes"][0]
    assert len(graph_node["properties"]["source"]) == server.MAX_GRAPH_PROPERTY_CHARS
    assert "source" in graph_node["truncatedProperties"]


@pytest.mark.parametrize(
    "projection, order_key",
    [("n", "n.uid"), ("n AS result", "result.uid")],
)
def test_custom_visualization_bounds_unaliased_node_before_database_read(
    monkeypatch, projection, order_key
):
    node = {
        "_id": "0:large-function",
        "_label": "Function",
        "uid": "large-function",
        "name": "large_function",
        "path": "/repo/large.py",
        "source": "源" * (server.MAX_GRAPH_PROPERTY_CHARS * 10),
    }
    calls = _install_graph_db(monkeypatch, [{"n": node, "rel": None, "m": None}])

    response = TestClient(server.app).get(
        "/api/graph",
        params={
            "cypher_query": (
                f"MATCH (n:Function) RETURN {projection} ORDER BY {order_key}"
            ),
            "global_scope": "true",
        },
    )

    assert response.status_code == 200, response.text
    executed_query = calls[0][0]
    assert "substring(n.source" in executed_query
    assert not re.search(r"\bRETURN\s+n\s+ORDER\s+BY", executed_query, re.IGNORECASE)
    graph_node = response.json()["nodes"][0]
    assert len(graph_node["properties"]["source"]) == server.MAX_GRAPH_PROPERTY_CHARS
    assert "source" in graph_node["truncatedProperties"]


def test_custom_visualization_rejects_nested_raw_node_output_before_database_read(
    monkeypatch,
):
    calls = _install_graph_db(monkeypatch, [])

    response = TestClient(server.app).get(
        "/api/graph",
        params={
            "cypher_query": "MATCH (n:Function) RETURN [n] ORDER BY n.uid",
            "global_scope": "true",
        },
    )

    assert response.status_code == 400
    assert "properties can be bounded" in response.json()["detail"]
    assert calls == []


def test_custom_visualization_normalizes_aliased_node_output_before_parsing(
    monkeypatch,
):
    node = {
        "_id": "0:large-function",
        "_label": "Function",
        "uid": "large-function",
        "name": "large_function",
        "path": "/repo/large.py",
        "source": "源" * (server.MAX_GRAPH_PROPERTY_CHARS * 10),
    }

    def rows_for_query(query, _params):
        match = re.search(
            r"\bEND\s+AS\s+`?([A-Za-z_]\w*)`?", query, re.IGNORECASE
        )
        assert match is not None
        return [{match.group(1): node}]

    calls = _install_graph_db(monkeypatch, rows_for_query)
    response = TestClient(server.app).get(
        "/api/graph",
        params={
            "cypher_query": (
                "MATCH (n:Function) RETURN n AS result ORDER BY n.uid"
            ),
            "global_scope": "true",
        },
    )

    assert response.status_code == 200, response.text
    executed_query = calls[0][0]
    assert "substring(n.source" in executed_query
    assert re.search(r"\bEND\s+AS\s+n\b", executed_query, re.IGNORECASE)
    assert not re.search(r"\bEND\s+AS\s+result\b", executed_query, re.IGNORECASE)
    graph_node = response.json()["nodes"][0]
    assert graph_node["name"] == "large_function"
    assert len(graph_node["properties"]["source"]) == server.MAX_GRAPH_PROPERTY_CHARS
    assert "source" in graph_node["truncatedProperties"]


def test_custom_visualization_resolves_return_alias_for_paged_order(monkeypatch):
    nodes = [
        {
            "_id": f"0:{name}",
            "_label": "Function",
            "uid": name,
            "name": name,
            "path": f"/repo/{name}.py",
        }
        for name in ("first", "second")
    ]

    def rows_for_query(query, _params):
        match = re.search(
            r"\bEND\s+AS\s+`?([A-Za-z_]\w*)`?", query, re.IGNORECASE
        )
        assert match is not None
        return [{match.group(1): node} for node in nodes]

    calls = _install_graph_db(monkeypatch, rows_for_query)
    response = TestClient(server.app).get(
        "/api/graph",
        params={
            "cypher_query": (
                "MATCH (n:Function) RETURN DISTINCT n AS result "
                "ORDER BY result.uid"
            ),
            "global_scope": "true",
            "offset": 1,
            "page_size": 1,
        },
    )

    assert response.status_code == 200, response.text
    executed_query = calls[0][0]
    assert "ORDER BY n.uid" in executed_query
    assert response.json()["nodes"][0]["name"] == "second"


def test_call_chain_visualization_pages_distinct_bounded_graph_rows(monkeypatch):
    from codegraphcontext.cli import visualizer

    nodes = [
        {
            "_id": f"0:{name}",
            "_label": "Function",
            "uid": name,
            "name": name,
            "path": f"/repo/{name}.py",
            "source": "源" * (server.MAX_GRAPH_PROPERTY_CHARS * 4),
        }
        for name in ("start", "finish")
    ]
    query = _call_chain_query(monkeypatch)
    calls = _install_graph_db(
        monkeypatch,
        [{"n": nodes[0], "rel": None, "m": nodes[1]}],
    )

    response = TestClient(server.app).get(
        "/api/graph",
        params={"cypher_query": query, "repo_path": "/repo", "page_size": 10},
    )

    assert response.status_code == 200, response.text
    executed_query = calls[0][0]
    assert re.search(r"RETURN\s+DISTINCT", executed_query, re.IGNORECASE)
    assert "substring(start.source" in executed_query
    assert [node["name"] for node in response.json()["nodes"]] == [
        "start",
        "finish",
    ]


def test_custom_visualization_cypher_requires_repository_or_global_scope(monkeypatch):
    calls = []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            return []

    class _Driver:
        def session(self, **kwargs):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self):
            return _Driver()

    monkeypatch.setattr(server, "db_manager", _DB())
    response = TestClient(server.app).get(
        "/api/graph", params={"cypher_query": "MATCH (f:Function) RETURN f"}
    )

    assert response.status_code == 400
    assert "repo_path" in response.json()["detail"]
    assert calls == []


@pytest.mark.parametrize("repo_path", [None, "/work/repo"])
def test_custom_visualization_explicit_global_scope_runs_unscoped(monkeypatch, repo_path):
    calls = []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            calls.append((query, params))
            return []

    class _Driver:
        def session(self, **kwargs):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "neo4j"

        def get_driver(self):
            return _Driver()

    monkeypatch.setattr(server, "db_manager", _DB())
    query = "MATCH (f:Function) RETURN f"
    params = {"cypher_query": query, "global_scope": "true"}
    if repo_path is not None:
        params["repo_path"] = repo_path
    response = TestClient(server.app).get("/api/graph", params=params)

    assert response.status_code == 200
    assert len(calls) == 1
    executed_query, query_params = calls[0]
    assert query_params == {}
    assert "substring(f.source" in executed_query
    assert "_cgc_repo_root" not in executed_query


def test_visualization_scope_rejection_is_a_client_error(monkeypatch):
    calls = _install_graph_db(monkeypatch, [])
    response = TestClient(server.app).get(
        "/api/graph",
        params={
            "cypher_query": "MATCH (f:Function) RETURN [(g:Function) | g]",
            "repo_path": "/work/repo",
        },
    )

    assert response.status_code == 400
    assert "global_scope=true" in response.json()["detail"]
    assert calls == []


def test_visualization_default_graph_requires_explicit_global_scope(monkeypatch):
    calls = _install_graph_db(monkeypatch, [])
    response = TestClient(server.app).get("/api/graph")

    assert response.status_code == 400
    assert "global_scope=true" in response.json()["detail"]
    assert calls == []


def test_global_scope_overrides_repo_path_for_the_default_graph(monkeypatch):
    records = [{
        "n": {
            "_id": "0:1",
            "_label": "Function",
            "uid": "function-1",
            "name": "example",
            "path": "/work/repo/example.py",
        },
        "rel": None,
        "m": None,
    }]
    calls = _install_graph_db(monkeypatch, records)

    response = TestClient(server.app).get(
        "/api/graph",
        params={"repo_path": "/work/repo", "global_scope": "true"},
    )

    assert response.status_code == 200, response.text
    query, params = calls[0]
    assert "MATCH (n) OPTIONAL MATCH (n)-[rel]->(m)" in query
    assert "MATCH (r:Repository" not in query
    assert params == {}


def test_visualization_response_bounds_rows_and_serialized_bytes(monkeypatch):
    records = [
        {
            "n": {
                "_id": f"0:{index}",
                "_label": "Function",
                "name": "name-" + ("n" * 1800),
                "source": "s" * 1800,
            },
            "m": None,
            "rel": None,
        }
        for index in range(6)
    ]
    _install_graph_db(monkeypatch, records)
    monkeypatch.setattr(server, "MAX_GRAPH_QUERY_ROWS", 2, raising=False)
    monkeypatch.setattr(server, "MAX_GRAPH_RESPONSE_BYTES", 2048, raising=False)
    response = TestClient(server.app).get(
        "/api/graph",
        params={
            "cypher_query": "MATCH (n:Function) RETURN n ORDER BY n.uid",
            "global_scope": "true",
        },
    )

    assert response.status_code == 200
    assert len(response.content) <= 2048
    payload = response.json()
    assert payload["truncated"] is True
    assert "row_limit" in payload["truncation_reasons"]
    assert "serialized_byte_limit" in payload["truncation_reasons"]


def test_visualization_response_previews_large_node_evidence(monkeypatch):
    calls = _install_graph_db(
        monkeypatch,
        [
            {
                "n": {
                    "_id": "0:1",
                    "_label": "Function",
                    "name": "function",
                    "source": "s" * 100_000,
                    "docstring": "d" * 50_000,
                },
                "m": None,
                "rel": None,
            }
        ],
    )
    response = TestClient(server.app).get(
        "/api/graph",
        params={
            "cypher_query": "MATCH (n:Function) RETURN n",
            "global_scope": "true",
        },
    )

    assert response.status_code == 200
    properties = response.json()["nodes"][0]["properties"]
    assert len(properties["source"]) <= 1024
    assert len(properties["docstring"]) <= 1024
    assert "source" in response.json()["nodes"][0]["truncatedProperties"]
    assert "docstring" in response.json()["nodes"][0]["truncatedProperties"]
    assert response.json()["truncated"] is True
    assert calls


def test_visualization_file_contents_are_bounded_previews(monkeypatch, tmp_path):
    source_file = tmp_path / "large.py"
    source_file.write_text("x" * (server.MAX_GRAPH_FILE_PREVIEW_CHARS * 3))
    _install_graph_db(
        monkeypatch,
        [
            {
                "n": {
                    "_id": "0:1",
                    "_label": "File",
                    "name": "large.py",
                    "path": str(source_file),
                },
                "m": None,
                "rel": None,
            }
        ],
    )
    monkeypatch.setattr(server, "is_path_allowed", lambda _path: True)
    response = TestClient(server.app).get(
        "/api/graph", params={"global_scope": "true"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["fileContents"][str(source_file)]) == server.MAX_GRAPH_FILE_PREVIEW_CHARS
    assert payload["fileContentsTruncated"][str(source_file)] is True
    assert "file_preview" in payload["truncation_reasons"]


def test_visualization_graph_pages_generated_rows_with_stable_continuation(monkeypatch):
    records = [
        {
            "n": {
                "_id": f"0:{index}",
                "_label": "Function",
                "uid": f"uid-{index}",
                "name": f"function-{index}",
                "path": f"/repo/{index}.py",
            },
            "m": None,
            "rel": None,
        }
        for index in range(5)
    ]
    calls = _install_graph_db(monkeypatch, records)
    monkeypatch.setattr(server, "MAX_GRAPH_QUERY_ROWS", 2)
    response = TestClient(server.app).get(
        "/api/graph",
        params={
            "global_scope": "true",
            "offset": 1,
            "page_size": 2,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert [node["id"] for node in payload["nodes"]] == ["0:1", "0:2"]
    assert payload["offset"] == 1
    assert payload["page_size"] == 2
    assert payload["next_offset"] == 3
    assert payload["truncated"] is True
    assert "row_limit" in payload["truncation_reasons"]
    assert "ORDER BY id(n), id(rel), id(m)" in calls[0][0]
    assert calls[0][0].rstrip().endswith("SKIP 1 LIMIT 3")


def test_repository_graph_query_does_not_collect_the_full_graph_before_paging(
    monkeypatch,
):
    calls = _install_graph_db(monkeypatch, [])

    response = TestClient(server.app).get(
        "/api/graph",
        params={"repo_path": "/work/repo", "page_size": 2},
    )

    assert response.status_code == 200
    query, params = calls[0]
    assert "COLLECT(DISTINCT n) as repo_nodes" not in query
    assert "repo_nodes" not in query
    assert query.rstrip().endswith("SKIP 0 LIMIT 3")
    assert params["repo_path"] == "/work/repo"


@pytest.mark.parametrize(
    "params",
    [
        {"repo_path": "/work/repo", "page_size": 2},
        {"global_scope": "true", "page_size": 2},
    ],
)
def test_generated_graph_queries_project_bounded_entity_fields_before_fetch(
    monkeypatch, params
):
    calls = _install_graph_db(monkeypatch, [])

    response = TestClient(server.app).get("/api/graph", params=params)

    assert response.status_code == 200
    query = calls[0][0]
    returned = query.split("RETURN", 1)[1].split("ORDER BY", 1)[0]
    assert not re.search(r"\b(?:node|n)\s+AS\s+n\b", returned, re.IGNORECASE)
    assert not re.search(r"\bRETURN\s+n\s*,\s*rel\s*,\s*m\b", query, re.IGNORECASE)
    assert "substring(" in query
    assert re.search(r"(?:node|n)\.source", query)


def test_visualization_rejects_unstable_custom_query_when_more_rows_exist(monkeypatch):
    records = [
        {
            "n": {"_id": f"0:{index}", "_label": "Function", "uid": f"uid-{index}"},
            "m": None,
            "rel": None,
        }
        for index in range(3)
    ]
    calls = _install_graph_db(monkeypatch, records)
    monkeypatch.setattr(server, "MAX_GRAPH_QUERY_ROWS", 2)
    response = TestClient(server.app).get(
        "/api/graph",
        params={
            "cypher_query": "MATCH (n:Function) RETURN n",
            "global_scope": "true",
            "page_size": 2,
        },
    )

    assert response.status_code == 400
    assert "stable ORDER BY" in response.json()["detail"]
    assert calls


def test_visualization_rejects_mixed_union_null_alias_when_relationships_tie(
    monkeypatch,
):
    caller = {
        "_id": "0:caller",
        "_label": "Function",
        "uid": "caller",
        "name": "caller",
        "path": "/repo/caller.py",
    }
    callee = {
        "_id": "0:callee",
        "_label": "Function",
        "uid": "callee",
        "name": "callee",
        "path": "/repo/callee.py",
    }
    records = [
        {
            "n": caller,
            "rel": {
                "_id": "5:first",
                "_label": "CALLS",
                "_src": "0:caller",
                "_dst": "0:callee",
            },
            "m": callee,
        },
        {
            "n": caller,
            "rel": {
                "_id": "5:second",
                "_label": "CALLS",
                "_src": "0:caller",
                "_dst": "0:callee",
            },
            "m": callee,
        },
    ]
    calls = _install_graph_db(monkeypatch, records)
    query = (
        "MATCH (caller:Function)-[rel:CALLS]->(called:Function) "
        "RETURN caller AS n, rel AS rel, called AS m UNION "
        "MATCH (caller:Function), (called:Function) "
        "RETURN caller AS n, NULL AS rel, called AS m LIMIT 10"
    )

    response = TestClient(server.app).get(
        "/api/graph",
        params={
            "cypher_query": query,
            "global_scope": "true",
            "page_size": 1,
        },
    )

    assert response.status_code == 400
    assert "stable ORDER BY" in response.json()["detail"]
    assert len(calls) == 1
    assert "substring(caller.source" in calls[0][0]
    assert "caller AS n" not in calls[0][0]


@pytest.mark.parametrize("direction", ["incoming", "outgoing"])
def test_cli_generated_call_graph_query_pages_beyond_the_first_graph_page(
    monkeypatch, direction
):
    from codegraphcontext.cli import visualizer

    captured = []
    monkeypatch.setattr(
        visualizer,
        "_launch_visualizer",
        lambda query, **kwargs: captured.append((query, kwargs)),
    )
    visualizer.visualize_call_graph(
        [], "target", direction=direction, repo_path="/repo"
    )
    query = captured[0][0]
    assert (" UNION " in query) is (direction == "outgoing")
    target = {
        "_id": "0:target",
        "_label": "Function",
        "uid": "target-uid",
        "name": "target",
        "path": "/repo/target.py",
    }
    records = [
        {
            "n": {
                "_id": f"0:{index:04d}",
                "_label": "Function",
                "uid": f"caller-{index:04d}",
                "name": f"caller-{index}",
                "path": f"/repo/{index}.py",
            },
            "rel": {
                "_id": f"5:{index:04d}",
                "_label": "CALLS",
                "_src": f"0:{index:04d}",
                "_dst": "0:target",
            },
            "m": target,
        }
        for index in range(121)
    ]
    calls = _install_graph_db(monkeypatch, records)
    client = TestClient(server.app)

    first = client.get(
        "/api/graph",
        params={"repo_path": "/repo", "cypher_query": query, "page_size": 50},
    )

    assert first.status_code == 200, first.text
    first_page = first.json()
    assert first_page["record_count"] == 50
    assert first_page["next_offset"] == 50
    assert len(first_page["links"]) == 50
    assert "ORDER BY" in calls[0][0]
    assert calls[0][0].rstrip().endswith("SKIP 0 LIMIT 51")

    second = client.get(
        "/api/graph",
        params={
            "repo_path": "/repo",
            "cypher_query": query,
            "offset": first_page["next_offset"],
            "page_size": 50,
        },
    )

    assert second.status_code == 200, second.text
    second_page = second.json()
    assert second_page["record_count"] == 50
    assert second_page["next_offset"] == 100
    assert len(second_page["links"]) == 50
    assert calls[1][0].rstrip().endswith("SKIP 50 LIMIT 51")

    third = client.get(
        "/api/graph",
        params={
            "repo_path": "/repo",
            "cypher_query": query,
            "offset": second_page["next_offset"],
            "page_size": 50,
        },
    )

    assert third.status_code == 200, third.text
    third_page = third.json()
    assert third_page["record_count"] == 20
    assert third_page["next_offset"] is None
    assert third_page["continuation_limit_reached"] is True
    assert third_page["result_limit"] == 120
    assert third_page["truncated"] is True
    assert len(third_page["links"]) == 20
    assert calls[2][0].rstrip().endswith("SKIP 100 LIMIT 20")


def test_visualization_byte_truncation_returns_a_smaller_page_retry_hint(monkeypatch):
    records = [
        {
            "n": {
                "_id": f"0:{index}",
                "_label": "Function",
                "uid": f"uid-{index}",
                "name": "name-" + ("n" * 900),
                "docstring": "d" * 900,
            },
            "m": None,
            "rel": None,
        }
        for index in range(6)
    ]
    _install_graph_db(monkeypatch, records)
    monkeypatch.setattr(server, "MAX_GRAPH_QUERY_ROWS", 10)
    monkeypatch.setattr(server, "MAX_GRAPH_RESPONSE_BYTES", 2048)
    response = TestClient(server.app).get(
        "/api/graph",
        params={"global_scope": "true", "offset": 1, "page_size": 4},
    )

    assert response.status_code == 200
    assert len(response.content) <= 2048
    payload = response.json()
    assert payload["retry_required"] is True
    assert payload["retry_offset"] == 1
    assert payload["retry_page_size"] == 2
    assert payload["next_offset"] is None
    assert "serialized_byte_limit" in payload["truncation_reasons"]


def test_visualization_file_endpoint_pages_long_source_with_line_and_char_limits(monkeypatch, tmp_path):
    source_file = tmp_path / "many-lines.py"
    source = "段落\n" * 10
    source_file.write_text(source, encoding="utf-8")
    monkeypatch.setattr(server, "is_path_allowed", lambda _path: True)
    monkeypatch.setattr(server, "MAX_GRAPH_FILE_PAGE_CHARS", 20, raising=False)
    monkeypatch.setattr(server, "MAX_GRAPH_FILE_PAGE_LINES", 3, raising=False)
    client = TestClient(server.app)

    first = client.get(
        "/api/file",
        params={"path": str(source_file), "offset": 0, "page_size": 20},
    )
    assert first.status_code == 200
    first_page = first.json()
    assert first_page["content"] == "段落\n" * 3
    assert first_page["offset"] == 0
    assert first_page["next_offset"] == len(first_page["content"])
    assert first_page["truncated"] is True

    second = client.get(
        "/api/file",
        params={
            "path": str(source_file),
            "offset": first_page["next_offset"],
            "page_size": 20,
        },
    )
    assert second.status_code == 200
    second_page = second.json()
    assert second_page["content"] == "段落\n" * 3
    assert second_page["offset"] == first_page["next_offset"]
    assert second_page["next_offset"] == len(first_page["content"]) + len(second_page["content"])

    monkeypatch.setattr(server, "MAX_GRAPH_FILE_PAGE_CHARS", 8_000)
    monkeypatch.setattr(server, "MAX_GRAPH_FILE_PAGE_LINES", 200)
    control_file = tmp_path / "escaped-control-chars.txt"
    control_file.write_text("\x01" * 9_000, encoding="utf-8")
    bounded = client.get("/api/file", params={"path": str(control_file)})
    assert bounded.status_code == 200
    assert len(bounded.content) <= 64 * 1024
    assert len(bounded.json()["content"]) == 8_000


def test_kuzu_visualization_graph_pages_order_internal_ids_without_error(monkeypatch, tmp_path):
    kuzu = pytest.importorskip("kuzu")
    database = kuzu.Database(str(tmp_path / "graph.kuzu"))
    connection = kuzu.Connection(database)
    connection.execute(
        "CREATE NODE TABLE Person("
        "name STRING, path STRING, uid STRING, lang STRING, source STRING, "
        "docstring STRING, context STRING, class_context STRING, "
        "line_number INT64, end_line INT64, is_dependency BOOLEAN, "
        "cyclomatic_complexity INT64, PRIMARY KEY(name))"
    )
    connection.execute("CREATE NODE TABLE Other(name STRING, PRIMARY KEY(name))")
    connection.execute("CREATE REL TABLE KNOWS(FROM Person TO Person)")
    connection.execute(
        "CREATE (:Person {name: 'A', path: 'a.py', uid: 'a', source: $source}), "
        "(:Person {name: 'B', path: 'b.py', uid: 'b', source: 'short'})",
        {"source": "源" * (server.MAX_GRAPH_PROPERTY_CHARS + 1000)},
    )
    connection.execute("CREATE (:Other {name: 'C'})")
    connection.execute(
        "MATCH (a:Person {name: 'A'}), (b:Person {name: 'B'}) "
        "CREATE (a)-[:KNOWS]->(b)"
    )

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            result = connection.execute(query, params)
            columns = result.get_column_names()
            return [dict(zip(columns, row)) for row in result.get_all()]

    class _Driver:
        def session(self, **kwargs):
            return _Session()

    class _DB:
        def get_backend_type(self):
            return "kuzudb"

        def get_driver(self):
            return _Driver()

    monkeypatch.setattr(server, "db_manager", _DB())
    client = TestClient(server.app)
    response = client.get("/api/graph", params={"global_scope": "true", "page_size": 1})

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["nodes"]) == 2
    assert len(payload["links"]) == 1
    assert payload["next_offset"] == 1
    node_a = next(node for node in payload["nodes"] if node["name"] == "A")
    assert node_a["properties"]["source"] == "源" * server.MAX_GRAPH_PROPERTY_CHARS
    assert "source" in node_a["truncatedProperties"]

    connection.execute("CREATE (c:Person {name: 'C'})")
    connection.execute(
        "MATCH (a:Person {name: 'A'}), (c:Person {name: 'C'}) "
        "CREATE (a)-[:KNOWS]->(c)"
    )
    bounded_query = (
        "MATCH (a:Person {name: 'A'})-[r:KNOWS]->(b:Person {name: 'B'}) "
        "RETURN a AS n, r AS rel, b AS m "
        "UNION "
        "MATCH (a:Person {name: 'A'})-[r:KNOWS]->(b:Person {name: 'C'}) "
        "RETURN a AS n, r AS rel, b AS m LIMIT 100"
    )
    bounded_custom = client.get(
        "/api/graph",
        params={
            "cypher_query": bounded_query,
            "global_scope": "true",
            "page_size": 1,
        },
    )
    assert bounded_custom.status_code == 200, bounded_custom.text
    assert len(bounded_custom.json()["links"]) == 1
    assert bounded_custom.json()["next_offset"] == 1
    bounded_custom_next = client.get(
        "/api/graph",
        params={
            "cypher_query": bounded_query,
            "global_scope": "true",
            "offset": 1,
            "page_size": 1,
        },
    )
    assert bounded_custom_next.status_code == 200, bounded_custom_next.text
    assert len(bounded_custom_next.json()["links"]) == 1

    null_relationship_query = (
        "MATCH (a:Person {name: 'A'}) "
        "RETURN a AS n, NULL AS rel, NULL AS m LIMIT 10"
    )
    null_relationship_page = client.get(
        "/api/graph",
        params={
            "cypher_query": null_relationship_query,
            "global_scope": "true",
            "page_size": 1,
        },
    )
    assert null_relationship_page.status_code == 200, null_relationship_page.text
    assert len(null_relationship_page.json()["nodes"]) == 1
    assert null_relationship_page.json()["links"] == []

    connection.execute("CREATE NODE TABLE Repository(path STRING, PRIMARY KEY(path))")
    connection.execute("CREATE NODE TABLE File(path STRING, PRIMARY KEY(path))")
    connection.execute("CREATE REL TABLE CONTAINS(FROM Repository TO File)")
    connection.execute("CREATE REL TABLE CALLS(FROM File TO File)")
    connection.execute(
        "CREATE (r:Repository {path: '/repo'}), (f:File {path: '/repo/a.py'})"
    )
    connection.execute("MATCH (r:Repository), (f:File) CREATE (r)-[:CONTAINS]->(f)")
    connection.execute("CREATE (outside:File {path: '/repo/nested/b.py'})")
    connection.execute(
        "MATCH (f:File {path: '/repo/a.py'}), (outside:File {path: '/repo/nested/b.py'}) "
        "CREATE (f)-[:CALLS]->(outside)"
    )
    scoped = client.get("/api/graph", params={"repo_path": "/repo", "page_size": 2})

    assert scoped.status_code == 200
    assert len(scoped.json()["nodes"]) == 2
    assert len(scoped.json()["links"]) == 1
    assert all(
        node["properties"].get("path") != "/repo/nested/b.py"
        for node in scoped.json()["nodes"]
    )


# --- KùzuDB shape: lowercase internal keys -----------------------------------

KUZU_NODE_A = {"_id": "0:1", "_label": "File", "path": "/repo/a.py", "name": "a.py"}
KUZU_NODE_B = {"_id": "0:2", "_label": "Function", "name": "alpha", "path": "/repo/a.py"}
KUZU_REL = {"_src": "0:1", "_dst": "0:2", "_label": "CONTAINS", "_id": "1:0"}

# --- LadybugDB shape: uppercase internal keys, everything else identical -----

LADYBUG_NODE_A = {"_ID": "0:1", "_LABEL": "File", "path": "/repo/a.py", "name": "a.py"}
LADYBUG_NODE_B = {"_ID": "0:2", "_LABEL": "Function", "name": "alpha", "path": "/repo/a.py"}
LADYBUG_REL = {"_SRC": "0:1", "_DST": "0:2", "_LABEL": "CONTAINS", "_ID": "1:0"}


def test_meta_reads_both_casings():
    assert server._meta(KUZU_NODE_A, "label") == "File"
    assert server._meta(LADYBUG_NODE_A, "label") == "File"
    assert server._meta({}, "label") is None


def test_get_eid_handles_both_casings():
    assert server._get_eid(KUZU_NODE_A) == "0:1"
    assert server._get_eid(LADYBUG_NODE_A) == "0:1"


def test_parse_element_classifies_ladybug_node_and_rel():
    """Before the fix, a LadybugDB node/rel dict matched neither branch in
    `parse_element` (both checks were lowercase-only), so nothing was ever
    routed to `parse_node`/`parse_rel`."""
    nodes_dict, edges = {}, []
    server.parse_element(LADYBUG_NODE_A, nodes_dict, edges)
    server.parse_element(LADYBUG_REL, nodes_dict, edges)
    server.parse_element(LADYBUG_NODE_B, nodes_dict, edges)

    assert "0:1" in nodes_dict and "0:2" in nodes_dict
    assert nodes_dict["0:1"]["type"] == "File"
    assert len(edges) == 1
    assert edges[0] == {"id": "1:0", "source": "0:1", "target": "0:2", "type": "CONTAINS"}


def test_parse_element_classifies_kuzu_node_and_rel():
    nodes_dict, edges = {}, []
    server.parse_element(KUZU_NODE_A, nodes_dict, edges)
    server.parse_element(KUZU_REL, nodes_dict, edges)
    server.parse_element(KUZU_NODE_B, nodes_dict, edges)

    assert "0:1" in nodes_dict and "0:2" in nodes_dict
    assert len(edges) == 1
    assert edges[0] == {"id": "1:0", "source": "0:1", "target": "0:2", "type": "CONTAINS"}


def test_parse_node_extracts_ladybug_properties():
    nodes_dict = {}
    server.parse_node(LADYBUG_NODE_A, nodes_dict)
    node = nodes_dict["0:1"]
    assert node["type"] == "File"
    assert node["name"] == "a.py"
    assert node["file"] == "/repo/a.py"


def test_parse_rel_resolves_ladybug_src_dst():
    edges = []
    server.parse_rel(LADYBUG_REL, edges)
    assert edges == [{"id": "1:0", "source": "0:1", "target": "0:2", "type": "CONTAINS"}]
