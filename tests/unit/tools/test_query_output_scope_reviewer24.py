"""Regressions for bounded and repository-scoped H0 outputs."""

from contextlib import contextmanager

import pytest

from codegraphcontext.tools.code_finder import CodeFinder
from codegraphcontext.server import _pagination_retry_metadata
from codegraphcontext.tools.handlers.analysis_handlers import (
    analyze_code_relationships,
    find_java_spring_beans,
)


@pytest.fixture(autouse=True)
def _skip_index_readiness_for_h0_query_doubles(monkeypatch):
    """Keep H0 query-contract fakes independent of the job-state gate."""
    @contextmanager
    def _read_session(_self, driver, *args, **kwargs):
        with driver.session(*args, **kwargs) as session:
            yield session

    monkeypatch.setattr(CodeFinder, "_read_session", _read_session)


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def data(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _Session:
    def __init__(self, recorder, rows=()):
        self.recorder = recorder
        self.rows = rows

    def run(self, query, **params):
        self.recorder.append((query, params))
        return _Result(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Driver:
    def __init__(self, recorder, rows=()):
        self.recorder = recorder
        self.rows = rows

    def session(self):
        return _Session(self.recorder, self.rows)


class _DB:
    def __init__(self, recorder, rows=()):
        self.recorder = recorder
        self.rows = rows

    def get_driver(self, *args, **kwargs):
        return _Driver(self.recorder, self.rows)


def test_dead_code_scopes_callers_for_both_confidence_queries():
    calls = []
    finder = CodeFinder(_DB(calls))

    finder.find_dead_code(repo_path="/work/repo", include_low_confidence=True)

    assert len(calls) == 2
    assert all("caller.path STARTS WITH $repo_path" in query for query, _ in calls)
    assert all(params["repo_path"] == "/work/repo/" for _, params in calls)


def test_spring_bean_injection_count_scopes_injector_source():
    calls = []
    finder = CodeFinder(_DB(calls))

    response = find_java_spring_beans(finder, repo_path="/work/repo")

    query, params = calls[0]
    assert response["success"] is True
    assert "OPTIONAL MATCH (injector)-[inj:INJECTS]->(c)" in query
    assert "injector.path STARTS WITH $repo_path" in query
    assert params["repo_path"] == "/work/repo/"


def test_transitive_call_scan_reports_a_candidate_window_and_continuation():
    repo = "/work/repo/"
    edge_rows = [
        {
            "caller_name": f"caller{index}",
            "caller_path": f"{repo}{index}.py",
            "callee_name": "target",
            "callee_path": f"{repo}target.py",
            "line": index,
            "_caller_uid": f"caller-{index}",
            "_callee_uid": "target",
        }
        for index in range(501)
    ]
    calls = []
    finder = CodeFinder(_DB(calls, edge_rows))

    page = finder.find_all_callers(
        "target", repo_path=repo, offset=0, return_metadata=True
    )

    query, params = calls[0]
    assert "SKIP $candidate_offset" in query
    assert 'all(path_node IN nodes(p) WHERE path_node.path STARTS WITH "/work/repo/")' in query
    assert params["candidate_limit"] == 501
    assert params["candidate_offset"] == 0
    assert len(page["results"]) == 500
    assert page["truncated"] is True
    assert page["next_offset"] == 500
    assert page["offset"] == 0
    assert page["page_size"] == 500


def test_transitive_call_pages_use_stable_distinct_edge_rows():
    calls = []
    finder = CodeFinder(_DB(calls))

    finder.find_all_callers("target", repo_path="/work/repo", page_size=3)

    query, params = calls[0]
    assert "WITH DISTINCT" in query
    assert "_caller_uid" in query and "_callee_uid" in query
    assert "ORDER BY _caller_path" in query
    assert "SKIP $candidate_offset LIMIT $candidate_limit" in query
    assert params["candidate_limit"] == 4


def test_transitive_call_scan_reports_when_continuation_window_is_exhausted():
    rows = [
        {
            "caller_name": f"caller{index}",
            "caller_path": f"/work/repo/{index}.py",
            "callee_name": "target",
            "callee_path": "/work/repo/target.py",
            "line": index,
            "_caller_uid": f"caller-{index}",
            "_callee_uid": "target",
        }
        for index in range(501)
    ]
    finder = CodeFinder(_DB([], rows))

    page = finder.find_all_callers(
        "target", repo_path="/work/repo", offset=99_999, return_metadata=True
    )

    assert page["truncated"] is True
    assert page["next_offset"] is None
    assert page["continuation_limit_reached"] is True


def test_relationship_handler_exposes_continuation_metadata():
    class _RelationshipFinder:
        def analyze_code_relationships(self, *args, **kwargs):
            assert kwargs["offset"] == 500
            return {
                "query_type": "find_all_callers",
                "results": [],
                "truncated": True,
                "result_limit": 500,
                "next_offset": 1000,
                "continuation_limit_reached": False,
            }

    response = analyze_code_relationships(
        _RelationshipFinder(),
        query_type="find_all_callers",
        target="target",
        offset=500,
    )

    assert response["truncated"] is True
    assert response["next_offset"] == 1000
    assert response["result_limit"] == 500
    assert response["continuation_limit_reached"] is False


def test_relationship_handler_preserves_page_inputs_for_limiter_retries():
    class _RelationshipFinder:
        def analyze_code_relationships(self, *args, **kwargs):
            assert kwargs["offset"] == 20
            assert kwargs["page_size"] == 16
            return {
                "query_type": "find_all_callers",
                "results": [],
                "truncated": True,
                "offset": 20,
                "page_size": 16,
                "result_limit": 16,
                "next_offset": 36,
                "continuation_limit_reached": False,
            }

    response = analyze_code_relationships(
        _RelationshipFinder(),
        query_type="find_all_callers",
        target="target",
        offset=20,
        page_size=16,
    )

    assert response["offset"] == 20
    assert response["page_size"] == 16
    assert response["result_limit"] == 16
    assert response["next_offset"] == 36
    assert _pagination_retry_metadata(response) == {"offset": 20, "page_size": 8}
