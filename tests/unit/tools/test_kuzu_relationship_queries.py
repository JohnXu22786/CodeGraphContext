from typing import Any, Dict, Optional
from codegraphcontext.tools.code_finder import CodeFinder
from codegraphcontext.tools.query_tool_languages.cpp_toolkit import CppToolkit

class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def __iter__(self):
        return iter(self.data())

    def data(self):
        return list(self._rows)

    def single(self):
        return None

class _FakeSession:
    def __init__(self, recorder: Dict[str, Any]):
        self._recorder = recorder

    def run(self, query: str, **kwargs: Any):
        if "index_state: 'building'" in query:
            return _FakeResult()
        self._recorder["last_query"] = query
        self._recorder["last_params"] = kwargs
        self._recorder.setdefault("queries", []).append((query, kwargs))
        return _FakeResult(self._recorder.get("result_rows"))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

class _FakeDriver:
    def __init__(self, recorder: Dict[str, Any]):
        self._recorder = recorder

    def session(self):
        return _FakeSession(self._recorder)

class _FakeDBManager:
    def __init__(self, recorder: Dict[str, Any], backend: str = "kuzudb"):
        self._recorder = recorder
        self._backend = backend

    def get_driver(self, graph_name: str = None):
        return _FakeDriver(self._recorder)

    # Used only for certain query formatting paths; safe to stub.
    def get_backend_type(self) -> str:
        return self._backend

def _make_finder(
    recorder: Optional[Dict[str, Any]] = None, backend: str = "kuzudb"
) -> tuple[CodeFinder, Dict[str, Any]]:
    if recorder is None:
        recorder = {}
    db_manager = _FakeDBManager(recorder, backend=backend)
    finder = CodeFinder(db_manager)
    return finder, recorder

def test_find_all_callers_parameterizes_symbol_and_path():
    finder, recorder = _make_finder()
    name = "TargetFn') MATCH (n) DELETE n //"
    path = "/repo/src/a'quoted.go"
    assert finder.find_all_callers(name, path=path, repo_path=None) == []
    assert name not in recorder["last_query"]
    assert path not in recorder["last_query"]
    assert recorder["last_params"]["function_name"] == name
    assert recorder["last_params"]["path"] == path


def test_find_all_callees_parameterizes_symbol_and_path():
    finder, recorder = _make_finder()
    name = "TargetFn') MATCH (n) DELETE n //"
    path = "/repo/src/a'quoted.go"
    assert finder.find_all_callees(name, path=path, repo_path=None) == []
    assert name not in recorder["last_query"]
    assert path not in recorder["last_query"]
    assert recorder["last_params"]["function_name"] == name
    assert recorder["last_params"]["path"] == path


def test_call_chain_avoids_list_extract():
    finder, recorder = _make_finder()
    finder.find_function_call_chain("FuncA", "FuncB", max_depth=5, repo_path=None)

    q = recorder["last_query"]
    assert "list_extract" not in q
    assert "MATCH path = (start)-[:CALLS|HEURISTIC_CALLS*1..5]->()" in q
    assert "size(call_rels) as chain_length" in q


def test_call_chain_uses_portable_path_keys_for_neo4j_compatible_backends():
    finder, recorder = _make_finder(backend="neo4j")
    finder.find_function_call_chain("FuncA", "FuncB", max_depth=5, limit=2)

    query = recorder["last_query"]
    assert "list_transform" not in query
    assert "func_nodes[size(func_nodes)]" not in query
    assert "last(func_nodes) as path_end" in query
    assert "reduce(" in query
    assert "toString(id(call_rel))" in query


def test_direct_call_queries_scope_both_relationship_endpoints():
    finder, recorder = _make_finder()
    repo = "/work/repo"

    finder.who_calls_function("target", repo_path=repo)
    caller_query = recorder["last_query"]
    assert "caller.path STARTS WITH $repo_path" in caller_query
    assert "target.path STARTS WITH $repo_path" in caller_query

    finder.what_does_function_call("caller", repo_path=repo)
    callee_query = recorder["last_query"]
    assert "caller.path STARTS WITH $repo_path" in callee_query
    assert "called.path STARTS WITH $repo_path" in callee_query


