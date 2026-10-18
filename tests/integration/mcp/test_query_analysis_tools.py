"""Behavior coverage for MCP query and analysis tools on an isolated Kùzu graph.

Each assertion enters through ``MCPServer.handle_tool_call`` so the public
tool name, argument normalization, wrapper, and handler all take part.  The
repository, context, database, report output, and datasource fixture are owned
by this test and live under pytest's temporary directory.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from codegraphcontext.core import is_falkordb_usable
from codegraphcontext.cli import config_manager
from codegraphcontext.server import MCPServer


_CONFIG_PATH_NAMES = (
    "CONFIG_DIR",
    "CONFIG_FILE",
    "CONTEXT_CONFIG_FILE",
    "_LEGACY_CONTEXT_CONFIG_FILE",
)
_ISOLATION_ENV_NAMES = (
    "CGC_HOME",
    "CGC_RUNTIME_DB_PATH",
    "CGC_RUNTIME_DB_TYPE",
    "CGC_ALLOWED_ROOTS",
    "CGC_REPO_CONTEXT_DIR",
    "CGC_IGNORE_PROJECT_ENV",
    "CGC_LOAD_PROJECT_ENV",
    "DEFAULT_DATABASE",
    "FALKORDB_HOST",
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "NORNIC_URI",
    "NORNIC_USERNAME",
    "NORNIC_PASSWORD",
)


@pytest.fixture(scope="module")
def mcp_graph(tmp_path_factory):
    """Build a real MCP server and index only a temporary Java repository."""
    pytest.importorskip("kuzu")

    root = tmp_path_factory.mktemp("mcp-query-analysis")
    repo = root / "java-service"
    repo.mkdir()
    home = root / "cgc-home"
    home.mkdir()
    context = repo / ".codegraphcontext"
    context.mkdir()
    (context / "db").mkdir()

    old_config_paths = {name: getattr(config_manager, name) for name in _CONFIG_PATH_NAMES}
    old_environment = {name: os.environ.get(name) for name in _ISOLATION_ENV_NAMES}

    server = None
    falkor_server = None
    try:
        config_manager.CONFIG_DIR = home
        config_manager.CONFIG_FILE = home / ".env"
        config_manager.CONTEXT_CONFIG_FILE = home / "config.yaml"
        config_manager._LEGACY_CONTEXT_CONFIG_FILE = home / "cgc_config.yaml"

        config_manager.CONFIG_FILE.write_text(
            "DEFAULT_DATABASE=kuzudb\nSCIP_INDEXER=false\nIGNORE_HIDDEN_FILES=true\n",
            encoding="utf-8",
        )
        config_manager.CONTEXT_CONFIG_FILE.write_text(
            "version: 1\nmode: per-repo\ndefault_context: ''\ncontexts: {}\n",
            encoding="utf-8",
        )
        (context / "config.yaml").write_text("database: kuzudb\n", encoding="utf-8")
        (context / ".cgcignore").write_text(".git/\n.codegraphcontext/\n", encoding="utf-8")

        (repo / "OrderController.java").write_text(
            """package fixture;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api")
public class OrderController {
    @GetMapping("/orders")
    public String getOrders() {
        return normalize(" ready ");
    }

    @GetMapping("/health")
    public String health() {
        return "ok";
    }

    @PostMapping("/orders")
    public String createOrder() {
        return "created";
    }

    private String normalize(String value) {
        if (value == null) {
            return "unknown";
        }
        return value.trim();
    }

    private int branchy(int value) {
        if (value > 0) {
            if (value > 10) {
                return 2;
            }
            return 1;
        }
        return 0;
    }

    private void unusedRoutine() {
        System.out.println("unused");
    }
}
""",
            encoding="utf-8",
        )
        (repo / "OrderService.java").write_text(
            """package fixture;

import org.springframework.stereotype.Service;

@Service
public class OrderService {
    public String loadOrders() {
        return "orders";
    }
}
""",
            encoding="utf-8",
        )
        (repo / "analysis_fixture.py").write_text(
            """def normalize_value(value):
    if value is None:
        return "unknown"
    return value.strip()


def get_orders():
    return normalize_value(" ready ")


def branchy(value):
    if value > 0:
        if value > 10:
            return 2
        return 1
    return 0


