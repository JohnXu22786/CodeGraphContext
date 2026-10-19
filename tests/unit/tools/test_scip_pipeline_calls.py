"""Tests for SCIP pipeline post-processing (CALLS resolution pass)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codegraphcontext.core.jobs import JobManager
from codegraphcontext.tools.indexing.persistence import writer as writer_module
from codegraphcontext.tools.indexing.persistence.writer import GraphWriter
from codegraphcontext.tools.indexing.scip_pipeline import run_scip_index_async


class _FakeScipIndexer:
    def run(self, _project_path: Path, _lang: str, output_dir: Path) -> Path:
        scip_file = output_dir / "index.scip"
        scip_file.write_bytes(b"fake")
        return scip_file


class _FakeScipIndexParser:
    files_data: dict[str, dict]

    def parse(self, _index_scip_path: Path, _project_path: Path) -> dict:
        return {"files": self.files_data}


class _FakeParser:
    def parse(self, path: Path, _is_dependency: bool, index_source: bool = False) -> dict:
        return {
            "path": str(path),
            "functions": [{"name": "main", "line_number": 1}],
            "classes": [],
            "imports": [],
            "variables": [],
            "function_calls": [
                {
                    "caller_name": "main",
                    "caller_line_number": 1,
                    "called_name": "helper",
                    "line_number": 2,
                    "full_name": "helper",
                    "args": [],
                }
            ],
            "function_calls_scip": [],
            "module_level_calls_scip": [],
        }


@pytest.mark.asyncio
async def test_scip_pipeline_runs_build_function_call_groups(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked_file = repo / "app.py"
    tracked_file.write_text("def main():\n    helper()\n", encoding="utf-8")

    fake_parser_mod = SimpleNamespace(
        ScipIndexer=_FakeScipIndexer,
        ScipIndexParser=_FakeScipIndexParser,
    )
    _FakeScipIndexParser.files_data = {
        str(tracked_file.resolve()): {
            "path": str(tracked_file.resolve()),
            "functions": [{"name": "main", "line_number": 1}],
            "classes": [],
            "imports": [],
            "function_calls_scip": [],
            "module_level_calls_scip": [],
        },
    }

    writer = MagicMock()
    job_manager = MagicMock()
    fake_groups = ([{"caller_name": "main"}], [], [], [], [], [], [], [], [], [])

    with patch(
        "codegraphcontext.tools.indexing.scip_pipeline.pre_scan_for_imports",
        return_value={},
    ), patch(
        "codegraphcontext.tools.indexing.scip_pipeline.build_function_call_groups",
        return_value=fake_groups,
    ) as mock_build_calls:
        await run_scip_index_async(
            repo,
            is_dependency=False,
            job_id="job-1",
            lang="python",
            writer=writer,
            job_manager=job_manager,
            parsers_keys={".py"},
            get_parser=lambda _suffix: _FakeParser(),
            scip_indexer_mod=fake_parser_mod,
        )

    mock_build_calls.assert_called_once()
    all_file_data, imports_map, file_class_lookup = mock_build_calls.call_args[0]
    assert file_class_lookup is None
    assert imports_map == {}
    assert len(all_file_data) == 1
    assert all_file_data[0].get("function_calls")

    writer.write_function_call_groups.assert_called_once_with(*fake_groups)
    writer.write_scip_call_edges.assert_called_once()
    job_manager.update_job.assert_any_call(
        "job-1", status_message="Resolving function CALLS edges..."
    )


def test_scip_call_edge_database_errors_propagate(monkeypatch):
    writer = GraphWriter(
        driver=MagicMock(),
        db_manager=SimpleNamespace(get_backend_type=lambda: "neo4j"),
    )
    session = MagicMock()
    session.run.side_effect = OSError("database unavailable")
    monkeypatch.setattr(
        writer_module,
        "execute_write_operation",
        lambda _driver, _backend, work: work(session),
    )

    with pytest.raises(OSError, match="database unavailable"):
        writer.write_scip_call_edges(
            {
                "app.py": {
                    "function_calls_scip": [
                        {
                            "caller_symbol": "app/main",
                            "caller_file": "/repo/app.py",
                            "caller_line": 1,
                            "callee_name": "helper",
                            "callee_file": "/repo/lib.py",
                            "ref_line": 2,
                        }
                    ],
                    "module_level_calls_scip": [],
                }
            },
            lambda symbol: symbol,
        )


@pytest.mark.asyncio
async def test_cancel_during_scip_call_edge_write_does_not_publish_ready(tmp_path, monkeypatch):
    import threading

    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def main(): pass\n", encoding="utf-8")
    _FakeScipIndexParser.files_data = {
        str(source.resolve()): {
            "path": str(source.resolve()),
            "functions": [{"name": "main", "line_number": 1}],
            "classes": [],
            "imports": [],
            "function_calls_scip": [],
            "module_level_calls_scip": [],
        },
    }
    fake_parser_mod = SimpleNamespace(
        ScipIndexer=_FakeScipIndexer,
        ScipIndexParser=_FakeScipIndexParser,
    )
    entered = threading.Event()
    release = threading.Event()
    writer = MagicMock()

    def block_call_edge_write(*_args):
        entered.set()
        assert release.wait(timeout=2)

    writer.write_scip_call_edges.side_effect = block_call_edge_write
    monkeypatch.setattr(
        "codegraphcontext.tools.indexing.scip_pipeline.pre_scan_for_imports",
        lambda *_args: {},
    )
    monkeypatch.setattr(
        "codegraphcontext.tools.indexing.scip_pipeline.build_function_call_groups",
        lambda *_args: ([], [], [], [], [], [], [], [], [], []),
    )
    task = asyncio.create_task(
        run_scip_index_async(
            repo,
            is_dependency=False,
            job_id=None,
            lang="python",
            writer=writer,
            job_manager=JobManager(),
            parsers_keys={".py"},
            get_parser=lambda _suffix: _FakeParser(),
            scip_indexer_mod=fake_parser_mod,
        )
    )
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0.02)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    writer.write_scip_call_edges.assert_called_once()
    writer.mark_repository_ready.assert_not_called()


@pytest.mark.asyncio
async def test_scip_fails_if_file_and_minimal_node_writes_both_fail(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def main(): pass\n", encoding="utf-8")
    _FakeScipIndexParser.files_data = {
        str(source.resolve()): {
            "path": str(source.resolve()),
            "functions": [{"name": "main", "line_number": 1}],
            "classes": [],
            "imports": [],
        },
    }
    fake_parser_mod = SimpleNamespace(
        ScipIndexer=_FakeScipIndexer,
        ScipIndexParser=_FakeScipIndexParser,
    )
    writer = MagicMock()
    writer.add_file_to_graph.side_effect = RuntimeError("SCIP graph write failed")
    writer.add_minimal_file_node.side_effect = RuntimeError("minimal node write failed")

    with patch(
        "codegraphcontext.tools.indexing.scip_pipeline.pre_scan_for_imports",
        return_value={},
    ), pytest.raises(RuntimeError, match="All 1 SCIP file write attempts failed"):
        await run_scip_index_async(
            repo,
            is_dependency=False,
            job_id=None,
            lang="python",
            writer=writer,
            job_manager=JobManager(),
            parsers_keys={".py"},
            get_parser=lambda _suffix: None,
            scip_indexer_mod=fake_parser_mod,
        )

    writer.add_file_to_graph.assert_called_once()
    writer.add_minimal_file_node.assert_called_once()


@pytest.mark.asyncio
async def test_scip_file_write_failure_is_incomplete_even_if_minimal_fallback_succeeds(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def main(): pass\n", encoding="utf-8")
    _FakeScipIndexParser.files_data = {
        str(source.resolve()): {
            "path": str(source.resolve()),
            "functions": [{"name": "main", "line_number": 1}],
            "classes": [],
            "imports": [],
        },
    }
    fake_parser_mod = SimpleNamespace(
        ScipIndexer=_FakeScipIndexer,
        ScipIndexParser=_FakeScipIndexParser,
    )
    writer = MagicMock()
    writer.add_file_to_graph.side_effect = RuntimeError("SCIP graph write failed")

    with patch(
        "codegraphcontext.tools.indexing.scip_pipeline.pre_scan_for_imports",
        return_value={},
    ), pytest.raises(RuntimeError, match="All 1 SCIP file write attempts failed"):
        await run_scip_index_async(
            repo,
            is_dependency=False,
            job_id=None,
            lang="python",
            writer=writer,
            job_manager=JobManager(),
            parsers_keys={".py"},
            get_parser=lambda _suffix: None,
            scip_indexer_mod=fake_parser_mod,
        )

    writer.add_minimal_file_node.assert_called_once_with(source.resolve(), repo.resolve(), False)
    writer.mark_repository_ready.assert_not_called()
    writer.write_scip_call_edges.assert_not_called()


@pytest.mark.asyncio
async def test_supplementary_write_failure_is_incomplete_if_minimal_fallback_succeeds(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "supplementary.py"
    source.write_text("def main(): pass\n", encoding="utf-8")
    _FakeScipIndexParser.files_data = {}
    fake_parser_mod = SimpleNamespace(
        ScipIndexer=_FakeScipIndexer,
        ScipIndexParser=_FakeScipIndexParser,
    )
    writer = MagicMock()
    writer.add_file_to_graph.side_effect = RuntimeError("Tree-sitter graph write failed")

    with patch(
        "codegraphcontext.tools.indexing.scip_pipeline.pre_scan_for_imports",
        return_value={},
    ), patch(
        "codegraphcontext.tools.indexing.discovery.discover_files_to_index",
        return_value=([source], []),
    ), pytest.raises(RuntimeError, match="All 1 SCIP file write attempts failed"):
        await run_scip_index_async(
            repo,
            is_dependency=False,
            job_id=None,
            lang="python",
            writer=writer,
            job_manager=JobManager(),
            parsers_keys={".py"},
            get_parser=lambda _suffix: _FakeParser(),
            scip_indexer_mod=fake_parser_mod,
        )

    writer.add_minimal_file_node.assert_called_once_with(source, repo.resolve(), False)
    writer.mark_repository_ready.assert_not_called()
    writer.write_scip_call_edges.assert_not_called()


@pytest.mark.asyncio
async def test_scip_fails_when_one_file_is_unwritten_after_another_succeeds(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    good_file = repo / "good.py"
    failed_file = repo / "failed.py"
    good_file.write_text("def good(): pass\n", encoding="utf-8")
    failed_file.write_text("def failed(): pass\n", encoding="utf-8")
    _FakeScipIndexParser.files_data = {
        str(good_file.resolve()): {
            "path": str(good_file.resolve()),
            "functions": [{"name": "good", "line_number": 1}],
            "classes": [],
            "imports": [],
        },
        str(failed_file.resolve()): {
            "path": str(failed_file.resolve()),
            "functions": [{"name": "failed", "line_number": 1}],
            "classes": [],
            "imports": [],
        },
    }
    fake_parser_mod = SimpleNamespace(
        ScipIndexer=_FakeScipIndexer,
        ScipIndexParser=_FakeScipIndexParser,
    )
    writer = MagicMock()

    def selective_write(file_data, *_args, **_kwargs):
        if Path(file_data["path"]).name == failed_file.name:
            raise RuntimeError("SCIP graph write failed")

    def fail_minimal_for_required_file(path: Path, *_args, **_kwargs):
        if path.name == failed_file.name:
            raise RuntimeError("minimal node write failed")

    writer.add_file_to_graph.side_effect = selective_write
    writer.add_minimal_file_node.side_effect = fail_minimal_for_required_file

    with patch(
        "codegraphcontext.tools.indexing.scip_pipeline.pre_scan_for_imports",
        return_value={},
    ), pytest.raises(RuntimeError, match="1 of 2 SCIP file writes failed"):
        await run_scip_index_async(
            repo,
            is_dependency=False,
            job_id=None,
            lang="python",
            writer=writer,
            job_manager=JobManager(),
            parsers_keys={".py"},
            get_parser=lambda _suffix: None,
            scip_indexer_mod=fake_parser_mod,
        )

    assert writer.add_file_to_graph.call_count == 2
    writer.add_minimal_file_node.assert_called_once_with(failed_file.resolve(), repo.resolve(), False)
    writer.write_scip_call_edges.assert_not_called()
