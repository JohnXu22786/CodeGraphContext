import threading
from unittest.mock import MagicMock, patch

import pytest

from codegraphcontext.core.jobs import JobManager, JobStatus
import codegraphcontext.tools.graph_builder as graph_builder_module
from codegraphcontext.tools import scip_indexer
from codegraphcontext.tools.graph_builder import GraphBuilder
from codegraphcontext.utils import tree_sitter_manager as manager


def builder():
    result = GraphBuilder.__new__(GraphBuilder)
    result.parsers = {'.py': 'python'}
    result.generic_extensions = set()
    result.generic_filenames = set()
    result._parsed_cache = threading.local()
    result._writer = MagicMock()
    result.job_manager = JobManager()
    return result


def test_supported_parser_failure_is_not_unsupported(tmp_path):
    target = tmp_path / 'sample.py'
    target.write_text('def f(): pass')
    with patch('codegraphcontext.tools.graph_builder.TreeSitterParser', side_effect=PermissionError('cache owned by uid 0')):
        data = builder().parse_file(tmp_path, target)
    assert data['parse_failed'] is True
    assert not data.get('unsupported')
    assert 'cache owned by uid 0' in data['error']


@pytest.mark.asyncio
async def test_initialization_failure_cannot_complete_a_hollow_graph(tmp_path):
    (tmp_path / 'sample.py').write_text('def f(): pass')
    indexer = builder()
    job_id = indexer.job_manager.create_job(str(tmp_path))
    with patch('codegraphcontext.tools.graph_builder.TreeSitterParser', side_effect=PermissionError('cache owned by uid 0')), patch(
        'codegraphcontext.tools.graph_builder.get_config_value', return_value='false'
    ):
        await indexer.build_graph_from_path_async(tmp_path, job_id=job_id)
    job = indexer.job_manager.get_job(job_id)
    assert job.status == JobStatus.FAILED
    assert 'cache owned by uid 0' in job.errors[0]
    indexer._writer.add_file_to_graph.assert_not_called()


@pytest.mark.asyncio
async def test_non_runtime_scip_failure_falls_back_to_tree_sitter(tmp_path, monkeypatch):
    indexer = builder()
    indexer.last_call_resolution_diagnostics = []
    indexer.last_index_summary = {}
    job_id = indexer.job_manager.create_job(str(tmp_path))
    tree_sitter_calls = []

    async def fail_scip(*_args, **_kwargs):
        raise ValueError("malformed SCIP output")

    async def run_tree_sitter(*_args, **_kwargs):
        tree_sitter_calls.append(True)
        indexer.job_manager.complete_job(job_id)

    monkeypatch.setattr(graph_builder_module, "get_config_value", lambda *_args, **_kwargs: "true")
    monkeypatch.setattr(scip_indexer, "detect_project_lang", lambda *_args: "python")
    monkeypatch.setattr(scip_indexer, "is_scip_available", lambda *_args: True)
    monkeypatch.setattr(indexer, "_build_graph_from_scip", fail_scip)
    monkeypatch.setattr(graph_builder_module, "run_tree_sitter_index_async", run_tree_sitter)

    await indexer.build_graph_from_path_async(tmp_path, job_id=job_id)

    assert tree_sitter_calls == [True]
    assert indexer.job_manager.get_job(job_id).status == JobStatus.COMPLETED


def test_dependency_import_does_not_download_unrelated_python_grammar(monkeypatch):
    import tree_sitter_language_pack
    for name in ('_Language', '_Parser', '_get_language'):
        monkeypatch.setattr(manager, name, None)
    loader = MagicMock(side_effect=PermissionError('manifest download denied'))
    monkeypatch.setattr(tree_sitter_language_pack, 'get_language', loader)
    prefetch = MagicMock()
    monkeypatch.setattr(tree_sitter_language_pack, 'prefetch', prefetch)
    manager._load_tree_sitter_dependencies()
    loader.assert_not_called()
    prefetch.assert_not_called()
    with pytest.raises(RuntimeError, match='manifest download denied') as failure:
        manager.TreeSitterManager().get_language_safe('java')
    assert isinstance(failure.value.__cause__, PermissionError)
    loader.assert_called_once_with('java')
    prefetch.assert_called_once_with(['java'])