def unused_python_routine():
    return "unused"
""",
            encoding="utf-8",
        )

        for name, value in {
            "CGC_HOME": str(home),
            "CGC_RUNTIME_DB_TYPE": "kuzudb",
            "CGC_ALLOWED_ROOTS": str(root),
            "CGC_REPO_CONTEXT_DIR": ".codegraphcontext",
            "CGC_IGNORE_PROJECT_ENV": "1",
            "DEFAULT_DATABASE": "kuzudb",
        }.items():
            os.environ[name] = value
        for name in (
            "CGC_RUNTIME_DB_PATH",
            "CGC_LOAD_PROJECT_ENV",
            "FALKORDB_HOST",
            "NEO4J_URI",
            "NEO4J_USERNAME",
            "NEO4J_PASSWORD",
            "NORNIC_URI",
            "NORNIC_USERNAME",
            "NORNIC_PASSWORD",
        ):
            os.environ.pop(name, None)

        server = MCPServer(cwd=repo)
        assert server.resolved_context.is_local
        assert Path(server.resolved_context.db_path).is_relative_to(context)
        assert server.db_manager.get_backend_type() == "kuzudb"

        _index_and_seed(server, repo, context)

        # The two optional Spring/datasource query shapes are currently not
        # supported by Kùzu's binder. Exercise their successful path against
        # the already available local FalkorDB Lite backend as well.
        if is_falkordb_usable():
            (context / "config.yaml").write_text("database: falkordb\n", encoding="utf-8")
            os.environ["CGC_RUNTIME_DB_TYPE"] = "falkordb"
            falkor_server = MCPServer(cwd=repo)
            assert falkor_server.resolved_context.is_local
            assert falkor_server.db_manager.get_backend_type() == "falkordb"
            _index_and_seed(falkor_server, repo, context)

        yield {"server": server, "falkor_server": falkor_server, "repo": repo, "root": root}
    finally:
        cleanup_error = None
        try:
            for active_server in (falkor_server, server):
                if active_server is None:
                    continue
                try:
                    active_server.shutdown()
                except BaseException as error:
                    if cleanup_error is None:
                        cleanup_error = error
                try:
                    if not active_server.loop.is_closed():
                        active_server.loop.close()
                except BaseException as error:
                    if cleanup_error is None:
                        cleanup_error = error
        finally:
            for name, value in old_environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            for name, value in old_config_paths.items():
                setattr(config_manager, name, value)
        if cleanup_error is not None:
            raise cleanup_error


def _index_and_seed(server: MCPServer, repo: Path, context: Path) -> None:
    server.loop.run_until_complete(
        server.graph_builder.build_graph_from_path_async(
            repo,
            is_dependency=False,
            job_id=None,
            cgcignore_path=str(context / ".cgcignore"),
        )
    )
    # Seed through the fixture writer, never by issuing write Cypher through an
    # MCP query tool; all writes stay in the per-repo temporary database.
    server.graph_builder._writer.write_datasource_graph(
        {
            "datasource": {
                "name": "fixture-mysql",
                "kind": "mysql",
                "host": "127.0.0.1",
                "env": "test",
            },
            "tables": [
                {
                    "fqn": "fixture-mysql.orders",
                    "name": "orders",
                    "datasource_name": "fixture-mysql",
                }
            ],
            "columns": [
                {
                    "name": "id",
                    "table_fqn": "fixture-mysql.orders",
                    "type": "INT",
                    "nullable": False,
                    "datasource_name": "fixture-mysql",
                    "is_primary_key": True,
                }
            ],
            "key_patterns": [],
        }
    )


def _falkor_server(mcp_graph) -> MCPServer:
    server = mcp_graph["falkor_server"]
    if server is None:
        pytest.skip("FalkorDB Lite is unavailable; Kùzu reports the documented binder limitation")
    return server


def _call(server: MCPServer, tool_name: str, arguments: dict) -> dict:
    """Call the same async dispatcher used by the MCP request loop."""

    async def invoke():
        return await server.handle_tool_call(tool_name, arguments)

    return asyncio.run(invoke())


def test_find_code_returns_matching_fixture_function(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "find_code",
        {"query": "getOrders", "fuzzy_search": False, "repo_path": str(mcp_graph["repo"])},
    )

    assert result["success"] is True
    assert any(row["name"] == "getOrders" for row in result["results"]["functions_by_name"])


def test_find_code_returns_no_matches_for_unknown_repository_filter(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "find_code",
        {"query": "getOrders", "repo_path": "/not/indexed/here"},
    )

    assert result["success"] is True
    assert result["results"]["ranked_results"] == []


def test_analyze_code_relationships_returns_real_callees(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "analyze_code_relationships",
        {
            "query_type": "find_callees",
            "target": "get_orders",
            "repo_path": str(mcp_graph["repo"]),
        },
    )

    assert result["success"] is True
    assert any(row.get("called_function") == "normalize_value" for row in result["results"]["results"])


def test_analyze_code_relationships_rejects_invalid_depth(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "analyze_code_relationships",
        {"query_type": "find_callees", "target": "getOrders", "depth": "deep"},
    )

    assert "Invalid 'depth' value" in result["error"]


def test_execute_cypher_query_reads_fixture_rows(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "execute_cypher_query",
        {
            "cypher_query": "MATCH (f:Function {name: $name}) RETURN f.name AS name",
            "params": {"name": "getOrders"},
        },
    )

    assert result["success"] is True
    assert result["results"] == [{"name": "getOrders"}]


def test_execute_cypher_query_rejects_write_query_without_mutating_graph(mcp_graph):
    before = _call(
        mcp_graph["server"],
        "execute_cypher_query",
        {"cypher_query": "MATCH (n) RETURN count(n) AS count"},
    )
    assert before["success"] is True
    assert len(before["results"]) == 1
    assert before["results"][0]["count"] > 0

    result = _call(
        mcp_graph["server"],
        "execute_cypher_query",
        {"cypher_query": "CREATE (:FixtureOnly {name: 'must-not-run'})"},
    )

    assert "error" in result
    assert "read-only" in result["error"].lower()

    after = _call(
        mcp_graph["server"],
        "execute_cypher_query",
        {"cypher_query": "MATCH (n) RETURN count(n) AS count"},
    )
    assert after["success"] is True
    assert len(after["results"]) == 1
    assert after["results"][0]["count"] == before["results"][0]["count"]


def test_find_dead_code_reports_unreferenced_fixture_method(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "find_dead_code",
        {"repo_path": str(mcp_graph["repo"])},
    )

    assert result["success"] is True
    unused_names = {
        row["function_name"]
        for row in result["results"]["potentially_unused_functions"]
    }
    assert "unusedRoutine" in unused_names


def test_find_dead_code_returns_no_results_for_unknown_repository_filter(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "find_dead_code",
        {"repo_path": "/not/indexed/here"},
    )

    assert result["success"] is True
    assert result["results"]["total_count"] == 0


def test_calculate_cyclomatic_complexity_returns_function_metrics(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "calculate_cyclomatic_complexity",
        {
            "function_name": "branchy",
            "path": "analysis_fixture.py",
            "repo_path": str(mcp_graph["repo"]),
        },
    )

    assert result["success"] is True
    assert result["results"]["function_name"] == "branchy"
    assert result["results"]["complexity"] > 1


def test_calculate_cyclomatic_complexity_returns_none_for_unknown_file(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "calculate_cyclomatic_complexity",
        {"function_name": "branchy", "path": "/not/a/file.py"},
    )

    assert result["success"] is True
    assert result["results"] is None


def test_find_most_complex_functions_ranks_fixture_methods(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "find_most_complex_functions",
        {"repo_path": str(mcp_graph["repo"]), "limit": 5},
    )

    assert result["success"] is True
    assert result["results"][0]["function_name"] == "branchy"
    assert result["results"][0]["complexity"] > 1


def test_find_most_complex_functions_returns_no_rows_for_unknown_repository_filter(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "find_most_complex_functions",
        {"repo_path": "/not/indexed/here"},
    )

    assert result["success"] is True
    assert result["results"] == []


def test_find_java_spring_endpoints_returns_parsed_endpoint(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "find_java_spring_endpoints",
        {"http_method": "get", "path_pattern": "/orders", "repo_path": str(mcp_graph["repo"])},
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["endpoints"][0]["handler"] == "getOrders"
    assert result["endpoints"][0]["method"] == "GET"
    assert "/orders" in result["endpoints"][0]["path"]


def test_find_java_spring_endpoints_returns_no_rows_for_unknown_repository_filter(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "find_java_spring_endpoints",
        {"repo_path": "/not/indexed/here"},
    )

    assert result["success"] is True
    assert result["count"] == 0


def test_find_java_spring_beans_returns_service_stereotype(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "find_java_spring_beans",
        {"stereotype": "service", "repo_path": str(mcp_graph["repo"])},
    )
    if "error" in result:
        assert "spring_stereotype" in result["error"]
        result = _call(
            _falkor_server(mcp_graph),
            "find_java_spring_beans",
            {"stereotype": "service", "repo_path": str(mcp_graph["repo"])},
        )

    assert result["success"] is True
    assert any(row["name"] == "OrderService" and row["stereotype"] == "SERVICE" for row in result["beans"])


def test_find_java_spring_beans_returns_no_rows_for_unknown_stereotype(mcp_graph):
    server = mcp_graph["falkor_server"] or mcp_graph["server"]
    result = _call(
        server,
        "find_java_spring_beans",
        {"stereotype": "not-a-stereotype", "repo_path": str(mcp_graph["repo"])},
    )

    if "error" in result and "spring_stereotype" in result["error"]:
        pytest.skip("Kùzu cannot bind the Spring stereotype property on the current graph schema")
    assert result["success"] is True
    assert result["count"] == 0


def test_find_datasource_nodes_returns_disposable_fixture_metadata(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "find_datasource_nodes",
        {"kind": "mysql", "include_columns": True},
    )
    if "error" in result:
        assert "not in scope" in result["error"]
        result = _call(
            _falkor_server(mcp_graph),
            "find_datasource_nodes",
            {"kind": "mysql", "include_columns": True},
        )

    assert result["success"] is True
    assert result["count"] == 1
    datasource = result["datasources"][0]
    assert datasource["datasource"] == "fixture-mysql"
    assert datasource["tables"] == ["fixture-mysql.orders"]
    assert datasource["columns"]["fixture-mysql.orders"][0]["name"] == "id"


def test_find_datasource_nodes_returns_empty_for_unknown_kind_filter(mcp_graph):
    server = mcp_graph["falkor_server"] or mcp_graph["server"]
    result = _call(
        server,
        "find_datasource_nodes",
        {"kind": "not-a-kind"},
    )

    if "error" in result and "not in scope" in result["error"]:
        pytest.skip("Kùzu cannot bind the datasource table query on the current backend")
    assert result["success"] is True
    assert result["count"] == 0


def test_generate_report_writes_a_scoped_report(mcp_graph):
    output_path = mcp_graph["repo"] / "analysis-report.md"
    result = _call(
        mcp_graph["server"],
        "generate_report",
        {"output_path": str(output_path), "include_java": True},
    )

    assert result["status"] == "ok"
    assert output_path.is_file()
    assert output_path.read_text(encoding="utf-8") == result["report"]
    assert "# CGC Report" in result["report"]
    assert "OrderController.java" in result["report"] or "branchy" in result["report"]


def test_generate_report_rejects_output_outside_allowed_roots(mcp_graph):
    output_path = mcp_graph["root"].parent / f"{mcp_graph['root'].name}-outside-report.md"
    assert not output_path.exists()
    result = _call(
        mcp_graph["server"],
        "generate_report",
        {"output_path": str(output_path)},
    )

    assert "error" in result
    assert "outside allowed roots" in result["error"]
    assert not output_path.exists()


def test_visualize_graph_query_encodes_a_read_query(mcp_graph):
    query = "MATCH (f:Function) RETURN f.name AS name LIMIT 5"
    result = _call(mcp_graph["server"], "visualize_graph_query", {"cypher_query": query})

    assert result["success"] is True
    parsed = urlparse(result["visualization_url"])
    assert parsed.path == "/index.html"
    assert parse_qs(parsed.query)["cypher_query"] == [query]


def test_visualize_graph_query_rejects_write_query(mcp_graph):
    result = _call(
        mcp_graph["server"],
        "visualize_graph_query",
        {"cypher_query": "MATCH (n) DETACH DELETE n"},
    )

    assert "error" in result
    assert "read-only" in result["error"].lower()
