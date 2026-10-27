"""Tests for issue #1097: cgc query propagates raw database exceptions."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest
import typer
from codegraphcontext.cli import cli_helpers
from codegraphcontext.server import MAX_TOOL_RESPONSE_BYTES


def _services():
    db_manager = MagicMock()
    db_manager.get_backend_type = lambda: "kuzudb"
    ctx = SimpleNamespace(mode="global")
    return db_manager, MagicMock(), MagicMock(), ctx


# ---------------------------------------------------------------------------
# _print_query_exception unit tests
# ---------------------------------------------------------------------------

class TestPrintQueryException:
    """Verify _print_query_exception surfaces the right message per backend."""

    def test_kuzu_parser_error_no_traceback(self, capsys):
        """KuzuDB RuntimeError with 'Parser exception' should print cleanly."""
        e = RuntimeError(
            "Parser exception: Invalid input <MATCH (n:Bad RETURN>: "
            "expected rule oC_SingleQuery (line: 1, offset: 21)"
        )
        cli_helpers._print_query_exception(e, "MATCH (n:Bad RETURN n")
        captured = cli_helpers.console.file  # rich Console, check via output below

    def test_kuzu_parser_error_shows_message(self, capsys):
        """Output must contain the raw parser exception text."""
        e = RuntimeError("Parser exception: Invalid input <MATCH")
        from io import StringIO
        from rich.console import Console
        buf = StringIO()
        original = cli_helpers.console
        cli_helpers.console = Console(file=buf, highlight=False)
        try:
            cli_helpers._print_query_exception(e, "MATCH (n RETURN n")
        finally:
            cli_helpers.console = original
        output = buf.getvalue()
        assert "Parser exception" in output
        assert "MATCH (n RETURN n" in output
        assert "Traceback" not in output

    def test_neo4j_error_shows_code_and_message(self):
        """Neo4j exceptions should show .code and .message attributes."""
        from io import StringIO
        from rich.console import Console

        FakeNeo4jError = type(
            "CypherSyntaxError",
            (Exception,),
            {"__module__": "neo4j.exceptions"},
        )

        e = FakeNeo4jError("Neo4j error")
        e.code = "Neo.ClientError.Statement.SyntaxError"
        e.message = "Invalid input 'RETURN': expected..."

        buf = StringIO()
        original = cli_helpers.console
        cli_helpers.console = Console(file=buf, highlight=False)
        try:
            cli_helpers._print_query_exception(e, "MATCH RETURN n")
        finally:
            cli_helpers.console = original

        output = buf.getvalue()
        assert "Neo.ClientError.Statement.SyntaxError" in output
        assert "Invalid input 'RETURN'" in output

    def test_falkordb_error_shows_message(self):
        """FalkorDB exceptions should show the database message."""
        from io import StringIO
        from rich.console import Console

        FakeFalkorError = type(
            "ResponseError",
            (Exception,),
            {"__module__": "falkordb.exceptions"},
        )

        e = FakeFalkorError("ERR query syntax error")

        buf = StringIO()
        original = cli_helpers.console
        cli_helpers.console = Console(file=buf, highlight=False)
        try:
            cli_helpers._print_query_exception(e, "MATCH (n RETURN n")
        finally:
            cli_helpers.console = original

        output = buf.getvalue()
        assert "ERR query syntax error" in output


# ---------------------------------------------------------------------------
# cypher_helper integration tests
# ---------------------------------------------------------------------------

class TestCypherHelperExceptionPropagation:
    """Verify cypher_helper calls _print_query_exception on failure."""

    def test_exits_nonzero_on_query_error(self, tmp_path):
        """cypher_helper must exit with code 1 when query raises."""
        services = _services()
        session_mock = MagicMock()
        session_mock.__enter__ = MagicMock(return_value=session_mock)
        session_mock.__exit__ = MagicMock(return_value=False)
        session_mock.run.side_effect = RuntimeError(
            "Parser exception: Invalid input"
        )
        services[0].get_driver.return_value.session.return_value = session_mock

        with (
            patch.object(cli_helpers, "_initialize_services", return_value=services),
            patch.object(cli_helpers, "_print_query_exception") as mock_print_exc,
            pytest.raises(typer.Exit) as exc_info,
        ):
            cli_helpers.cypher_helper("MATCH (n RETURN n")

        assert exc_info.value.exit_code == 1
        mock_print_exc.assert_called_once()
        call_args = mock_print_exc.call_args
        assert "MATCH (n RETURN n" in call_args[0]

    def test_print_query_exception_called_with_correct_query(self, tmp_path):
        """_print_query_exception must receive the original query string."""
        services = _services()
        session_mock = MagicMock()
        session_mock.__enter__ = MagicMock(return_value=session_mock)
        session_mock.__exit__ = MagicMock(return_value=False)
        bad_query = "MATCH (n:Broken RETURN n"
        session_mock.run.side_effect = RuntimeError("Parser exception")
        services[0].get_driver.return_value.session.return_value = session_mock

        with (
            patch.object(cli_helpers, "_initialize_services", return_value=services),
            patch.object(cli_helpers, "_print_query_exception") as mock_print_exc,
            pytest.raises(typer.Exit),
        ):
            cli_helpers.cypher_helper(bad_query)

        assert mock_print_exc.call_args[0][1] == bad_query

    def test_uses_scoped_paged_handler_and_caps_serialized_output(self, tmp_path):
        from io import StringIO
        from rich.console import Console

        services = _services()
        output = StringIO()
        query = "MATCH (f:Function) RETURN f.uid ORDER BY f.uid"
        large_result = {
            "success": True,
            "results": [{"source": "x" * (MAX_TOOL_RESPONSE_BYTES * 2)}],
        }
        with (
            patch.object(cli_helpers, "_initialize_services", return_value=services),
            patch(
                "codegraphcontext.tools.handlers.query_handlers.execute_cypher_query",
                return_value=large_result,
            ) as execute_query,
            patch.object(cli_helpers, "console", Console(file=output, highlight=False)),
        ):
            cli_helpers.cypher_helper(
                query,
                repo_path="/work/repo",
                offset=4,
                page_size=5,
            )

        assert execute_query.call_args.args[0] is services[0]
        assert execute_query.call_args.kwargs == {
            "cypher_query": query,
            "repo_path": "/work/repo",
            "global_scope": False,
            "offset": 4,
            "page_size": 5,
        }
        encoded = output.getvalue().encode("utf-8")
        assert len(encoded) <= MAX_TOOL_RESPONSE_BYTES
        assert '"truncated":true' in output.getvalue()


# ---------------------------------------------------------------------------
# cypher_helper_visual integration tests
# ---------------------------------------------------------------------------

class TestCypherHelperVisualExceptionPropagation:
    """Verify cypher_helper_visual also calls _print_query_exception."""

    def test_visual_exits_nonzero_on_query_error(self, tmp_path):
        services = _services()

        with (
            patch.object(cli_helpers, "_initialize_services", return_value=services),
            patch(
                "codegraphcontext.cli.visualizer.visualize_cypher_results",
                 side_effect=RuntimeError("Parser exception: bad query"),
            ),
            patch.object(cli_helpers, "_print_query_exception") as mock_print_exc,
            pytest.raises(typer.Exit) as exc_info,
        ):
            cli_helpers.cypher_helper_visual("MATCH (n RETURN n")

        assert exc_info.value.exit_code == 1
        mock_print_exc.assert_called_once()

    def test_visual_forwards_requested_page_window(self):
        services = _services()
        query = "MATCH (f:Function) RETURN f"

        with (
            patch.object(cli_helpers, "_initialize_services", return_value=services),
            patch(
                "codegraphcontext.cli.visualizer.visualize_cypher_results"
            ) as visualize,
        ):
            cli_helpers.cypher_helper_visual(
                query,
                repo_path="/work/repo",
                offset=12,
                page_size=7,
            )

        visualize.assert_called_once_with(
            query,
            repo_path="/work/repo",
            global_scope=False,
            offset=12,
            page_size=7,
        )

    def test_visualize_helper_puts_page_window_in_explore_url(self, monkeypatch):
        from io import StringIO
        from pathlib import Path
        from rich.console import Console

        services = _services()
        output = StringIO()
        monkeypatch.setattr(cli_helpers, "_initialize_services", lambda *_a, **_k: services)
        monkeypatch.setattr(cli_helpers, "set_db_manager", lambda *_a, **_k: None)
        monkeypatch.setattr(cli_helpers, "run_server", lambda **_kwargs: None)
        monkeypatch.setattr(Path, "exists", lambda _self: True)
        monkeypatch.setattr(Path, "is_file", lambda _self: True)
        monkeypatch.setattr("threading.Thread.start", lambda _self: None)
        monkeypatch.setattr(cli_helpers, "console", Console(file=output, highlight=False))

        cli_helpers.visualize_helper(
            repo_path="/work/repo",
            cypher_query="MATCH (f:Function) RETURN f AS n, null AS rel, null AS m",
            offset=12,
            page_size=7,
        )

        assert "offset=12" in output.getvalue()
        assert "page_size=7" in output.getvalue()

    def test_visualizer_receives_repository_scope(self):
        from codegraphcontext.cli import visualizer

        with patch.object(visualizer, "visualize_helper") as launch:
            visualizer.visualize_search_results(
                [], "Widget", search_type="name", repo_path="/work/repo"
            )

        assert launch.call_args.kwargs["repo_path"] == "/work/repo"

    def test_visualizer_forwards_search_page_window(self):
        from codegraphcontext.cli import visualizer

        with patch.object(visualizer, "visualize_helper") as launch:
            visualizer.visualize_search_results(
                [], "Widget", search_type="name", offset=12, page_size=7
            )
            visualizer.visualize_dependencies(
                [], "package", offset=8, page_size=6
            )
            visualizer.visualize_inheritance_tree(
                [], "Widget", offset=4, page_size=3
            )

        assert launch.call_args_list[0].kwargs["offset"] == 12
        assert launch.call_args_list[0].kwargs["page_size"] == 7
        assert launch.call_args_list[1].kwargs["offset"] == 8
        assert launch.call_args_list[1].kwargs["page_size"] == 6
        assert launch.call_args_list[2].kwargs["offset"] == 4
        assert launch.call_args_list[2].kwargs["page_size"] == 3

    def test_builtin_visualization_queries_accept_repository_scoping(self, monkeypatch):
        from codegraphcontext.cli import visualizer
        from codegraphcontext.tools.handlers.query_handlers import _scope_cypher_query

        calls = []
        monkeypatch.setattr(
            visualizer,
            "visualize_helper",
            lambda **kwargs: calls.append(kwargs),
        )
        repo_path = "/work/repo"

        visualizer.visualize_search_results(
            [{"type": "Function"}], "widget", search_type="name", repo_path=repo_path
        )
        visualizer.visualize_search_results(
            [{"type": "Function"}], "wid", search_type="pattern", repo_path=repo_path
        )
        visualizer.visualize_search_results(
            [{"type": "Function"}], "Function", search_type="type", repo_path=repo_path
        )
        visualizer.visualize_search_results(
            [{"type": "Module"}], "Module", search_type="type", repo_path=repo_path
        )
        visualizer.visualize_search_results(
            [{"type": "Module"}, {"type": "Import"}],
            "widget",
            search_type="name",
            repo_path=repo_path,
        )
        visualizer.visualize_call_graph(
            [], "widget", direction="outgoing", repo_path=repo_path
        )
        visualizer.visualize_call_graph(
            [], "widget", direction="incoming", repo_path=repo_path
        )
        visualizer.visualize_call_chain(
            [
                {
                    "function_chain": [
                        {"name": "start", "path": "/work/repo/start.py"},
                        {"name": "finish", "path": "/work/repo/finish.py"},
                    ],
                    "call_details": [{"call_line": 12}],
                }
            ],
            "start",
            "finish",
            repo_path=repo_path,
        )

        assert len(calls) == 8
        assert "MATCH (n:Function)" in calls[2]["cypher_query"]
        assert "n.name" not in calls[2]["cypher_query"]
        assert "MATCH (f:File)-[rel:IMPORTS]->(m:Module)" in calls[3]["cypher_query"]
        assert "m.name" not in calls[3]["cypher_query"]
        assert "called:Class" in calls[5]["cypher_query"]
        for call in calls:
            _scope_cypher_query(call["cypher_query"], call["repo_path"])

    def test_custom_query_visualization_preserves_explicit_global_scope(self):
        from codegraphcontext.cli import visualizer

        with patch.object(visualizer, "visualize_helper") as launch:
            visualizer.visualize_cypher_results(
                "MATCH (f:Function) RETURN f",
                repo_path="/work/repo",
                global_scope=True,
                offset=12,
                page_size=7,
            )

        assert launch.call_args.kwargs["repo_path"] == "/work/repo"
        assert launch.call_args.kwargs["global_scope"] is True
        assert launch.call_args.kwargs["offset"] == 12
        assert launch.call_args.kwargs["page_size"] == 7
