"""Native regression for Function → Class constructor call lookups (#1723)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from codegraphcontext.tools.code_finder import CodeFinder


NATIVE_SCRIPT = r'''
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

backend = sys.argv[1]
module = "kuzu" if backend == "kuzudb" else "ladybug"
if importlib.util.find_spec(module) is None:
    raise SystemExit(77)
if backend == "ladybugdb":
    from codegraphcontext.core import ladybugdb_unavailable_reason
    reason = ladybugdb_unavailable_reason()
    if reason:
        print(reason)
        raise SystemExit(77)
    from codegraphcontext.core.database_ladybug import LadybugDBManager as Manager
else:
    from codegraphcontext.core.database_kuzu import KuzuDBManager as Manager
from codegraphcontext.tools.code_finder import CodeFinder

class Adapter:
    def get_driver(self, graph_name=None):
        return driver
    def get_backend_type(self):
        return backend

os.environ["CGC_EMBEDDED_BUFFER_POOL_MB"] = "256"
with tempfile.TemporaryDirectory() as root:
    repo = Path(root) / "repo"
    other = Path(root) / "repo-other"
    repo.mkdir()
    other.mkdir()
    path = (repo / "app.py").as_posix()
    class_path = (repo / "models.py").as_posix()
    other_path = (other / "models.py").as_posix()
    manager = Manager(str(Path(root) / "db"))
    driver = manager.get_driver()
    try:
        with driver.session() as session:
            for name in (
                "entry", "build", "leaf", "scoped_target",
                "page_entry", "page_left", "page_right", "page_merge", "page_target",
                "fallback_target", *(f"fallback_caller_{i}" for i in range(4)),
            ):
                session.run(
                    "CREATE (:Function {uid: $uid, name: $name, path: $path, "
                    "line_number: 3, is_dependency: false})",
                    uid=name, name=name, path=path,
                )
            for uid, name in (("outsider", "outsider"), ("bridge", "bridge"), ("other-entry", "entry")):
                session.run(
                    "CREATE (:Function {uid: $uid, name: $name, path: $path, "
                    "line_number: 3, is_dependency: false})", uid=uid, name=name, path=other_path,
                )
            for uid, node_path in (("widget", class_path), ("other-widget", other_path)):
                session.run(
                    "CREATE (:Class {uid: $uid, name: 'Widget', path: $path, "
                    "line_number: 10, is_dependency: false})", uid=uid, path=node_path,
                )
            session.run(
                "CREATE (:Class {uid: 'scope-base', name: 'ScopeBase', path: $path, "
                "line_number: 20, is_dependency: false})", path=class_path,
            )
            session.run(
                "CREATE (:Class {uid: 'scope-probe', name: 'ScopeProbe', path: $path, "
                "line_number: 20, is_dependency: false})", path=other_path,
            )
            session.run("MATCH (a:Function {uid: 'entry'}), (b:Function {uid: 'build'}) "
                        "CREATE (a)-[:CALLS {line_number: 5}]->(b)")
            session.run("MATCH (a:Function {uid: 'build'}), (b:Class {uid: 'widget'}) "
                        "CREATE (a)-[:CALLS {line_number: 8}]->(b)")
            session.run("MATCH (a:Function {uid: 'build'}), (b:Function {uid: 'leaf'}) "
                        "CREATE (a)-[:HEURISTIC_CALLS {line_number: 9}]->(b)")
            session.run("MATCH (a:Function {uid: 'outsider'}), (b:Class {uid: 'other-widget'}) "
                        "CREATE (a)-[:CALLS {line_number: 8}]->(b)")
            session.run("MATCH (a:Function {uid: 'outsider'}), (b:Class {uid: 'widget'}) "
                        "CREATE (a)-[:CALLS {line_number: 11}]->(b)")
            session.run("MATCH (a:Function {uid: 'entry'}), (b:Class {uid: 'other-widget'}) "
                        "CREATE (a)-[:CALLS {line_number: 12}]->(b)")
            session.run("MATCH (a:Function {uid: 'other-entry'}), (b:Function {uid: 'leaf'}) "
                        "CREATE (a)-[:CALLS {line_number: 13}]->(b)")
            session.run("MATCH (a:Function {uid: 'entry'}), (b:Function {uid: 'bridge'}) "
                        "CREATE (a)-[:CALLS {line_number: 14}]->(b)")
            session.run("MATCH (a:Function {uid: 'bridge'}), (b:Function {uid: 'scoped_target'}) "
                        "CREATE (a)-[:CALLS {line_number: 15}]->(b)")
            for i in range(4):
                session.run(
                    "MATCH (caller:Function {uid: $caller_uid}), "
                    "(target:Function {uid: 'fallback_target'}) "
                    "CREATE (caller)-[:CALLS {line_number: $line}]->(target)",
                    caller_uid=f"fallback_caller_{i}",
                    line=i + 20,
                )
            for uid, source, destination, line in (
                ("page-left-edge", "page_entry", "page_left", 30),
                ("page-right-edge", "page_entry", "page_right", 31),
                ("page-left-merge", "page_left", "page_merge", 32),
                ("page-right-merge", "page_right", "page_merge", 33),
                ("page-shared-edge", "page_merge", "page_target", 34),
            ):
                session.run(
                    "MATCH (a:Function {uid: $source}), (b:Function {uid: $destination}) "
                    "CREATE (a)-[:CALLS {line_number: $line}]->(b)",
                    source=source, destination=destination, line=line,
                )
            session.run(
                "MATCH (a:Function {uid: 'page_merge'}), "
                "(b:Function {uid: 'page_target'}) "
                "CREATE (a)-[:CALLS {line_number: 34, "
                "full_call_name: 'page_target.with_argument', args_key: '[1]'}]->(b)"
            )
            session.run("MATCH (a:Class {uid: 'scope-probe'}), (b:Class {uid: 'scope-base'}) "
                        "CREATE (a)-[:INHERITS]->(b)")
        finder = CodeFinder(Adapter())
        def edges(rows):
            return {(r["caller_name"], r["callee_name"]) for r in rows}

        for with_path in (False, True):
            target_path = class_path if with_path else None
            callers = finder.who_calls_function("Widget", path=target_path, repo_path=str(repo))
            assert [r["caller_function"] for r in callers] == ["build"], callers
            assert callers[0]["target_file_path"] == class_path
            callees = finder.what_does_function_call("build", path=path if with_path else None,
                                                     repo_path=str(repo))
            assert {r["called_function"] for r in callees} == {"Widget", "leaf"}, callees
            assert edges(finder.find_all_callers("Widget", path=target_path, repo_path=str(repo))) == {
                ("entry", "build"), ("build", "Widget"),
            }
            assert edges(finder.find_all_callees("entry", path=path if with_path else None,
                                                repo_path=str(repo))) == {
                ("entry", "build"), ("build", "Widget"), ("build", "leaf"),
            }
            direct_callers = finder.who_calls_function("Widget", repo_path=str(repo))
            assert [r["caller_function"] for r in direct_callers] == ["build"], direct_callers
            assert all(r["target_file_path"] == class_path for r in direct_callers)
            assert finder.what_does_function_call("outsider", repo_path=str(repo)) == []

        scoped_callees = finder.find_all_callees("entry", repo_path=str(repo))
        scoped_callers = finder.find_all_callers("scoped_target", repo_path=str(repo))
        assert not any(other_path in (row["caller_path"], row["callee_path"])
                       for row in scoped_callees + scoped_callers)

        caller_page = finder.find_all_callers(
            "page_target", repo_path=str(repo), depth=3,
            offset=0, page_size=3, return_metadata=True,
        )
        next_caller_page = finder.find_all_callers(
            "page_target", repo_path=str(repo), depth=3,
            offset=caller_page["next_offset"], page_size=3, return_metadata=True,
        )
        paged_edges = caller_page["results"] + next_caller_page["results"]
        paged_edge_keys = [
            (edge["caller_name"], edge["callee_name"], edge["line"])
            for edge in paged_edges
        ]
        assert caller_page["offset"] == 0
        assert caller_page["page_size"] == 3
        assert caller_page["next_offset"] == 3
        assert next_caller_page["offset"] == 3
        assert next_caller_page["next_offset"] is None
        assert len(paged_edge_keys) == 6
        assert len(set(paged_edge_keys)) == 5
        assert paged_edge_keys.count(("page_merge", "page_target", 34)) == 2
        assert set(paged_edge_keys) == {
            ("page_entry", "page_left", 30),
            ("page_entry", "page_right", 31),
            ("page_left", "page_merge", 32),
            ("page_right", "page_merge", 33),
            ("page_merge", "page_target", 34),
        }
        callee_page = finder.find_all_callees(
            "page_entry", repo_path=str(repo), depth=3,
            offset=0, page_size=3, return_metadata=True,
        )
        next_callee_page = finder.find_all_callees(
            "page_entry", repo_path=str(repo), depth=3,
            offset=callee_page["next_offset"], page_size=3, return_metadata=True,
        )
        paged_callee_edges = callee_page["results"] + next_callee_page["results"]
        paged_callee_keys = [
            (edge["caller_name"], edge["callee_name"], edge["line"])
            for edge in paged_callee_edges
        ]
        assert len(paged_callee_keys) == 6
        assert len(set(paged_callee_keys)) == 5
        assert paged_callee_keys.count(("page_merge", "page_target", 34)) == 2
        assert set(paged_callee_keys) == set(paged_edge_keys)
        call_chain_first_page = finder.find_function_call_chain(
            "page_entry", "page_target", repo_path=str(repo),
            max_depth=3, limit=2, offset=0,
        )
        call_chain_second_page = finder.find_function_call_chain(
            "page_entry", "page_target", repo_path=str(repo),
            max_depth=3, limit=2, offset=2,
        )
        call_chain_pages = call_chain_first_page + call_chain_second_page
        call_chain_keys = [
            (
                tuple(node["name"] for node in row["function_chain"]),
                tuple(
                    (call["call_line"], call["full_call_name"])
                    for call in row["call_details"]
                ),
            )
            for row in call_chain_pages
        ]
        assert len(call_chain_keys) == 4
        assert len(set(call_chain_keys)) == 4
        assert finder.find_function_call_chain(
            "entry", "scoped_target", repo_path=str(repo)
        ) == []
        hierarchy = finder.find_class_hierarchy("ScopeProbe", repo_path=str(repo))
        assert hierarchy["parent_classes"] == []
        assert hierarchy["child_classes"] == []
        assert hierarchy["methods"] == []
        # Cover the existing missing-path fallback and the traversal-depth bound.
        fallback = finder.who_calls_function("Widget", path=str(repo / "missing.py"), repo_path=str(repo))
        assert [r["caller_function"] for r in fallback] == ["build"], fallback
        fallback_first_page = finder.who_calls_function(
            "fallback_target", path=str(repo / "missing.py"), repo_path=str(repo),
            limit=2, offset=0,
        )
        fallback_second_page = finder.who_calls_function(
            "fallback_target", path=str(repo / "missing.py"), repo_path=str(repo),
            limit=2, offset=2,
        )
        assert [row["caller_function"] for row in fallback_first_page + fallback_second_page] == [
            "fallback_caller_0", "fallback_caller_1", "fallback_caller_2", "fallback_caller_3",
        ]
        assert edges(finder.find_all_callers("Widget", repo_path=str(repo), depth=1)) == {("build", "Widget")}
        assert finder.find_all_callers("Widget", path=other_path, repo_path=str(repo)) == []
        assert [r["caller_function"] for r in finder.who_calls_function("leaf", repo_path=str(repo))] == ["build"]
        scoped_diagram = finder.export_diagram_edges(level="call", repo_path=str(repo))
        assert ("entry", "build") in scoped_diagram["edges"]
        assert ("entry", "bridge") not in scoped_diagram["edges"]
        print("constructor and function queries preserve scope and depth")
    finally:
        manager.close_driver()
'''


@pytest.mark.parametrize("backend", ["kuzudb", "ladybugdb"])
def test_class_constructor_queries_on_native_backend(backend):
    # Native Kuzu and Ladybug extensions must run in separate interpreters.
    env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[3] / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        path for path in (source_root, env.get("PYTHONPATH")) if path
    )
    result = subprocess.run(
        [sys.executable, "-c", NATIVE_SCRIPT, backend],
        capture_output=True, text=True, timeout=90, env=env,
    )
    if result.returncode == 77:
        pytest.skip(result.stdout.strip() or f"{backend} is unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "preserve scope and depth" in result.stdout


@pytest.mark.parametrize("backend", ["neo4j", "falkordb"])
def test_path_conversion_preserves_driver_relationship_properties(backend):
    if backend == "neo4j":
        from neo4j import Record
        from neo4j.graph import Graph, Node, Relationship

        graph = Graph()
        nodes = [Node(graph, str(i), i, ["Function"], {"name": name, "path": "/repo/app.py"})
                 for i, name in enumerate(("entry", "build"))]
        rel = Relationship(graph, "r", 0, {"line_number": 7})
        rows = [Record([("path_nodes", nodes), ("rels", [rel])])]
    else:
        from falkordb.node import Node
        from falkordb.edge import Edge

        nodes = [Node(node_id=i, properties={"name": name, "path": "/repo/app.py"})
                 for i, name in enumerate(("entry", "build"))]
        rel = Edge(nodes[0], "CALLS", nodes[1], properties={"line_number": 7})
        rows = [{"path_nodes": nodes, "rels": [rel]}]
    assert CodeFinder._call_path_edges(rows + rows) == [{
        "caller_name": "entry", "caller_path": "/repo/app.py",
        "callee_name": "build", "callee_path": "/repo/app.py", "line": 7,
    }]


def test_path_conversion_retains_the_existing_500_edge_limit():
    row = {"path_nodes": [{"name": "entry", "path": "/repo/app.py"},
                          {"name": "build", "path": "/repo/app.py"}]}
    rows = [{**row, "rels": [{"line_number": line}]} for line in range(501)]
    assert len(CodeFinder._call_path_edges(rows)) == 500