def test_direct_call_results_expose_resolution_provenance_and_unresolved_status():
    finder, recorder = _make_finder()

    finder.who_calls_function("target")
    query = recorder["last_query"]

    assert "call.confidence as confidence" in query
    assert "call.resolution_tier as resolution_tier" in query
    assert "call.confidence_label as confidence_label" in query
    assert "call.resolution_method as resolution_method" in query
    assert "THEN 'AMBIGUOUS'" in query
    assert "THEN 'UNRESOLVED'" in query
    assert "THEN 'UNKNOWN'" in query
    assert "ELSE 'RESOLVED'" in query


def test_transitive_call_queries_scope_every_returned_path_node():
    finder, recorder = _make_finder()
    repo = "/work/repo"

    finder.find_all_callers("target", repo_path=repo)
    callers_query = recorder["last_query"]
    assert "src.path STARTS WITH $repo_path" in callers_query
    assert "dst.path STARTS WITH $repo_path" in callers_query
    assert 'all(path_node IN nodes(p) WHERE path_node.path STARTS WITH "/work/repo/")' in callers_query

    finder.find_all_callees("caller", repo_path=repo)
    callees_query = recorder["last_query"]
    assert "src.path STARTS WITH $repo_path" in callees_query
    assert "dst.path STARTS WITH $repo_path" in callees_query
    assert 'all(path_node IN nodes(p) WHERE path_node.path STARTS WITH "/work/repo/")' in callees_query

    finder.find_function_call_chain("caller", "target", repo_path=repo)
    chain_query = recorder["last_query"]
    assert "start.path STARTS WITH $repo_path" in chain_query
    assert "end_target.path STARTS WITH $repo_path" in chain_query


def test_transitive_caller_and_callee_pages_preserve_relationship_identity():
    finder, recorder = _make_finder()

    for method, name in (
        (finder.find_all_callers, "target"),
        (finder.find_all_callees, "caller"),
    ):
        method(name, page_size=3)
        query = recorder["last_query"]
        assert "type(edge) AS _edge_type" in query
        assert "edge.full_call_name AS _edge_full_call_name" in query
        assert "edge.args_key AS _edge_args_key" in query
        assert "_edge_line,\n                    _edge_type, " in query
        assert "_edge_full_call_name, _edge_args_key" in query


def test_caller_and_callee_queries_do_not_return_full_argument_fields():
    finder, recorder = _make_finder()
    methods = (
        (finder.who_calls_function, ("target",)),
        (finder.what_does_function_call, ("caller",)),
        (finder.find_all_callers, ("target",)),
        (finder.find_all_callees, ("caller",)),
    )
    queries = []

    for method, args in methods:
        if method in (finder.find_all_callers, finder.find_all_callees):
            method(*args, page_size=2)
        else:
            method(*args, limit=2)
        queries.append(recorder["last_query"])

    for query in queries:
        returned = query.rsplit("RETURN", 1)[1]
        assert "_call_args_key" not in returned
        assert "_edge_args_key" not in returned
        if "call.args" in query:
            assert "call.args as call_args" not in query.lower()
            assert "call_args" not in returned


def test_call_chain_query_projects_only_bounded_node_and_edge_fields():
    finder, recorder = _make_finder()

    finder.find_function_call_chain("start", "end", limit=2)

    query = recorder["last_query"]
    returned = query.rsplit("RETURN", 1)[1].split("ORDER BY", 1)[0]
    assert "func_nodes as function_nodes" not in returned
    assert "call_rels as call_nodes" not in returned


def test_call_chain_returns_uid_for_visualization_of_truncated_paths():
    finder, recorder = _make_finder()
    recorder["result_rows"] = [{
        "function_nodes": [
            {
                "uid": "start-uid",
                "name": "start",
                "path": "/work/repo/" + "x" * 1_012,
                "path_total_chars": 1_100,
                "path_truncated": True,
                "line_number": 1,
            },
            {
                "uid": "end-uid",
                "name": "end",
                "path": "/work/repo/" + "y" * 1_012,
                "path_total_chars": 1_100,
                "path_truncated": True,
                "line_number": 2,
            },
        ],
        "call_nodes": [{"line_number": 12}],
        "chain_length": 1,
    }]

    result = finder.find_function_call_chain("start", "end", limit=1)

    assert result[0]["function_chain"][0]["uid"] == "start-uid"
    assert result[0]["function_chain"][1]["uid"] == "end-uid"
    assert "uid: chain_node.uid" in recorder["last_query"]


