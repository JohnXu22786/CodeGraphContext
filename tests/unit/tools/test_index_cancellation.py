"""Cancellation and timeout guarantees for indexed repository jobs."""

import asyncio
import queue
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import codegraphcontext.tools.graph_builder as graph_builder_module
from codegraphcontext.api import mcp_sse
from codegraphcontext.core.database_embedded_kuzu import EmbeddedDriverWrapper, EmbeddedSessionWrapper
from codegraphcontext.core.database_falkordb import FalkorDBDriverWrapper
from codegraphcontext.core.database import Neo4jDriverWrapper
from codegraphcontext.core.database_kuzu import KuzuDBManager
from codegraphcontext.core.jobs import JobCancelledError, JobManager, JobStatus
from codegraphcontext.server import MCPServer
from codegraphcontext.tools.code_finder import CodeFinder, RepositoryIndexInProgressError
from codegraphcontext.tools.graph_builder import GraphBuilder
from codegraphcontext.tools.handlers import management_handlers, query_handlers
from codegraphcontext.tools.indexing import pipeline
from codegraphcontext.tools.indexing import scip_pipeline
from codegraphcontext.tools.indexing.persistence.utils import execute_write_operation


def _new_indexer(tmp_path, monkeypatch):
    pytest.importorskip("kuzu")
    monkeypatch.setenv("CGC_EMBEDDED_BUFFER_POOL_MB", "256")
    monkeypatch.setattr(graph_builder_module, "get_config_value", lambda *_args, **_kwargs: "false")
    monkeypatch.setattr(pipeline, "get_parallel_workers", lambda: 1)

    db_manager = KuzuDBManager(str(tmp_path / "database" / "graph"))
    driver = db_manager.get_driver()
    job_manager = JobManager()
    builder = GraphBuilder(db_manager, job_manager, asyncio.get_running_loop())
    builder.parsers = {".py": "python"}
    return db_manager, driver, job_manager, builder


