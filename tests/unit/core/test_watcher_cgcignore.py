from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from codegraphcontext.core.watcher import RepositoryEventHandler


def _graph_builder():
    graph_builder = MagicMock()
    graph_builder.parsers = {".py": "python"}
    graph_builder.get_importer_file_paths.return_value = set()
    graph_builder.generic_extensions = {".md"}
    graph_builder.generic_filenames = {"Dockerfile"}
    graph_builder.pre_scan_imports.return_value = {}
    graph_builder.parse_file.return_value = {"path": "parsed.py"}
    graph_builder.link_function_calls.return_value = None
    graph_builder.link_inheritance.return_value = None
    graph_builder.get_caller_file_paths.return_value = set()
    graph_builder.get_inheritance_neighbor_paths.return_value = set()
    graph_builder.get_repo_class_lookup.return_value = {}
    graph_builder.update_file_in_graph.return_value = None
    return graph_builder


def test_watcher_skips_ignored_modified_event(tmp_path: Path):
    repo = tmp_path / "repo"
    ignored_dir = repo / "bin"
    src_dir = repo / "src"
    ignored_dir.mkdir(parents=True)
    src_dir.mkdir()
    (repo / ".cgcignore").write_text("**/bin/\n", encoding="utf-8")
    ignored_file = ignored_dir / "generated.py"
    tracked_file = src_dir / "app.py"
    ignored_file.write_text("print('ignored')\n", encoding="utf-8")
    tracked_file.write_text("print('tracked')\n", encoding="utf-8")

    handler = RepositoryEventHandler(_graph_builder(), repo, perform_initial_scan=False)
    generic_file = repo / "README.md"
    generic_file.write_text("# tracked generic file\n", encoding="utf-8")

    with patch.object(handler, "_debounce") as debounce:
        handler.on_modified(SimpleNamespace(is_directory=False, src_path=str(ignored_file)))
        debounce.assert_not_called()

        handler.on_modified(SimpleNamespace(is_directory=False, src_path=str(tracked_file)))
        debounce.assert_called_once()

        handler.on_created(SimpleNamespace(is_directory=False, src_path=str(generic_file)))
        assert debounce.call_count == 2, "creating an indexed generic file should queue an update"

        handler.on_modified(SimpleNamespace(is_directory=False, src_path=str(generic_file)))
        assert debounce.call_count == 3, "editing an indexed generic file should queue an update"


def test_watcher_filters_large_ignored_deletion_batch_before_queueing(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / ".cgcignore").write_text("**/bin/\n", encoding="utf-8")
    graph_builder = _graph_builder()
    graph_builder.generic_extensions = {".md"}
    graph_builder.generic_filenames = {"Dockerfile"}
    handler = RepositoryEventHandler(graph_builder, repo, perform_initial_scan=False)

    # Deleted paths no longer exist, so filtering must use the path and ignore
    # rules before adding any work, not rely on is_file().
    with patch.object(handler, "_debounce") as debounce:
        for index in range(1000):
            handler.on_deleted(
                SimpleNamespace(
                    is_directory=False,
                    src_path=str(repo / "bin" / f"generated_{index}.py"),
                )
            )

        debounce.assert_not_called()
        handler.on_deleted(SimpleNamespace(is_directory=False, src_path=str(repo / "README.md")))
        assert debounce.call_count == 1, "deleting a graph-indexed generic file should remain supported"


def test_watcher_initial_scan_filters_cgcignore(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / ".cgcignore").write_text("**/bin/\n", encoding="utf-8")
    ignored_file = repo / "bin" / "generated.py"
    tracked_file = repo / "src" / "app.py"
    ignored_file.write_text("print('ignored')\n", encoding="utf-8")
    tracked_file.write_text("print('tracked')\n", encoding="utf-8")

    graph_builder = _graph_builder()
    RepositoryEventHandler(graph_builder, repo, perform_initial_scan=True)

    parsed_paths = [call.args[1] for call in graph_builder.parse_file.call_args_list]
    assert parsed_paths == [tracked_file]
    pre_scan_paths = graph_builder.pre_scan_imports.call_args.args[0]
    assert pre_scan_paths == [tracked_file]


def test_watcher_incremental_reparse_filters_ignored_affected_files(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "obj").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / ".cgcignore").write_text("**/obj/\n", encoding="utf-8")
    changed_file = repo / "src" / "module.py"
    ignored_caller = repo / "obj" / "generated.py"
    changed_file.write_text("def module(): pass\n", encoding="utf-8")
    ignored_caller.write_text("def generated(): pass\n", encoding="utf-8")

    graph_builder = _graph_builder()
    graph_builder.get_caller_file_paths.return_value = {str(ignored_caller.resolve())}
    handler = RepositoryEventHandler(graph_builder, repo, perform_initial_scan=False)

    handler._handle_modification(str(changed_file))

    graph_builder.update_file_in_graph.assert_called_once()
    assert graph_builder.update_file_in_graph.call_args.args[0] == changed_file
    # Callers under cgcignore must not be pulled into incremental relink.
    graph_builder.delete_outgoing_calls_from_files.assert_not_called()