def test_kotlin_call_audit_scopes_both_call_endpoints():
    finder, recorder = _make_finder()
    repo = "/work/repo"

    finder.audit_kotlin_call_ambiguity(repo_path=repo)

    query = recorder["last_query"]
    assert "a.path STARTS WITH $repo_path" in query
    assert "b.path STARTS WITH $repo_path" in query
    assert recorder["last_params"]["repo_path"] == "/work/repo/"


def test_kotlin_call_audit_bounds_database_rows_and_target_contexts():
    finder, recorder = _make_finder()
    recorder["result_rows"] = [
        {
            "caller_name": "run",
            "caller_path": "/work/repo/A.kt",
            "caller_line": 10,
            "caller_end_line": 20,
            "call_line": 12,
            "full_call_name": "service.target",
            "target_name": "target",
            "target_path": f"/work/repo/Target{i}.kt",
            "target_line": 5,
            "target_context": "context preview",
            "target_context_total_chars": 4096,
            "_caller_uid": "caller-1",
            "_target_uid": f"target-{i}",
            "args": ["value"],
        }
        for i in range(4)
    ]

    result = finder.audit_kotlin_call_ambiguity(
        repo_path="/work/repo", offset=1, page_size=2
    )

    query = recorder["last_query"]
    params = recorder["last_params"]
    assert "substring(b.context" in query
    assert "target_context_total_chars" in query
    assert "ORDER BY caller_path" in query
    assert "type(r)" in query
    assert "r.args_key" in query
    assert "id(r)" not in query
    assert "SKIP $offset LIMIT $limit" in query
    assert params["offset"] == 0
    assert params["limit"] == 4
    assert params["context_char_limit"] <= 256
    assert result["offset"] == 1
    assert result["page_size"] == 2
    assert result["next_offset"] == 3
    assert result["examples"][0]["target_count"] == 2
    assert result["examples"][0]["resolution_status"] == "AMBIGUOUS"
    assert result["examples"][0]["partial"] is True
    assert result["examples"][0]["targets"][0]["context_total_chars"] == 4096
    assert result["examples"][0]["targets"][0]["context_truncated"] is True


def test_kotlin_call_audit_omits_unbounded_argument_source():
    finder, recorder = _make_finder()
    recorder["result_rows"] = [
        {
            "caller_name": "run",
            "caller_path": "/work/repo/A.kt",
            "caller_line": 10,
            "caller_end_line": 20,
            "call_line": 12,
            "full_call_name": "service.target",
            "target_name": "target",
            "target_path": f"/work/repo/Target{index}.kt",
            "target_line": 5,
            "target_context": "context preview",
            "target_context_total_chars": 4096,
            "_caller_uid": "caller-1",
            "_target_uid": f"target-{index}",
            "args": ["x" * 100_000],
        }
        for index in range(2)
    ]

    result = finder.audit_kotlin_call_ambiguity(repo_path="/work/repo")

    assert "r.args as args" not in recorder["last_query"]
    assert "_edge_args_key" not in recorder["last_query"].rsplit("RETURN", 1)[1]
    assert "args" not in result["examples"][0]


def test_call_chain_filters_intermediate_nodes_before_database_limit():
    finder, recorder = _make_finder()
    repo = "/work/repo"

    finder.find_function_call_chain(
        "entry", "target", repo_path=repo, limit=1
    )

    query = recorder["last_query"]
    path_scope = 'all(path_node IN nodes(path) WHERE path_node.path STARTS WITH "/work/repo/")'
    assert path_scope in query
    assert query.index(path_scope) < query.index("LIMIT $limit")


def test_call_chain_keeps_valid_repository_chain_when_path_preview_is_truncated():
    finder, recorder = _make_finder()
    repo_path = "/work/" + ("long-repository-name-" * 60)
    full_path = f"{repo_path}/src/module.py"
    recorder["result_rows"] = [{
        "function_nodes": [
            {
                "name": "entry",
                "name_total_chars": 5,
                "name_truncated": False,
                "path": full_path[:1_024],
                "path_total_chars": len(full_path),
                "path_truncated": True,
                "line_number": 1,
                "is_dependency": False,
            },
            {
                "name": "target",
                "name_total_chars": 6,
                "name_truncated": False,
                "path": full_path[:1_024],
                "path_total_chars": len(full_path),
                "path_truncated": True,
                "line_number": 2,
                "is_dependency": False,
            },
        ],
        "call_nodes": [{"line_number": 1, "full_call_name": "target"}],
        "chain_length": 1,
    }]

    result = finder.find_function_call_chain(
        "entry", "target", repo_path=repo_path, limit=1
    )

    assert len(result) == 1
    assert result[0]["function_chain"][0]["path"] == full_path[:1_024]
    query = recorder["last_query"]
    assert "all(path_node IN nodes(path) WHERE path_node.path STARTS WITH" in query