def _new_repository(tmp_path):
    repo = tmp_path / "disposable-repository"
    repo.mkdir()
    (repo / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    return repo


def _minimal_server(job_manager):
    server = MCPServer.__new__(MCPServer)
    server.disabled_tools = set()
    server._context_note_pending = False
    server.discovered_child_contexts = []
    server.job_manager = job_manager
    return server


@pytest.mark.asyncio
async def test_tree_sitter_cancellation_drains_parallel_parse_workers(tmp_path, monkeypatch):
    repo = tmp_path / "parse-cancellation"
    repo.mkdir()
    first_file = repo / "first.py"
    second_file = repo / "second.py"
    first_file.write_text("first", encoding="utf-8")
    second_file.write_text("second", encoding="utf-8")

    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    cancellation_checked = threading.Event()
    cancellation_requested = threading.Event()
    second_finished = threading.Event()

    class JobManager:
        def raise_if_cancelled(self, _job_id):
            if cancellation_requested.is_set():
                cancellation_checked.set()
                raise JobCancelledError()

        def update_job(self, _job_id, **_kwargs):
            pass

    def parse_file(_repo_path, file, _is_dependency):
        if file == first_file:
            first_started.set()
            assert release_first.wait(timeout=3), "test did not release first parser"
            return {"path": str(file)}
        second_started.set()
        try:
            assert release_second.wait(timeout=3), "test did not release second parser"
            return {"path": str(file)}
        finally:
            second_finished.set()

    monkeypatch.setattr(pipeline, "get_parallel_workers", lambda: 2)
    monkeypatch.setattr(
        pipeline,
        "discover_files_to_index",
        lambda *_args, **_kwargs: ([first_file, second_file], None),
    )
    monkeypatch.setattr(pipeline, "pre_scan_for_imports", lambda *_args, **_kwargs: {})
    writer = SimpleNamespace(add_repository_to_graph=lambda *_args, **_kwargs: None)
    manager = JobManager()
    indexing_task = asyncio.create_task(
        pipeline.run_tree_sitter_index_async(
            repo,
            False,
            "cancel-job",
            None,
            writer,
            manager,
            {".py": "python"},
            lambda *_args: None,
            parse_file,
            lambda *_args: None,
        )
    )

    try:
        assert await asyncio.to_thread(first_started.wait, 3), "first parser did not start"
        assert await asyncio.to_thread(second_started.wait, 3), "second parser did not start"
        cancellation_requested.set()
        release_first.set()
        assert await asyncio.to_thread(cancellation_checked.wait, 3), "cancellation was not observed"
        await asyncio.sleep(0.05)
        pipeline_waited_for_sibling = not indexing_task.done()
    finally:
        release_first.set()
        release_second.set()

    with pytest.raises(JobCancelledError):
        await indexing_task
    assert pipeline_waited_for_sibling, "indexing returned while a sibling parser worker was still active"
    assert second_finished.is_set(), "sibling parser worker was not drained before cancellation returned"


def test_write_session_is_acquired_before_shared_query_lock():
    """Pooled readers waiting on the query lock must not starve a writer's session."""

    class OneConnectionPool:
        def __init__(self):
            self._connections = queue.Queue(maxsize=1)
            self._connections.put(object())
            self.writer_started = threading.Event()
            self.writer_waiting_for_connection = threading.Event()

        def get(self, timeout=None):
            if self.writer_started.is_set():
                self.writer_waiting_for_connection.set()
            try:
                return self._connections.get(timeout=min(timeout or 0.3, 0.3))
            except queue.Empty:
                raise

        def put(self, connection):
            self._connections.put(connection)

    pool = OneConnectionPool()
    query_lock = threading.Lock()
    driver = EmbeddedDriverWrapper(db=None, pool_or_lock=pool, write_lock=query_lock)
    reader_session_opened = threading.Event()
    allow_reader_query = threading.Event()

    def read_like_code_finder():
        with driver.session() as _session:
            reader_session_opened.set()
            assert allow_reader_query.wait(timeout=3), "test did not release reader query"
            query_lock.acquire()
            try:
                return "read"
            finally:
                query_lock.release()

    def write():
        pool.writer_started.set()
        return execute_write_operation(driver, "kuzudb", lambda _session: "written")

    with ThreadPoolExecutor(max_workers=2) as executor:
        reader = executor.submit(read_like_code_finder)
        assert reader_session_opened.wait(timeout=2), "reader did not acquire its pooled session"
        writer = executor.submit(write)
        try:
            assert pool.writer_waiting_for_connection.wait(timeout=2), "writer did not request a session"
        finally:
            allow_reader_query.set()

        assert reader.result(timeout=2) == "read"
        assert writer.result(timeout=2) == "written"


def _repository_count(driver, repo):
    with driver.session() as session:
        row = session.run(
            "MATCH (r:Repository {path: $path}) RETURN count(r) AS count",
            path=repo.resolve().as_posix(),
        ).single()
    return row["count"]


def _block_embedded_query(monkeypatch, query_fragment):
    """Pause an embedded query inside the executor until the test releases it."""
    query_started = threading.Event()
    release_query = threading.Event()
    original_execute = EmbeddedSessionWrapper._execute_bound

    def blocking_execute(session, query, parameters):
        if query_fragment in query and not query_started.is_set():
            query_started.set()
            assert release_query.wait(timeout=5), "test did not release the embedded query"
        return original_execute(session, query, parameters)

    monkeypatch.setattr(EmbeddedSessionWrapper, "_execute_bound", blocking_execute)
    return query_started, release_query


def _block_embedded_file_write(monkeypatch):
    """Pause a File write inside the embedded query executor until released."""
    return _block_embedded_query(monkeypatch, "MERGE (f:File {path: $path})")


@pytest.mark.asyncio
async def test_repository_is_hidden_from_indexed_list_until_ready(tmp_path, monkeypatch):
    """A building root remains unavailable to indexed-repository discovery."""
    db_manager, _driver, _jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    finder = CodeFinder(db_manager)
    try:
        builder._writer.add_repository_to_graph(repo, index_state="building")
        assert finder.list_indexed_repositories() == []

        builder._writer.mark_repository_ready(repo)

        assert finder.list_indexed_repositories() == [
            {"name": repo.name, "path": repo.resolve().as_posix(), "is_dependency": False}
        ]
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_repository_upsert_preserves_building_state_during_watcher_scan(
    tmp_path, monkeypatch
):
    """A watcher upsert cannot expose an index that is still being built."""
    db_manager, driver, _jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    try:
        builder._writer.add_repository_to_graph(repo, index_state="building")

        # GraphWatcher._initial_scan uses the default repository upsert.
        builder._writer.add_repository_to_graph(repo)

        with driver.session() as session:
            state = session.run(
                "MATCH (r:Repository {path: $path}) RETURN r.index_state AS state",
                path=repo.resolve().as_posix(),
            ).single()["state"]
        assert state == "building"
        assert CodeFinder(db_manager).list_indexed_repositories() == []
        await builder._cleanup_partial_index(repo, only_if_building=True)
        assert _repository_count(driver, repo) == 0
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_codefinder_queries_reject_partial_building_graph(tmp_path, monkeypatch):
    """Search APIs cannot return nodes from a repository that is not ready."""
    db_manager, driver, _jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    builder._writer.add_repository_to_graph(repo, index_state="building")
    builder._writer.add_file_to_graph(
        {
            "path": repo / "app.py",
            "repo_path": repo,
            "lang": "python",
            "is_dependency": False,
            "imports": [
                {
                    "name": "target_module",
                    "source": "target_module",
                    "line_number": 1,
                    "lang": "python",
                }
            ],
        },
        repo.name,
        {},
    )
    try:
        with driver.session() as session:
            partial_count = session.run(
                "MATCH (file:File)-[:IMPORTS]->(module:Module {name: $name}) "
                "RETURN count(file) AS count",
                name="target_module",
            ).single()["count"]
        assert partial_count == 1
        assert CodeFinder(db_manager).list_indexed_repositories() == []

        with pytest.raises(RepositoryIndexInProgressError, match="indexing is in progress"):
            CodeFinder(db_manager).who_imports_module("target_module")

        raw_query_result = query_handlers.execute_cypher_query(
            db_manager,
            cypher_query="MATCH (file:File) RETURN file.path AS path",
        )
        assert "indexing is in progress" in raw_query_result["details"]
        stats_result = management_handlers.get_repository_stats(
            CodeFinder(db_manager), repo_path=str(repo)
        )
        assert "indexing is in progress" in stats_result["error"]
    finally:
        db_manager.close_driver()


def test_falkordb_readiness_check_is_atomic_with_index_writes():
    class _Result:
        def __init__(self, header, result_set):
            self.header = header
            self.result_set = result_set

    class _Graph:
        def __init__(self):
            self.building = False
            self.readiness_query_started = threading.Event()
            self.release_readiness_query = threading.Event()
            self.writer_started = threading.Event()
            self.writer_completed = threading.Event()

        def query(self, query, _parameters=None):
            if "index_state: 'building'" in query:
                # Model a completed empty guard result, then pause before it
                # returns to CodeFinder so an index write can race the gap.
                result = _Result([[0, "path"]], [])
                self.readiness_query_started.set()
                if not self.release_readiness_query.wait(timeout=2):
                    raise TimeoutError("test did not release the readiness query")
                return result
            if query == "MARK BUILDING":
                self.building = True
                self.writer_completed.set()
                return _Result([], [])
            if "MATCH (file:File)-[imp:IMPORTS]" in query:
                rows = [["partial.py"]] if self.building else []
                return _Result([[0, "file_name"]], rows)
            raise AssertionError(f"unexpected query: {query}")

    class _Manager:
        def __init__(self, driver):
            self.driver = driver

        def get_driver(self, _graph_name=None):
            return self.driver

    graph = _Graph()
    driver = FalkorDBDriverWrapper(graph)
    writer_driver = FalkorDBDriverWrapper(graph)
    finder = CodeFinder(_Manager(driver))
    rows = []
    errors = []

    def read_imports():
        try:
            rows.extend(finder.who_imports_module("pending_module"))
        except BaseException as error:
            errors.append(error)

    def mark_building():
        graph.writer_started.set()
        with writer_driver.session() as session:
            session.run("MARK BUILDING")

    reader = threading.Thread(target=read_imports)
    writer = threading.Thread(target=mark_building)
    reader.start()
    try:
        assert graph.readiness_query_started.wait(timeout=2)
        writer.start()
        assert graph.writer_started.wait(timeout=2)
        writer_crossed_readiness = graph.writer_completed.wait(timeout=0.1)
    finally:
        graph.release_readiness_query.set()
        reader.join(timeout=2)
        if writer.ident is not None:
            writer.join(timeout=2)

    assert not reader.is_alive()
    assert not writer.is_alive()
    assert not errors
    assert writer_crossed_readiness is False
    assert rows == []
    assert graph.building is True


def test_neo4j_readiness_check_is_atomic_with_index_writes():
    class _Result:
        def __init__(self, rows):
            self.rows = rows

        def single(self):
            return self.rows[0] if self.rows else None

        def data(self):
            return self.rows

    class _Graph:
        def __init__(self):
            self.building = False
            self.readiness_query_started = threading.Event()
            self.release_readiness_query = threading.Event()
            self.writer_started = threading.Event()
            self.writer_completed = threading.Event()

        def run(self, query):
            if "index_state: 'building'" in query:
                result = _Result([])
                self.readiness_query_started.set()
                if not self.release_readiness_query.wait(timeout=2):
                    raise TimeoutError("test did not release the readiness query")
                return result
            if query == "MARK BUILDING":
                self.building = True
                self.writer_completed.set()
                return _Result([])
            if "MATCH (file:File)-[imp:IMPORTS]" in query:
                rows = [{"file_name": "partial.py"}] if self.building else []
                return _Result(rows)
            raise AssertionError(f"unexpected query: {query}")

    class _Transaction:
        def __init__(self, graph):
            self.graph = graph

        def run(self, query, *args, **kwargs):
            return self.graph.run(query)

        def commit(self):
            pass

        def rollback(self):
            pass

    class _Session:
        def __init__(self, graph):
            self.graph = graph

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def begin_transaction(self):
            return _Transaction(self.graph)

        def execute_write(self, work_fn):
            return work_fn(_Transaction(self.graph))

    class _Driver:
        def __init__(self, graph):
            self.graph = graph

        def session(self, **_kwargs):
            return _Session(self.graph)

    class _Manager:
        def __init__(self, driver):
            self.driver = driver

        def get_driver(self, _graph_name=None):
            return self.driver

    graph = _Graph()
    underlying_driver = _Driver(graph)
    read_driver = Neo4jDriverWrapper(underlying_driver)
    write_driver = Neo4jDriverWrapper(underlying_driver)
    finder = CodeFinder(_Manager(read_driver))
    rows = []
    errors = []

    def read_imports():
        try:
            rows.extend(finder.who_imports_module("pending_module"))
        except BaseException as error:
            errors.append(error)

    def mark_building():
        graph.writer_started.set()
        execute_write_operation(
            write_driver,
            "neo4j",
            lambda session: session.run("MARK BUILDING"),
        )

    reader = threading.Thread(target=read_imports)
    writer = threading.Thread(target=mark_building)
    reader.start()
    try:
        assert graph.readiness_query_started.wait(timeout=2)
        writer.start()
        assert graph.writer_started.wait(timeout=2)
        writer_crossed_readiness = graph.writer_completed.wait(timeout=0.1)
    finally:
        graph.release_readiness_query.set()
        reader.join(timeout=2)
        if writer.ident is not None:
            writer.join(timeout=2)

    assert not reader.is_alive()
    assert not writer.is_alive()
    assert not errors
    assert writer_crossed_readiness is False
    assert rows == []
    assert graph.building is True


@pytest.mark.asyncio
async def test_successful_job_commits_repository_ready_before_completion(tmp_path, monkeypatch):
    """A successful full index is visible only after its ready commit."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))
    try:
        await builder.build_graph_from_path_async(repo, job_id=job_id)

        assert jobs.get_job(job_id).status == JobStatus.COMPLETED
        assert CodeFinder(db_manager).list_indexed_repositories() == [
            {"name": repo.name, "path": repo.resolve().as_posix(), "is_dependency": False}
        ]
        with driver.session() as session:
            row = session.run(
                "MATCH (r:Repository {path: $path}) RETURN r.index_state AS state",
                path=repo.resolve().as_posix(),
            ).single()
        assert row["state"] == "ready"
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_preflight_cancellation_preserves_existing_ready_index(tmp_path, monkeypatch):
    """A job cancelled before marking the repository building must not delete its prior index."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    try:
        first_job_id = jobs.create_job(str(repo))
        await builder.build_graph_from_path_async(repo, job_id=first_job_id)
        assert jobs.get_job(first_job_id).status == JobStatus.COMPLETED

        job_id = jobs.create_job(str(repo))

        def cancel_during_preflight(*_args, **_kwargs):
            assert jobs.request_cancel(job_id)
            return "false"

        monkeypatch.setattr(graph_builder_module, "get_config_value", cancel_during_preflight)
        await builder.build_graph_from_path_async(repo, job_id=job_id)

        assert jobs.get_job(job_id).status == JobStatus.CANCELLED
        assert CodeFinder(db_manager).list_indexed_repositories() == [
            {"name": repo.name, "path": repo.resolve().as_posix(), "is_dependency": False}
        ]
        with driver.session() as session:
            state = session.run(
                "MATCH (r:Repository {path: $path}) RETURN r.index_state AS state",
                path=repo.resolve().as_posix(),
            ).single()["state"]
        assert state == "ready"
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_parent_cancellation_cleanup_preserves_ready_nested_repository(
    tmp_path, monkeypatch
):
    """Removing a cancelled parent build must retain a ready nested repository."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    nested_repo = repo / "nested-repository"
    nested_repo.mkdir()
    nested_file = nested_repo / "nested.py"
    nested_file.write_text("def nested():\n    return 2\n", encoding="utf-8")
    builder._writer.add_repository_to_graph(nested_repo, index_state="ready")
    builder._writer.add_minimal_file_node(nested_file, nested_repo)
    job_id = jobs.create_job(str(repo))

    async def create_partial_parent_then_cancel(
        path, is_dependency, active_job_id, *_args, **_kwargs
    ):
        builder._writer.add_repository_to_graph(
            path, is_dependency, index_state="building"
        )
        builder._writer.add_minimal_file_node(path / "app.py", path, is_dependency)
        jobs.request_cancel(active_job_id)
        jobs.raise_if_cancelled(active_job_id)

    monkeypatch.setattr(
        graph_builder_module,
        "run_tree_sitter_index_async",
        create_partial_parent_then_cancel,
    )
    try:
        await builder.build_graph_from_path_async(repo, job_id=job_id)

        assert jobs.get_job(job_id).status == JobStatus.CANCELLED
        assert _repository_count(driver, repo) == 0
        assert _repository_count(driver, nested_repo) == 1
        assert CodeFinder(db_manager).list_indexed_repositories() == [
            {
                "name": nested_repo.name,
                "path": nested_repo.resolve().as_posix(),
                "is_dependency": False,
            }
        ]
        with driver.session() as session:
            file_count = session.run(
                "MATCH (f:File {path: $path}) RETURN count(f) AS count",
                path=nested_file.resolve().as_posix(),
            ).single()["count"]
        assert file_count == 1
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_explicit_parent_deletion_removes_nested_repository_nodes(tmp_path, monkeypatch):
    """Explicit subtree deletion removes nested Repository nodes as well as files."""
    db_manager, driver, _jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    nested_repo = repo / "nested-repository"
    nested_repo.mkdir()
    nested_file = nested_repo / "nested.py"
    nested_file.write_text("def nested():\n    return 2\n", encoding="utf-8")
    builder._writer.add_repository_to_graph(repo, index_state="ready")
    builder._writer.add_repository_to_graph(nested_repo, index_state="ready")
    builder._writer.add_minimal_file_node(nested_file, nested_repo)

    try:
        assert builder._writer.delete_repository_from_graph(repo)

        assert _repository_count(driver, repo) == 0
        assert _repository_count(driver, nested_repo) == 0
        with driver.session() as session:
            assert session.run(
                "MATCH (f:File {path: $path}) RETURN count(f) AS count",
                path=nested_file.resolve().as_posix(),
            ).single()["count"] == 0
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_parent_partial_cleanup_removes_nested_building_marker(tmp_path, monkeypatch):
    """Partial cleanup removes unready nested roots so they cannot block graph reads."""
    db_manager, driver, _jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    nested_repo = repo / "nested-repository"
    nested_repo.mkdir()
    nested_file = nested_repo / "nested.py"
    nested_file.write_text("def nested():\n    return 2\n", encoding="utf-8")
    builder._writer.add_repository_to_graph(repo, index_state="building")
    builder._writer.add_repository_to_graph(nested_repo, index_state="building")
    builder._writer.add_minimal_file_node(nested_file, nested_repo)

    try:
        assert builder._writer.delete_repository_from_graph(
            repo, preserve_nested_repositories=True
        )

        assert _repository_count(driver, repo) == 0
        assert _repository_count(driver, nested_repo) == 0
        assert CodeFinder(db_manager).list_indexed_repositories() == []
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_partial_cleanup_keeps_building_marker_until_all_nodes_are_deleted(
    tmp_path, monkeypatch
):
    """Readiness checks must stay blocked while later labels still contain partial data."""
    db_manager, driver, _jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    builder._writer.add_repository_to_graph(repo, index_state="building")
    with driver.session() as session:
        session.run(
            "CREATE (:Struct {uid: $uid, name: $name, path: $path})",
            uid="partial-struct",
            name="PartialClass",
            path=(repo / "app.py").resolve().as_posix(),
        )

    # Pause after the Repository label and before the remaining Struct label.
    # The readiness marker must still exist while partial data remains.
    next_label_started = threading.Event()
    release_next_label = threading.Event()

    def ordered_labels():
        yield "Repository"
        next_label_started.set()
        assert release_next_label.wait(timeout=5), "test did not release cleanup"
        yield "Struct"

    monkeypatch.setattr(builder._writer, "_get_all_node_labels", ordered_labels)
    cleanup_task = asyncio.create_task(
        builder._cleanup_partial_index(repo, only_if_building=True)
    )
    try:
        assert await asyncio.to_thread(next_label_started.wait, 5)
        with pytest.raises(
            RepositoryIndexInProgressError, match="indexing is in progress"
        ):
            with CodeFinder(db_manager).driver.session() as session:
                session.run(
                    "MATCH (n:Struct {name: $name}) RETURN n.name AS name",
                    name="PartialClass",
                ).data()
    finally:
        release_next_label.set()
    await cleanup_task
    try:
        assert _repository_count(driver, repo) == 0
        with driver.session() as session:
            remaining = session.run(
                "MATCH (n:Struct {name: $name}) RETURN count(n) AS count",
                name="PartialClass",
            ).single()["count"]
        assert remaining == 0
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", ["maven", "gradle", "mybatis"])
async def test_manifest_graph_write_failure_cleans_partial_index(
    tmp_path, monkeypatch, manifest
):
    """Manifest graph persistence failures must fail and clean the whole index job."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    if manifest == "maven":
        from codegraphcontext.tools.languages import maven as language_module

        monkeypatch.setattr(
            language_module,
            "parse_repo_maven",
            lambda *_args: {"modules": [{}]},
        )
        writer_method = "write_maven_build_graph"
    elif manifest == "gradle":
        from codegraphcontext.tools.languages import gradle as language_module

        monkeypatch.setattr(
            language_module,
            "parse_repo_gradle",
            lambda *_args: {"modules": [{}]},
        )
        writer_method = "write_gradle_build_graph"
    else:
        from codegraphcontext.tools.languages import mybatis as language_module

        monkeypatch.setattr(
            language_module,
            "find_and_parse_mybatis_mappers",
            lambda *_args: [{}],
        )
        writer_method = "write_mybatis_links"

    def write_partial_graph_then_fail(*_args, **_kwargs):
        with driver.session() as session:
            session.run(
                "CREATE (:Struct {uid: $uid, name: $name, path: $path})",
                uid=f"partial-{manifest}",
                name="PartialManifestNode",
                path=(repo / "app.py").resolve().as_posix(),
            )
        raise RuntimeError(f"injected {manifest} graph write failure")

    monkeypatch.setattr(builder._writer, writer_method, write_partial_graph_then_fail)
    job_id = jobs.create_job(str(repo))

    try:
        await builder.build_graph_from_path_async(repo, job_id=job_id)

        assert jobs.get_job(job_id).status == JobStatus.FAILED
        assert _repository_count(driver, repo) == 0
        with driver.session() as session:
            partial_count = session.run(
                "MATCH (n:Struct {uid: $uid}) RETURN count(n) AS count",
                uid=f"partial-{manifest}",
            ).single()["count"]
        assert partial_count == 0
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_cleanup_error_is_not_swallowed_when_caller_is_cancelled(tmp_path, monkeypatch):
    """Cancellation while a cleanup thread runs still reports its DB failure."""
    db_manager, _driver, _jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    def fail_after_release(_repo_path, **_kwargs):
        cleanup_started.set()
        assert release_cleanup.wait(timeout=5), "test did not release cleanup"
        raise RuntimeError("cleanup transaction failed")

    monkeypatch.setattr(builder._writer, "delete_repository_from_graph", fail_after_release)
    try:
        cleanup_task = asyncio.create_task(
            builder._cleanup_partial_index(repo, propagate_cancellation=False)
        )
        assert await asyncio.to_thread(cleanup_started.wait, 5), "cleanup thread did not start"
        cleanup_task.cancel()
        await asyncio.sleep(0.02)
        release_cleanup.set()

        error = await cleanup_task

        assert isinstance(error, RuntimeError)
        assert str(error) == "cleanup transaction failed"
    finally:
        release_cleanup.set()
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_cancel_with_cleanup_failure_reports_failed_and_hides_partial_index(
    tmp_path, monkeypatch
):
    """A failed delete cannot report a cancelled or discoverable repository."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))

    async def create_partial_then_cancel(path, is_dependency, active_job_id, *_args, **_kwargs):
        builder._writer.add_repository_to_graph(path, is_dependency, index_state="building")
        builder._writer.add_minimal_file_node(path / "app.py", path, is_dependency)
        jobs.request_cancel(active_job_id)
        jobs.raise_if_cancelled(active_job_id)

    def fail_delete(_repo_path, **_kwargs):
        raise RuntimeError("cleanup transaction unavailable")

    monkeypatch.setattr(graph_builder_module, "run_tree_sitter_index_async", create_partial_then_cancel)
    monkeypatch.setattr(builder._writer, "delete_repository_from_graph", fail_delete)
    try:
        await builder.build_graph_from_path_async(repo, job_id=job_id)

        job = jobs.get_job(job_id)
        assert job.status == JobStatus.FAILED
        assert "partial index cleanup failed" in job.errors[0]
        assert "cleanup transaction unavailable" in job.errors[0]
        assert _repository_count(driver, repo) == 1
        assert CodeFinder(db_manager).list_indexed_repositories() == []
        cancellation_result = await jobs.cancel_job_async(job_id)
        assert cancellation_result["status"] == JobStatus.FAILED.value
        assert cancellation_result["cancelled"] is False
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_retry_removes_stale_partial_graph_before_writing(tmp_path, monkeypatch):
    """A later job cleans a hidden leftover graph before merging fresh data."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))
    stale_file = repo / "removed-before-retry.py"
    builder._writer.add_repository_to_graph(repo, index_state="building")
    builder._writer.add_minimal_file_node(stale_file, repo)

    async def verify_stale_graph_removed(path, is_dependency, active_job_id, *_args, **_kwargs):
        assert _repository_count(driver, path) == 0
        with driver.session() as session:
            stale_count = session.run(
                "MATCH (f:File {path: $path}) RETURN count(f) AS count",
                path=stale_file.resolve().as_posix(),
            ).single()["count"]
        assert stale_count == 0
        builder._writer.add_repository_to_graph(path, is_dependency, index_state="building")
        jobs.complete_job(
            active_job_id,
            before_commit=lambda: builder._writer.mark_repository_ready(path),
        )

    monkeypatch.setattr(
        graph_builder_module, "run_tree_sitter_index_async", verify_stale_graph_removed
    )
    try:
        await builder.build_graph_from_path_async(repo, job_id=job_id)

        assert jobs.get_job(job_id).status == JobStatus.COMPLETED
        assert CodeFinder(db_manager).list_indexed_repositories()[0]["path"] == repo.resolve().as_posix()
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_deadline_during_ready_marker_keeps_completed_index_visible(tmp_path, monkeypatch):
    """A successful ready-marker commit wins if its write crosses the deadline."""
    db_manager, _driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo), timeout_seconds=30)
    mark_ready = builder._writer.mark_repository_ready

    def mark_ready_after_deadline(path):
        mark_ready(path)
        jobs._deadlines[job_id] = 0.0

    monkeypatch.setattr(builder._writer, "mark_repository_ready", mark_ready_after_deadline)
    try:
        await builder.build_graph_from_path_async(repo, job_id=job_id)

        job = jobs.get_job(job_id)
        assert job.status == JobStatus.COMPLETED
        assert job.cancel_requested is False
        indexed = CodeFinder(db_manager).list_indexed_repositories()
        assert len(indexed) == 1
        assert indexed[0]["path"] == repo.resolve().as_posix()
        assert indexed[0]["is_dependency"] is False
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_cancel_before_queued_completion_worker_starts_prevents_ready_commit(monkeypatch):
    """Cancellation is recorded before a queued completion thread can publish ready."""
    jobs = JobManager()
    job_id = jobs.create_job("/queued-completion")
    completion_queued = asyncio.Event()
    release_completion = asyncio.Event()
    ready_marker_calls = []

    async def queued_to_thread(func, *args, **kwargs):
        completion_queued.set()
        await release_completion.wait()
        return func(*args, **kwargs)

    async def indexing_worker():
        try:
            await pipeline._run_job_thread(
                jobs,
                job_id,
                jobs.complete_job,
                job_id,
                before_commit=lambda: ready_marker_calls.append(True),
            )
        except asyncio.CancelledError:
            jobs.finish_cancelled(job_id)
            raise

    monkeypatch.setattr(asyncio, "to_thread", queued_to_thread)
    worker_task = asyncio.create_task(indexing_worker())
    assert jobs.start_job(job_id, worker_task)
    await completion_queued.wait()

    worker_task.cancel()
    try:
        await asyncio.sleep(0)
        job = jobs.get_job(job_id)
        assert job.cancel_requested is True
        assert job.status == JobStatus.RUNNING
    finally:
        release_completion.set()

    with pytest.raises(asyncio.CancelledError):
        await worker_task

    job = jobs.get_job(job_id)
    assert job.status == JobStatus.CANCELLED
    assert job.cancel_reason == "request_cancelled"
    assert ready_marker_calls == []


