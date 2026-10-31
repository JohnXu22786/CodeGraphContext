import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import typer

from codegraphcontext.cli import cli_helpers
from codegraphcontext.core.index_generation import generation_marker_path
from codegraphcontext.core.index_generation import read_generation_state, write_generation_state
from codegraphcontext.cli.snapshot_commands import effective_snapshot_identity


async def _failing_index(*_args, **_kwargs):
    raise RuntimeError("boom")


async def _successful_index(*_args, **_kwargs):
    return None


def _services(tmp_path):
    db_manager = MagicMock()
    code_finder = MagicMock()
    code_finder.list_indexed_repositories.return_value = []
    ctx = SimpleNamespace(cgcignore_path=None, mode="global")
    return db_manager, MagicMock(), code_finder, ctx


def test_index_helper_exits_nonzero_on_indexing_failure(tmp_path):
    services = _services(tmp_path)
    services[1].estimate_processing_time.return_value = (2, 0.1)
    with (
        patch.object(cli_helpers, "_initialize_services", return_value=services),
        patch.object(cli_helpers, "_run_index_with_progress", side_effect=_failing_index),
        pytest.raises(typer.Exit) as exc_info,
    ):
        cli_helpers.index_helper(str(tmp_path))

    assert exc_info.value.exit_code == 1
    services[0].close_driver.assert_called_once()


def test_reindex_helper_exits_nonzero_on_indexing_failure(tmp_path):
    services = _services(tmp_path)
    services[0].db_path = str(tmp_path / "graph")
    services[0].get_backend_type.return_value = "ladybugdb"
    with (
        patch.object(cli_helpers, "_initialize_services", return_value=services),
        patch.object(cli_helpers, "_run_index_with_progress", side_effect=_failing_index),
        pytest.raises(typer.Exit) as exc_info,
    ):
        cli_helpers.reindex_helper(str(tmp_path))

    assert exc_info.value.exit_code == 1
    services[0].close_driver.assert_called_once()
    assert json.loads(generation_marker_path(services[0].db_path).read_text())["state"] == "building"


def test_reindex_helper_marks_generation_complete_after_success(tmp_path):
    services = _services(tmp_path)
    services[3].mode = "per-repo"
    services[3].is_local = True
    services[0].db_path = str(tmp_path / "graph")
    services[0].get_backend_type.return_value = "ladybugdb"
    with (
        patch.object(cli_helpers, "_initialize_services", return_value=services),
        patch.object(cli_helpers, "_run_index_with_progress", side_effect=_successful_index),
        patch.object(cli_helpers, "_print_index_execution_summary"),
    ):
        cli_helpers.reindex_helper(str(tmp_path))

    assert json.loads(generation_marker_path(services[0].db_path).read_text())["state"] == "complete"


def test_index_helper_does_not_skip_directory_with_partial_index(tmp_path):
    """A prior one-file scan must not mark its parent directory complete."""
    (tmp_path / "indexed.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "missing.py").write_text("pass\n", encoding="utf-8")
    services = _services(tmp_path)
    services[1].estimate_processing_time.return_value = (2, 0.1)
    services[2].list_indexed_repositories.return_value = [{"path": str(tmp_path)}]

    session = MagicMock()
    session.run.return_value.single.return_value = {"file_count": 1}
    services[0].get_driver.return_value.session.return_value.__enter__.return_value = session

    with (
        patch.object(cli_helpers, "_initialize_services", return_value=services),
        patch.object(cli_helpers, "_run_index_with_progress") as run_index,
        patch.object(cli_helpers, "_print_index_execution_summary"),
    ):
        cli_helpers.index_helper(str(tmp_path))

    run_index.assert_called_once()


def test_index_helper_clears_repository_before_rebuilding_stale_generation(tmp_path):
    services = _services(tmp_path)
    services[3].mode = "per-repo"
    services[3].is_local = True
    services[3].mode = "per-repo"
    services[3].is_local = True
    db_path = str(tmp_path / "graph")
    services[0].db_path = db_path
    services[0].get_backend_type.return_value = "ladybugdb"
    services[1].estimate_processing_time.return_value = (1, 0.1)
    services[2].list_indexed_repositories.return_value = [{"path": str(tmp_path)}]
    services[3].database = "ladybugdb"
    services[3].db_path = db_path
    session = MagicMock()
    session.run.return_value.single.return_value = {"file_count": 1}
    services[0].get_driver.return_value.session.return_value.__enter__.return_value = session
    identity, _root = effective_snapshot_identity(services[3], tmp_path)
    write_generation_state(
        db_path,
        {**identity, "storage_key": "previous-generation"},
        state="complete",
        generation_id="old",
    )

    with (
        patch.object(cli_helpers, "_initialize_services", return_value=services),
        patch.object(cli_helpers, "_run_index_with_progress", side_effect=_successful_index),
        patch.object(cli_helpers, "_print_index_execution_summary"),
    ):
        cli_helpers.index_helper(str(tmp_path))

    services[1].delete_repository_from_graph.assert_called_once_with(str(tmp_path))
    assert read_generation_state(db_path, identity)["state"] == "complete"