def test_path_conversion_discards_edges_from_cross_repository_paths():
    rows = [{
        "path_nodes": [
            {"name": "inside", "path": "/work/repo/src.py"},
            {"name": "outside", "path": "/work/repo-other/src.py"},
        ],
        "rels": [{"line_number": 7}],
    }]

    assert CodeFinder._call_path_edges(rows, repo_path="/work/repo/") == []


def test_class_hierarchy_scopes_start_and_returned_nodes():
    finder, recorder = _make_finder()
    finder.find_class_hierarchy("Child", repo_path="/work/repo")

    queries = [query for query, _ in recorder["queries"]]
    assert len(queries) == 3
    assert "child.path STARTS WITH $repo_path" in queries[0]
    assert "parent.path STARTS WITH $repo_path" in queries[0]
    assert "child.path STARTS WITH $repo_path" in queries[1]
    assert "grandchild.path STARTS WITH $repo_path" in queries[1]
    assert "child.path STARTS WITH $repo_path" in queries[2]
    assert "method.path STARTS WITH $repo_path" in queries[2]


def test_bounded_relationship_previews_use_backend_substring_start():
    for backend, expected_start in (
        ("kuzudb", 1),
        ("ladybugdb", 1),
        ("neo4j", 0),
    ):
        finder, recorder = _make_finder(backend=backend)

        finder.find_functions_by_argument("value", limit=1)
        finder.find_functions_by_decorator("cached", limit=1)
        finder.find_functions_by_decorator(
            "cached", limit=1, decorator_offset=16, decorator_page_size=8
        )
        finder.who_modifies_variable("value", limit=1)
        finder.find_class_hierarchy("Child", page_size=1)
        finder.find_function_overrides("run", limit=1)

        preview_queries = [
            (query, params)
            for query, params in recorder["queries"]
            if "substring(" in query.lower()
        ]
        assert len(preview_queries) >= 7
        for query, params in preview_queries:
            assert "$preview_start" in query
            assert params["preview_start"] == expected_start

        decorator_query = recorder["queries"][2][0]
        assert "f.decorators as decorators" not in decorator_query.lower()
        assert "decorators_total_count" in decorator_query.lower()
        assert "decorators_truncated" in decorator_query.lower()
        assert "decorators_next_offset" in decorator_query.lower()
        if backend in {"kuzudb", "ladybugdb"}:
            assert "list_slice(f.decorators, 17, 24)" in decorator_query
            assert "substring(_cgc_decorator_preview, 1, 128)" in decorator_query
        else:
            assert "f.decorators[16..24]" in decorator_query
            assert "substring(_cgc_decorator_preview, 0, 128)" in decorator_query
        assert "decorators_value_next_offset" in decorator_query



def test_find_by_type_scopes_path_nodes_and_repository_modules():
    finder, recorder = _make_finder()
    repo = "/work/repo"

    finder.find_by_type("function", limit=12, repo_path=repo)
    function_query, function_params = recorder["queries"][-1]
    assert "n.path STARTS WITH $repo_path" in function_query
    assert function_params["repo_path"] == repo + "/"
    assert function_params["limit"] == 12

    finder.find_by_type("file", limit=8, repo_path=repo)
    file_query, file_params = recorder["queries"][-1]
    assert "MATCH (n:File)" in file_query
    assert "n.path STARTS WITH $repo_path" in file_query
    assert file_params["repo_path"] == repo + "/"
    assert file_params["limit"] == 8

    finder.find_by_type("module", limit=12, repo_path=repo)
    module_query, module_params = recorder["queries"][-1]
    assert "MATCH (file:File)-[:IMPORTS]->(n:Module)" in module_query
    assert "file.path STARTS WITH $repo_path" in module_query
    assert module_params["repo_path"] == repo + "/"


def test_import_alias_queries_use_imports_relationship():
    finder, recorder = _make_finder()
    finder.who_imports_module("numpy")

    query = recorder["last_query"]
    assert "import_alias: imp.alias" in query
    assert "module.alias" not in query

    cpp_query = CppToolkit().get_cypher_query("Module")
    assert "i.alias AS alias" in cpp_query
    assert "m.alias AS alias" not in cpp_query