@pytest.mark.asyncio
async def test_cancel_before_index_start_prevents_writes(tmp_path, monkeypatch):
    """A cancellation already requested on a pending job creates no graph data."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))
    try:
        assert jobs.request_cancel(job_id)

        await builder.build_graph_from_path_async(repo, job_id=job_id)

        assert jobs.get_job(job_id).status == JobStatus.CANCELLED
        assert _repository_count(driver, repo) == 0
        assert builder.last_index_summary == {}
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_cancel_active_embedded_write_waits_then_removes_partial_repository(tmp_path, monkeypatch):
    """Cancel waits for the active embedded write, then cleans before returning."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))
    write_started, release_write = _block_embedded_file_write(monkeypatch)
    cancel_requested = threading.Event()
    writes_after_request = []
    original_write = builder._writer.add_file_to_graph

    def blocking_write(*args, **kwargs):
        if cancel_requested.is_set():
            writes_after_request.append("write started after cancel")
        original_write(*args, **kwargs)

    monkeypatch.setattr(builder._writer, "add_file_to_graph", blocking_write)
    try:
        index_task = asyncio.create_task(builder.build_graph_from_path_async(repo, job_id=job_id))
        assert await asyncio.to_thread(write_started.wait, 5), "indexing did not reach its DB write"
        cancel_requested.set()
        cancel_task = asyncio.create_task(
            _minimal_server(jobs).handle_tool_call(
                "cancel_indexing_job", {"job_id": job_id}
            )
        )
        await asyncio.sleep(0.05)

        assert not cancel_task.done(), "cancel returned while an embedded write was still active"
        assert jobs.get_job(job_id).status != JobStatus.CANCELLED

        release_write.set()
        await index_task
        result = await cancel_task

        assert result["status"] == JobStatus.CANCELLED.value
        assert jobs.get_job(job_id).status == JobStatus.CANCELLED
        assert writes_after_request == []
        assert _repository_count(driver, repo) == 0
    finally:
        release_write.set()
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_cancel_after_source_removal_cleans_original_root_only(tmp_path, monkeypatch):
    """Cleanup retains the captured repo root if its source directory disappears."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))
    write_started, release_write = _block_embedded_file_write(monkeypatch)
    builder._writer.add_repository_to_graph(tmp_path, is_dependency=False)
    try:
        index_task = asyncio.create_task(builder.build_graph_from_path_async(repo, job_id=job_id))
        assert await asyncio.to_thread(write_started.wait, 5), "indexing did not reach its DB write"

        assert jobs.request_cancel(job_id)
        shutil.rmtree(repo)
        release_write.set()
        await index_task

        assert jobs.get_job(job_id).status == JobStatus.CANCELLED
        assert _repository_count(driver, repo) == 0
        assert _repository_count(driver, tmp_path) == 1
    finally:
        release_write.set()
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_source_removed_while_waiting_for_repository_lock_does_not_index_parent(
    tmp_path, monkeypatch
):
    """A queued job must retain its original root and fail before writing if removed."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))
    builder._writer.add_repository_to_graph(tmp_path, is_dependency=False)
    lock_key, repo_lock = builder._get_repository_lock(repo.resolve())
    assert repo_lock.acquire(blocking=False)

    try:
        index_task = asyncio.create_task(builder.build_graph_from_path_async(repo, job_id=job_id))
        await asyncio.sleep(0.05)
        shutil.rmtree(repo)
        repo_lock.release()
        await index_task

        assert jobs.get_job(job_id).status == JobStatus.FAILED
        assert _repository_count(driver, repo) == 0
        assert _repository_count(driver, tmp_path) == 1
    finally:
        if repo_lock.locked():
            repo_lock.release()
        builder._release_repository_lock_reference(lock_key, repo_lock)
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_incomplete_scip_index_is_cleaned_without_tree_sitter_fallback(
    tmp_path, monkeypatch
):
    """An unwritable SCIP file cannot become a ready graph through fallback."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))

    def fail_partial_scip(*_args, **_kwargs):
        builder._writer.add_repository_to_graph(
            repo, is_dependency=False, index_state="building"
        )
        raise scip_pipeline.IncompleteScipIndexError("1 of 2 SCIP file writes failed")

    async def unexpected_fallback(*_args, **_kwargs):
        pytest.fail("Tree-sitter fallback must not complete a partial SCIP index")

    monkeypatch.setattr(graph_builder_module, "get_config_value", lambda *_args, **_kwargs: "true")
    monkeypatch.setattr(
        "codegraphcontext.tools.scip_indexer.detect_project_lang", lambda *_args: "python"
    )
    monkeypatch.setattr(
        "codegraphcontext.tools.scip_indexer.is_scip_available", lambda *_args: True
    )
    monkeypatch.setattr(builder, "_build_graph_from_scip", fail_partial_scip)
    monkeypatch.setattr(graph_builder_module, "run_tree_sitter_index_async", unexpected_fallback)

    try:
        await builder.build_graph_from_path_async(repo, job_id=job_id)

        assert jobs.get_job(job_id).status == JobStatus.FAILED
        assert _repository_count(driver, repo) == 0
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_scip_fallback_clears_partial_graph_before_tree_sitter_runs(
    tmp_path, monkeypatch
):
    """A generic SCIP failure cannot leave nodes for Tree-sitter to merge into."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))

    def fail_after_partial_write(*_args, **_kwargs):
        builder._writer.add_repository_to_graph(
            repo, is_dependency=False, index_state="building"
        )
        builder._writer.add_minimal_file_node(repo / "app.py", repo, is_dependency=False)
        raise ValueError("malformed SCIP output after graph writes")

    async def verify_clean_fallback(*_args, **_kwargs):
        assert _repository_count(driver, repo) == 0
        with driver.session() as session:
            file_count = session.run(
                "MATCH (f:File {path: $path}) RETURN count(f) AS count",
                path=(repo / "app.py").resolve().as_posix(),
            ).single()["count"]
        assert file_count == 0
        builder._writer.add_repository_to_graph(repo, is_dependency=False)
        jobs.complete_job(job_id)

    monkeypatch.setattr(graph_builder_module, "get_config_value", lambda *_args, **_kwargs: "true")
    monkeypatch.setattr(
        "codegraphcontext.tools.scip_indexer.detect_project_lang", lambda *_args: "python"
    )
    monkeypatch.setattr(
        "codegraphcontext.tools.scip_indexer.is_scip_available", lambda *_args: True
    )
    monkeypatch.setattr(builder, "_build_graph_from_scip", fail_after_partial_write)
    monkeypatch.setattr(
        graph_builder_module, "run_tree_sitter_index_async", verify_clean_fallback
    )

    try:
        await builder.build_graph_from_path_async(repo, job_id=job_id)

        assert jobs.get_job(job_id).status == JobStatus.COMPLETED
        assert _repository_count(driver, repo) == 1
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_timeout_during_embedded_write_cleans_partial_repository(tmp_path, monkeypatch):
    """A deadline reached during a write stops the job at that safe boundary."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo), timeout_seconds=30)
    write_started, release_write = _block_embedded_file_write(monkeypatch)
    try:
        index_task = asyncio.create_task(builder.build_graph_from_path_async(repo, job_id=job_id))
        assert await asyncio.to_thread(write_started.wait, 5), "indexing did not reach its DB write"
        jobs._deadlines[job_id] = 0.0
        release_write.set()
        await index_task

        job = jobs.get_job(job_id)
        assert job.status == JobStatus.CANCELLED
        assert job.cancel_reason == "timeout"
        assert _repository_count(driver, repo) == 0
    finally:
        release_write.set()
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_cancelled_scip_request_prevents_completion_and_cleans_graph(tmp_path, monkeypatch):
    """SCIP cancellation reaches JobManager before its worker is drained."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))
    worker_at_completion = threading.Event()
    release_worker = threading.Event()

    def blocked_scip_worker(path, is_dependency, worker_job_id, _lang, writer, manager,
                            *_args, **_kwargs):
        manager.raise_if_cancelled(worker_job_id)
        writer.add_repository_to_graph(path, is_dependency, index_state="building")
        worker_at_completion.set()
        assert release_worker.wait(timeout=5), "test did not release the SCIP worker"
        manager.raise_if_cancelled(worker_job_id)
        manager.complete_job(worker_job_id)

    monkeypatch.setattr(graph_builder_module, "get_config_value", lambda *_args, **_kwargs: "true")
    monkeypatch.setattr(scip_pipeline, "_run_scip_index", blocked_scip_worker)
    monkeypatch.setattr(
        "codegraphcontext.tools.scip_indexer.detect_project_lang", lambda *_args: "python"
    )
    monkeypatch.setattr(
        "codegraphcontext.tools.scip_indexer.is_scip_available", lambda *_args: True
    )

    try:
        index_task = asyncio.create_task(builder.build_graph_from_path_async(repo, job_id=job_id))
        assert await asyncio.to_thread(worker_at_completion.wait, 5), "SCIP worker did not start"

        index_task.cancel()
        await asyncio.sleep(0.05)
        assert jobs.is_cancel_requested(job_id)
        assert not index_task.done(), "cancel returned before the SCIP worker drained"

        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await index_task

        assert jobs.get_job(job_id).status == JobStatus.CANCELLED
        assert _repository_count(driver, repo) == 0
    finally:
        release_worker.set()
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_scip_cancellation_during_repository_write_skips_indexer_launch(
    tmp_path, monkeypatch
):
    """Cancellation during the initial DB write prevents launching SCIP afterward."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))
    write_started, release_write = _block_embedded_query(
        monkeypatch, "MERGE (r:Repository {path: $path})"
    )
    scip_runs = []

    class FakeScipIndexer:
        def run(self, *_args, **_kwargs):
            scip_runs.append(True)
            return None

    monkeypatch.setattr(graph_builder_module, "get_config_value", lambda *_args, **_kwargs: "true")
    monkeypatch.setattr(
        "codegraphcontext.tools.scip_indexer.detect_project_lang", lambda *_args: "python"
    )
    monkeypatch.setattr(
        "codegraphcontext.tools.scip_indexer.is_scip_available", lambda *_args: True
    )
    monkeypatch.setattr("codegraphcontext.tools.scip_indexer.ScipIndexer", FakeScipIndexer)
    try:
        index_task = asyncio.create_task(builder.build_graph_from_path_async(repo, job_id=job_id))
        assert await asyncio.to_thread(write_started.wait, 5), "SCIP repository write did not start"

        index_task.cancel()
        await asyncio.sleep(0.02)
        assert not index_task.done(), "cancellation returned before the repository write drained"

        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await index_task

        assert scip_runs == []
        assert jobs.get_job(job_id).status == JobStatus.CANCELLED
        assert _repository_count(driver, repo) == 0
    finally:
        release_write.set()
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_scip_cancellation_during_indexer_run_skips_output_parsing(tmp_path, monkeypatch):
    """Cancellation waits for SCIP to return and skips parsing its stale output."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))
    scip_started = threading.Event()
    release_scip = threading.Event()
    parser_calls = []

    class FakeScipIndexer:
        def run(self, _path, _lang, output_dir):
            scip_started.set()
            assert release_scip.wait(timeout=5), "test did not release the SCIP process"
            return Path(output_dir) / "index.scip"

    class FakeScipParser:
        def parse(self, *_args, **_kwargs):
            parser_calls.append(True)
            return {"files": {str(repo / "app.py"): {"functions": []}}}

    monkeypatch.setattr(graph_builder_module, "get_config_value", lambda *_args, **_kwargs: "true")
    monkeypatch.setattr(
        "codegraphcontext.tools.scip_indexer.detect_project_lang", lambda *_args: "python"
    )
    monkeypatch.setattr(
        "codegraphcontext.tools.scip_indexer.is_scip_available", lambda *_args: True
    )
    monkeypatch.setattr("codegraphcontext.tools.scip_indexer.ScipIndexer", FakeScipIndexer)
    monkeypatch.setattr("codegraphcontext.tools.scip_indexer.ScipIndexParser", FakeScipParser)
    try:
        index_task = asyncio.create_task(builder.build_graph_from_path_async(repo, job_id=job_id))
        assert await asyncio.to_thread(scip_started.wait, 5), "SCIP process did not start"

        index_task.cancel()
        await asyncio.sleep(0.02)
        assert jobs.is_cancel_requested(job_id)
        assert not index_task.done(), "cancellation returned before the SCIP process drained"

        release_scip.set()
        with pytest.raises(asyncio.CancelledError):
            await index_task

        assert parser_calls == []
        assert jobs.get_job(job_id).status == JobStatus.CANCELLED
        assert _repository_count(driver, repo) == 0
    finally:
        release_scip.set()
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_cancellation_during_scip_fallback_cleanup_stops_before_tree_sitter(
    tmp_path, monkeypatch
):
    """Cancellation during fallback cleanup must not be swallowed before Tree-sitter starts."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    original_delete = builder._writer.delete_repository_from_graph

    async def fail_scip_after_partial_write(*_args, **_kwargs):
        builder._writer.add_repository_to_graph(
            repo, is_dependency=False, index_state="building"
        )
        builder._writer.add_minimal_file_node(repo / "app.py", repo, is_dependency=False)
        raise ValueError("malformed SCIP output after graph writes")

    def blocked_delete(path, **_kwargs):
        cleanup_started.set()
        assert release_cleanup.wait(timeout=5), "test did not release fallback cleanup"
        return original_delete(path)

    async def unexpected_tree_sitter_fallback(*_args, **_kwargs):
        pytest.fail("Tree-sitter must not start after request cancellation")

    monkeypatch.setattr(graph_builder_module, "get_config_value", lambda *_args, **_kwargs: "true")
    monkeypatch.setattr(
        "codegraphcontext.tools.scip_indexer.detect_project_lang", lambda *_args: "python"
    )
    monkeypatch.setattr(
        "codegraphcontext.tools.scip_indexer.is_scip_available", lambda *_args: True
    )
    monkeypatch.setattr(builder, "_build_graph_from_scip", fail_scip_after_partial_write)
    monkeypatch.setattr(builder._writer, "delete_repository_from_graph", blocked_delete)
    monkeypatch.setattr(
        graph_builder_module, "run_tree_sitter_index_async", unexpected_tree_sitter_fallback
    )

    try:
        index_task = asyncio.create_task(builder.build_graph_from_path_async(repo, job_id=job_id))
        assert await asyncio.to_thread(cleanup_started.wait, 5), "SCIP fallback cleanup did not start"

        index_task.cancel()
        await asyncio.sleep(0.02)
        assert not index_task.done(), "cancellation returned before fallback cleanup drained"

        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await index_task

        assert jobs.get_job(job_id).status == JobStatus.CANCELLED
        assert _repository_count(driver, repo) == 0
    finally:
        release_cleanup.set()
        db_manager.close_driver()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository_layout", ["same-path", "nested-path", "sibling-path", "graph-alias"]
)
async def test_repository_jobs_serialize_cancellation_cleanup(
    tmp_path, monkeypatch, repository_layout
):
    """Cancellation cleanup cannot delete another job's graph data."""
    db_manager, driver, jobs, first_builder = _new_indexer(tmp_path, monkeypatch)
    second_builder = GraphBuilder(
        db_manager,
        jobs,
        asyncio.get_running_loop(),
        graph_name="ignored-alias" if repository_layout == "graph-alias" else None,
    )
    repo = _new_repository(tmp_path)
    second_repo = repo
    if repository_layout == "nested-path":
        second_repo = repo / "nested-repository"
        second_repo.mkdir()
        (second_repo / "app.py").write_text("def child():\n    return 2\n", encoding="utf-8")
    elif repository_layout == "sibling-path":
        second_repo = tmp_path / "sibling-repository"
        second_repo.mkdir()
        (second_repo / "app.py").write_text("def sibling():\n    return 3\n", encoding="utf-8")
    first_job = jobs.create_job(str(repo))
    second_job = jobs.create_job(str(second_repo))
    first_started = threading.Event()
    release_first = threading.Event()

    async def controlled_pipeline(
        path,
        is_dependency,
        job_id,
        _cgcignore_path,
        writer,
        manager,
        _parsers,
        _get_parser,
        _parse_file,
        add_minimal_file_node,
        **_kwargs,
    ):
        manager.raise_if_cancelled(job_id)
        writer.add_repository_to_graph(path, is_dependency, index_state="building")
        if job_id == first_job:
            first_started.set()
            assert await asyncio.to_thread(release_first.wait, 5), "test did not release first job"
        manager.raise_if_cancelled(job_id)
        add_minimal_file_node(path / "app.py", path, is_dependency)
        manager.complete_job(
            job_id, before_commit=lambda: writer.mark_repository_ready(path)
        )

    monkeypatch.setattr(graph_builder_module, "run_tree_sitter_index_async", controlled_pipeline)
    first_task = None
    second_task = None
    try:
        first_task = asyncio.create_task(
            first_builder.build_graph_from_path_async(repo, job_id=first_job)
        )
        assert await asyncio.to_thread(first_started.wait, 5), "first job did not start indexing"
        second_task = asyncio.create_task(
            second_builder.build_graph_from_path_async(second_repo, job_id=second_job)
        )
        await asyncio.sleep(0.05)

        assert not second_task.done(), "second repository job bypassed the graph index lock"
        assert jobs.get_job(second_job).status == JobStatus.PENDING
        assert jobs.request_cancel(first_job)
        release_first.set()
        await first_task
        await second_task

        assert jobs.get_job(first_job).status == JobStatus.CANCELLED
        assert jobs.get_job(second_job).status == JobStatus.COMPLETED
        assert _repository_count(driver, second_repo) == 1
        if repository_layout == "nested-path":
            assert _repository_count(driver, repo) == 0
            assert [
                repository["path"]
                for repository in CodeFinder(db_manager).list_indexed_repositories()
            ] == [second_repo.resolve().as_posix()]
        elif repository_layout in ("same-path", "graph-alias"):
            assert _repository_count(driver, repo) == 1
        else:
            assert _repository_count(driver, repo) == 0
            assert [
                repository["path"]
                for repository in CodeFinder(db_manager).list_indexed_repositories()
            ] == [second_repo.resolve().as_posix()]
        with driver.session() as session:
            file_count = session.run(
                "MATCH (f:File {path: $path}) RETURN count(f) AS count",
                path=(second_repo / "app.py").resolve().as_posix(),
            ).single()["count"]
        assert file_count == 1
    finally:
        jobs.request_cancel(first_job)
        release_first.set()
        pending = [task for task in (first_task, second_task) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_failure_during_postprocessing_removes_partial_repository(tmp_path, monkeypatch):
    """A fatal post-processing error cannot leave a ready partial repository."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))

    def fail_postprocessing(*_args, **_kwargs):
        raise RuntimeError("post-processing failed")

    monkeypatch.setattr(builder._writer, "write_function_call_groups", fail_postprocessing)
    try:
        await builder.build_graph_from_path_async(repo, job_id=job_id)

        job = jobs.get_job(job_id)
        assert job.status == JobStatus.FAILED
        assert job.errors == ["post-processing failed"]
        assert _repository_count(driver, repo) == 0
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_repeated_cancel_racing_completion_cleans_index(tmp_path, monkeypatch):
    """Repeated cancel requests win just before the indexing completion point."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))
    cancellation_requested = threading.Event()
    writes_after_cancel = []
    cancel_results = []
    original_write = builder._writer.add_file_to_graph
    original_complete = jobs.complete_job

    def observe_file_write(*args, **kwargs):
        if cancellation_requested.is_set():
            writes_after_cancel.append(True)
        return original_write(*args, **kwargs)

    def cancel_before_completion(completing_job_id, **kwargs):
        if completing_job_id == job_id:
            cancellation_requested.set()
            cancel_results.extend((jobs.request_cancel(job_id), jobs.request_cancel(job_id)))
        return original_complete(completing_job_id, **kwargs)

    monkeypatch.setattr(builder._writer, "add_file_to_graph", observe_file_write)
    monkeypatch.setattr(jobs, "complete_job", cancel_before_completion)
    try:
        await builder.build_graph_from_path_async(repo, job_id=job_id)

        assert cancel_results == [True, True]
        assert jobs.get_job(job_id).status == JobStatus.CANCELLED
        assert writes_after_cancel == []
        assert _repository_count(driver, repo) == 0
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_total_file_write_failure_marks_job_failed_and_removes_repository(tmp_path, monkeypatch):
    """A graph with no successfully written files cannot be reported ready."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    job_id = jobs.create_job(str(repo))
    write_attempts = []

    def fail_file_write(*_args, **_kwargs):
        write_attempts.append(True)
        raise RuntimeError("embedded graph write unavailable")

    monkeypatch.setattr(builder._writer, "add_file_to_graph", fail_file_write)
    try:
        await builder.build_graph_from_path_async(repo, job_id=job_id)

        job = jobs.get_job(job_id)
        assert job.status == JobStatus.FAILED
        assert "All 1 indexed file write attempts failed" in job.errors[0]
        assert len(write_attempts) == 2  # initial write and one retry
        assert _repository_count(driver, repo) == 0
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_permanent_file_write_failure_removes_index_after_other_retry_succeeds(
    tmp_path, monkeypatch
):
    """One permanently unwritten file prevents a partial graph from becoming ready."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    (repo / "app.py").unlink()
    retry_file = repo / "retry.py"
    retry_file.write_text("def retry():\n    return 2\n", encoding="utf-8")
    permanent_failure = repo / "permanent_failure.py"
    permanent_failure.write_text("def fail():\n    return 3\n", encoding="utf-8")
    job_id = jobs.create_job(str(repo))
    attempts = {retry_file.name: 0, permanent_failure.name: 0}
    original_write = builder._writer.add_file_to_graph

    def selective_write(file_data, *args, **kwargs):
        name = Path(file_data["path"]).name
        attempts[name] += 1
        if name == permanent_failure.name:
            raise RuntimeError("persistent write failure")
        if name == retry_file.name and attempts[name] == 1:
            raise RuntimeError("transient write failure")
        return original_write(file_data, *args, **kwargs)

    monkeypatch.setattr(builder._writer, "add_file_to_graph", selective_write)
    try:
        await builder.build_graph_from_path_async(repo, job_id=job_id)

        assert jobs.get_job(job_id).status == JobStatus.FAILED
        assert "1 of 2 indexed file writes failed after retry" in jobs.get_job(job_id).errors[0]
        assert attempts == {retry_file.name: 2, permanent_failure.name: 2}
        assert _repository_count(driver, repo) == 0
        with driver.session() as session:
            retry_count = session.run(
                "MATCH (f:File {path: $path}) RETURN count(f) AS count",
                path=retry_file.resolve().as_posix(),
            ).single()["count"]
            failed_count = session.run(
                "MATCH (f:File {path: $path}) RETURN count(f) AS count",
                path=permanent_failure.resolve().as_posix(),
            ).single()["count"]
        assert retry_count == 0
        assert failed_count == 0
    finally:
        db_manager.close_driver()


@pytest.mark.asyncio
async def test_cancelled_mcp_request_stops_its_scheduled_index_job(tmp_path, monkeypatch):
    """Cancelling the MCP request also cancels the job its handler was starting."""
    db_manager, driver, jobs, builder = _new_indexer(tmp_path, monkeypatch)
    repo = _new_repository(tmp_path)
    handler_started = threading.Event()
    allow_schedule = threading.Event()
    write_started, release_write = _block_embedded_file_write(monkeypatch)
    scheduled = {}
    loop = asyncio.get_running_loop()

    def add_code_to_graph_tool(**_args):
        handler_started.set()
        assert allow_schedule.wait(timeout=5), "test did not release the MCP tool handler"
        job_id = jobs.create_job(str(repo))
        scheduled["job_id"] = job_id
        scheduled["future"] = asyncio.run_coroutine_threadsafe(
            builder.build_graph_from_path_async(repo, job_id=job_id), loop
        )
        assert write_started.wait(timeout=5), "scheduled job did not reach its DB write"
        return {"success": True, "job_id": job_id}

    server = _minimal_server(jobs)
    server.add_code_to_graph_tool = add_code_to_graph_tool

    async def get_server():
        return server

    monkeypatch.setattr(mcp_sse, "get_server", get_server)
    params = SimpleNamespace(name="add_code_to_graph", arguments={"repo_path": str(repo)})
    try:
        request_task = asyncio.create_task(mcp_sse.handle_call_tool(None, params))
        assert await asyncio.to_thread(handler_started.wait, 5), "MCP tool handler did not start"
        request_task.cancel()
        await asyncio.sleep(0.05)
        assert not request_task.done(), "cancelled request returned before its handler was drained"

        allow_schedule.set()
        assert await asyncio.to_thread(write_started.wait, 5), "scheduled job did not reach its DB write"
        for _ in range(100):
            if jobs.is_cancel_requested(scheduled["job_id"]):
                break
            await asyncio.sleep(0.01)
        assert jobs.is_cancel_requested(scheduled["job_id"])
        assert not request_task.done(), "request cancellation returned before index cleanup"

        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await request_task
        scheduled["future"].result(timeout=5)

        assert jobs.get_job(scheduled["job_id"]).status == JobStatus.CANCELLED
        assert _repository_count(driver, repo) == 0
    finally:
        allow_schedule.set()
        release_write.set()
        db_manager.close_driver()
