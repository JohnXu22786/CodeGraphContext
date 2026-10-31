"""Cross-process ownership tests for embedded database paths."""

import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest

from codegraphcontext.core.database_ownership import (
    DatabaseOwnershipError,
    DatabaseOwnershipLease,
    GatewayRegistration,
    GatewayRequestError,
    find_active_gateway,
    owner_metadata_path,
    read_database_owner,
)
from codegraphcontext.core import database_embedded_kuzu, database_ownership
from codegraphcontext.core.database_kuzu import KuzuDBManager


def _child_env(tmp_path):
    env = os.environ.copy()
    env["CGC_HOME"] = str(tmp_path / "home")
    return env


def test_database_lease_is_exclusive_and_released_after_process_crash(tmp_path):
    database_path = tmp_path / "db" / "ladybugdb"
    script = textwrap.dedent(
        """
        import os
        import sys
        import time
        from codegraphcontext.core.database_ownership import DatabaseOwnershipLease

        lease = DatabaseOwnershipLease(sys.argv[1], "ladybugdb").acquire()
        lease.register_gateway("http://127.0.0.1:1", port=1)
        print(f"ready:{os.getpid()}", flush=True)
        time.sleep(60)
        """
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(database_path)],
        env=_child_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = owner.stdout.readline().strip()
        assert ready == f"ready:{owner.pid}"

        contender_script = textwrap.dedent(
            """
            import sys
            from codegraphcontext.core.database_ownership import (
                DatabaseOwnershipError,
                DatabaseOwnershipLease,
            )

            try:
                DatabaseOwnershipLease(sys.argv[1], "ladybugdb").acquire()
            except DatabaseOwnershipError as exc:
                print(f"blocked:{exc.owner.get('pid')}")
                raise SystemExit(3)
            print("unexpected-owner")
            """
        )
        contender = subprocess.run(
            [sys.executable, "-c", contender_script, str(database_path)],
            env=_child_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert contender.returncode == 3
        assert contender.stdout.strip() == f"blocked:{owner.pid}"

        owner.kill()
        owner.wait(timeout=10)
        stale_owner = read_database_owner(database_path)
        assert stale_owner["pid"] == owner.pid
        assert find_active_gateway(database_path, "ladybugdb") is None

        recovered_lease = DatabaseOwnershipLease(database_path, "ladybugdb").acquire()
        try:
            recovered_owner = read_database_owner(database_path)
            assert recovered_owner["pid"] == os.getpid()
            assert recovered_owner["gateway"] is None
        finally:
            recovered_lease.release()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)


class _OwnerHandler(BaseHTTPRequestHandler):
    response = {}

    def do_GET(self):
        body = json.dumps(self.response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def owner_http_server():
    server = HTTPServer(("127.0.0.1", 0), _OwnerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_gateway_discovery_rejects_port_and_database_mismatch(
    tmp_path, owner_http_server
):
    database_path = tmp_path / "db" / "ladybugdb"
    port = owner_http_server.server_address[1]
    lease = DatabaseOwnershipLease(database_path, "ladybugdb").acquire()
    lease.register_gateway(f"http://127.0.0.1:{port}", port=port)
    owner_http_server.RequestHandlerClass.response = {
        "database_path": str(Path(database_path).resolve()),
        "backend": "ladybugdb",
        "pid": os.getpid(),
        "hostname": __import__("socket").gethostname(),
        "instance_name": lease.instance_name,
        "lease_id": lease.lease_id,
        "port": port,
    }

    try:
        assert find_active_gateway(database_path, "ladybugdb") is not None

        owner_http_server.RequestHandlerClass.response["port"] = port + 1
        assert find_active_gateway(database_path, "ladybugdb") is None

        owner_http_server.RequestHandlerClass.response.update(
            {"port": port, "database_path": str(tmp_path / "other-db")}
        )
        assert find_active_gateway(database_path, "ladybugdb") is None
    finally:
        lease.release()


def test_unreachable_gateway_registration_never_allows_a_second_owner(
    tmp_path, owner_http_server
):
    database_path = tmp_path / "db" / "ladybugdb"
    lease = DatabaseOwnershipLease(database_path, "ladybugdb").acquire()
    lease.register_gateway("http://127.0.0.1:1", port=1)
    try:
        assert find_active_gateway(database_path, "ladybugdb", timeout=0.1) is None
        with pytest.raises(DatabaseOwnershipError) as exc_info:
            DatabaseOwnershipLease(database_path, "ladybugdb").acquire()
        assert exc_info.value.owner["pid"] == os.getpid()
    finally:
        lease.release()


def test_truncated_gateway_handshake_still_blocks_a_second_owner(tmp_path):
    class TruncatedOwnerHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b'10\r\n{"incomplete')

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), TruncatedOwnerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    database_path = tmp_path / "db" / "ladybugdb"
    port = server.server_address[1]
    lease = DatabaseOwnershipLease(database_path, "ladybugdb").acquire()
    lease.register_gateway(f"http://127.0.0.1:{port}", port=port)
    try:
        assert find_active_gateway(database_path, "ladybugdb", timeout=0.1) is None
        with pytest.raises(DatabaseOwnershipError) as exc_info:
            DatabaseOwnershipLease(database_path, "ladybugdb").acquire()
        assert exc_info.value.owner["pid"] == os.getpid()
    finally:
        lease.release()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_local_owner_opener_does_not_forward_tokens_across_redirects():
    received_tokens = []

    class TokenSinkHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            received_tokens.append(self.headers.get("X-CGC-Owner-Token"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            pass

    target = HTTPServer(("127.0.0.1", 0), TokenSinkHandler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{target.server_address[1]}/sink",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    redirector = HTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = threading.Thread(
        target=redirector.serve_forever, daemon=True
    )
    redirect_thread.start()
    request = database_ownership.Request(
        f"http://127.0.0.1:{redirector.server_address[1]}/owner",
        headers={"X-CGC-Owner-Token": "owner-secret"},
    )

    try:
        with pytest.raises(HTTPError) as exc_info:
            database_ownership._local_opener().open(request, timeout=1)
        assert exc_info.value.code == 302
        assert received_tokens == []
    finally:
        redirector.shutdown()
        redirect_thread.join(timeout=5)
        redirector.server_close()
        target.shutdown()
        target_thread.join(timeout=5)
        target.server_close()


def test_truncated_http_error_body_becomes_gateway_request_error(monkeypatch, tmp_path):
    class TruncatedBody:
        def read(self, _limit):
            raise IncompleteRead(b"partial", 32)

        def close(self):
            pass

    error = HTTPError(
        "http://127.0.0.1:1/update",
        400,
        "Bad Request",
        hdrs=None,
        fp=TruncatedBody(),
    )

    class Opener:
        def open(self, _request, *, timeout):
            assert timeout is None
            raise error

    monkeypatch.setattr(database_ownership, "_local_opener", lambda: Opener())
    registration = GatewayRegistration(
        database_path=str(tmp_path / "db"),
        backend="ladybugdb",
        pid=os.getpid(),
        hostname="localhost",
        instance_name="test-instance",
        lease_id="test-lease",
        endpoint="http://127.0.0.1:1",
        port=1,
        token="test-token",
    )

    with pytest.raises(GatewayRequestError, match="HTTP 400"):
        registration.update_repository(tmp_path)


def test_invalid_owner_metadata_is_not_accepted_as_gateway(tmp_path):
    database_path = tmp_path / "db" / "ladybugdb"
    lease = DatabaseOwnershipLease(database_path, "ladybugdb").acquire()
    lease.register_gateway("http://127.0.0.1:1", port=1)
    try:
        metadata_path = owner_metadata_path(database_path)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["owner"]["backend"] = "kuzudb"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        assert find_active_gateway(database_path, "ladybugdb", timeout=0.1) is None
    finally:
        lease.release()


def test_out_of_range_stale_owner_pid_allows_lease_recovery(tmp_path, monkeypatch):
    database_path = tmp_path / "db" / "ladybugdb"
    lease = DatabaseOwnershipLease(database_path, "ladybugdb").acquire()
    lease.register_gateway("http://127.0.0.1:1", port=1)
    metadata_path = owner_metadata_path(database_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    lease.release()

    invalid_pid = 1 << 100
    metadata["owner"]["pid"] = invalid_pid
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    def raise_pid_overflow(pid, _signal):
        assert pid == invalid_pid
        raise OverflowError("PID is outside the platform range")

    monkeypatch.setattr(os, "kill", raise_pid_overflow)
    assert find_active_gateway(database_path, "ladybugdb") is None

    recovered_lease = DatabaseOwnershipLease(database_path, "ladybugdb").acquire()
    try:
        assert read_database_owner(database_path)["pid"] == os.getpid()
    finally:
        recovered_lease.release()


def test_update_retries_with_restarted_gateway_registration(tmp_path):
    class RestartingGatewayHandler(BaseHTTPRequestHandler):
        owner = None
        post_tokens = []

        def _respond(self, status_code, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            owner = self.owner
            token = owner["gateway"]["token"]
            if self.headers.get("X-CGC-Owner-Token") != token:
                self._respond(403, {"detail": "Owner registration changed"})
                return
            self._respond(
                200,
                {
                    "database_path": owner["database_path"],
                    "backend": owner["backend"],
                    "pid": owner["pid"],
                    "hostname": owner["hostname"],
                    "instance_name": owner["instance_name"],
                    "lease_id": owner["lease_id"],
                    "port": owner["gateway"]["port"],
                },
            )

        def do_POST(self):
            token = self.headers.get("X-CGC-Owner-Token")
            self.post_tokens.append(token)
            if token != self.owner["gateway"]["token"]:
                self._respond(403, {"detail": "Owner registration changed"})
                return
            self._respond(200, {"status": "ok"})

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), RestartingGatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    database_path = tmp_path / "db" / "ladybugdb"
    port = server.server_address[1]
    endpoint = f"http://127.0.0.1:{port}"
    lease = DatabaseOwnershipLease(database_path, "ladybugdb").acquire()
    lease.register_gateway(endpoint, port=port)
    RestartingGatewayHandler.owner = lease.owner
    RestartingGatewayHandler.post_tokens = []
    old_registration = GatewayRegistration(
        database_path=lease.database_path,
        backend="ladybugdb",
        pid=os.getpid(),
        hostname=lease.owner["hostname"],
        instance_name=lease.instance_name,
        lease_id="old-lease",
        endpoint=endpoint,
        port=port,
        token="old-token",
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    try:
        assert old_registration.update_repository(repo) == {"status": "ok"}
        assert RestartingGatewayHandler.post_tokens == ["old-token", lease.token]
    finally:
        lease.release()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_update_retries_after_gateway_restarts_on_a_new_port(tmp_path):
    class GatewayHandler(BaseHTTPRequestHandler):
        owner = None
        post_tokens = []

        def _respond(self, status_code, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            owner = self.owner
            if self.headers.get("X-CGC-Owner-Token") != owner["gateway"]["token"]:
                self._respond(403, {"detail": "Owner registration changed"})
                return
            self._respond(
                200,
                {
                    "database_path": owner["database_path"],
                    "backend": owner["backend"],
                    "pid": owner["pid"],
                    "hostname": owner["hostname"],
                    "instance_name": owner["instance_name"],
                    "lease_id": owner["lease_id"],
                    "port": owner["gateway"]["port"],
                },
            )

        def do_POST(self):
            token = self.headers.get("X-CGC-Owner-Token")
            self.post_tokens.append(token)
            if token != self.owner["gateway"]["token"]:
                self._respond(403, {"detail": "Owner registration changed"})
                return
            self._respond(200, {"status": "ok"})

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), GatewayHandler)
    closed_socket = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    old_port = closed_socket.server_address[1]
    closed_socket.server_close()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    database_path = tmp_path / "db" / "ladybugdb"
    lease = DatabaseOwnershipLease(database_path, "ladybugdb").acquire()
    port = server.server_address[1]
    endpoint = f"http://127.0.0.1:{port}"
    lease.register_gateway(endpoint, port=port)
    GatewayHandler.owner = lease.owner
    GatewayHandler.post_tokens = []
    old_registration = GatewayRegistration(
        database_path=lease.database_path,
        backend="ladybugdb",
        pid=os.getpid(),
        hostname=lease.owner["hostname"],
        instance_name=lease.instance_name,
        lease_id="old-lease",
        endpoint=f"http://127.0.0.1:{old_port}",
        port=old_port,
        token="old-token",
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    try:
        assert old_registration.update_repository(repo) == {"status": "ok"}
        assert GatewayHandler.post_tokens == [lease.token]
    finally:
        lease.release()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_update_repository_has_no_default_deadline(tmp_path, monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, _limit):
            return b'{"status":"ok"}'

    class Opener:
        def open(self, _request, *, timeout):
            assert timeout is None
            return Response()

    monkeypatch.setattr(
        database_ownership,
        "_local_opener",
        lambda: Opener(),
    )
    registration = GatewayRegistration(
        database_path=str(tmp_path / "db"),
        backend="ladybugdb",
        pid=os.getpid(),
        hostname="localhost",
        instance_name="test-instance",
        lease_id="test-lease",
        endpoint="http://127.0.0.1:1",
        port=1,
        token="test-token",
    )

    assert registration.update_repository(tmp_path) == {"status": "ok"}


def test_close_driver_retains_lease_when_database_close_fails(tmp_path):
    class FailingDatabase:
        is_closed = False

        def close(self):
            raise RuntimeError("native close failed")

    manager = object.__new__(KuzuDBManager)
    manager._lock = threading.Lock()
    database = FailingDatabase()
    manager._db = database
    manager._pool = queue.Queue()
    lease = DatabaseOwnershipLease(tmp_path / "db" / "kuzudb", "kuzudb").acquire()
    manager._ownership_lease = lease

    try:
        manager.close_driver()
        assert manager._db is database
        assert manager._ownership_lease is lease
        assert lease.is_acquired
        with pytest.raises(RuntimeError, match="close"):
            manager.get_driver()
    finally:
        lease.release()
        manager._db = None
        manager._pool = None
        manager._ownership_lease = None


def test_context_switch_retains_manager_when_database_close_fails(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from codegraphcontext.cli.config_manager import ResolvedContext
    from codegraphcontext.core.jobs import JobManager
    from codegraphcontext.server import MCPServer
    import codegraphcontext.server as server_module

    class FailingDatabase:
        is_closed = False

        def close(self):
            raise RuntimeError("native close failed")

    class Observer:
        def is_alive(self):
            return False

    class Watcher:
        observer = Observer()

        def stop(self):
            pass

    manager = object.__new__(KuzuDBManager)
    manager._lock = threading.Lock()
    manager.db_path = str(tmp_path / "db" / "kuzudb")
    manager._db = FailingDatabase()
    manager._pool = queue.Queue()
    manager._conn = None
    manager._ownership_lease = DatabaseOwnershipLease(
        manager.db_path, "kuzudb"
    ).acquire()

    server = object.__new__(MCPServer)
    server._context_transition_lock = threading.RLock()
    server.db_manager = manager
    server.job_manager = JobManager()
    server.code_watcher = Watcher()
    server.loop = None
    server.resolved_context = ResolvedContext(
        mode="per-repo",
        context_name="",
        database="kuzudb",
        db_path=str(tmp_path / "old-db"),
        cgcignore_path=str(tmp_path / ".cgcignore"),
        is_local=True,
    )
    factory_calls = []

    def make_new_manager(**kwargs):
        factory_calls.append(kwargs)
        return SimpleNamespace(get_driver=lambda: None)

    monkeypatch.setattr(server_module, "get_database_manager", make_new_manager)
    monkeypatch.setattr(
        server_module, "_default_global_db_path", lambda _db: "/tmp/global-db"
    )
    monkeypatch.setattr(
        server_module, "load_config", lambda **_kwargs: {"DEFAULT_DATABASE": "kuzudb"}
    )
    monkeypatch.setattr(
        server_module, "GraphBuilder", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        server_module, "CodeFinder", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        server_module, "CodeWatcher", lambda *_args, **_kwargs: object()
    )

    try:
        result = server.switch_context_tool(context_path="global")

        assert "error" in result
        assert "ownership lease remains held" in result["error"].lower()
        assert server.db_manager is manager
        assert manager._ownership_lease.is_acquired
        assert factory_calls == []
        with pytest.raises(DatabaseOwnershipError):
            DatabaseOwnershipLease(manager.db_path, "kuzudb").acquire()
    finally:
        manager._ownership_lease.release()
        manager._ownership_lease = None
        manager._db = None


def test_failed_initialization_retains_lease_when_database_close_fails(
    tmp_path, monkeypatch
):
    class FailingDatabase:
        is_closed = False

        def close(self):
            raise RuntimeError("native close failed")

    class FakeConnection:
        def close(self):
            pass

    database = FailingDatabase()

    class FakeBackend:
        @staticmethod
        def Database(*_args, **_kwargs):
            return database

        @staticmethod
        def Connection(_database):
            return FakeConnection()

    manager = object.__new__(KuzuDBManager)
    manager._lock = threading.Lock()
    manager.db_path = str(tmp_path / "db" / "kuzudb")
    manager._db = None
    manager._pool = None
    manager._conn = None
    manager._ownership_lease = None

    def fail_schema():
        raise RuntimeError("schema initialization failed")

    manager._initialize_schema = fail_schema
    monkeypatch.setattr(
        database_embedded_kuzu.importlib,
        "import_module",
        lambda _name: FakeBackend,
    )
    monkeypatch.setattr(
        database_embedded_kuzu, "resolve_embedded_buffer_pool_size", lambda: 0
    )

    try:
        with pytest.raises(RuntimeError, match="schema initialization failed"):
            manager.get_driver()

        lease = manager._ownership_lease
        assert manager._db is database
        assert manager._pool is None
        assert lease is not None and lease.is_acquired
        with pytest.raises(RuntimeError, match="close"):
            manager.get_driver()
    finally:
        if manager._ownership_lease is not None:
            manager._ownership_lease.release()
        manager._db = None
        manager._pool = None
        manager._ownership_lease = None


def test_lock_retry_closes_previous_database_before_reopening(tmp_path, monkeypatch):
    databases = []

    class FakeDatabase:
        is_closed = False

        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            self.is_closed = True

    class FakeConnection:
        def close(self):
            pass

    class FakeBackend:
        @staticmethod
        def Database(*_args, **_kwargs):
            database = FakeDatabase()
            databases.append(database)
            return database

        @staticmethod
        def Connection(_database):
            return FakeConnection()

    manager = object.__new__(KuzuDBManager)
    manager._lock = threading.Lock()
    manager.db_path = str(tmp_path / "db" / "kuzudb")
    manager._db = None
    manager._pool = None
    manager._conn = None
    manager._ownership_lease = None
    schema_attempts = 0

    def fail_once_with_lock():
        nonlocal schema_attempts
        schema_attempts += 1
        if schema_attempts == 1:
            raise RuntimeError("database lock contention")

    manager._initialize_schema = fail_once_with_lock
    monkeypatch.setattr(
        database_embedded_kuzu.importlib,
        "import_module",
        lambda _name: FakeBackend,
    )
    monkeypatch.setattr(
        database_embedded_kuzu, "resolve_embedded_buffer_pool_size", lambda: 0
    )
    monkeypatch.setattr(database_embedded_kuzu.time, "sleep", lambda _delay: None)

    try:
        driver = manager.get_driver()
        assert schema_attempts == 2
        assert len(databases) == 2
        assert databases[0].close_calls == 1
        assert databases[0].is_closed
        assert manager._db is databases[1]
        driver.close()
        manager.close_driver()
    finally:
        if manager._ownership_lease is not None:
            manager._ownership_lease.release()
        manager._db = None
        manager._pool = None
        manager._ownership_lease = None


def test_lock_retry_close_failure_retains_database_lease(tmp_path, monkeypatch):
    class FailingDatabase:
        is_closed = False

        def close(self):
            raise RuntimeError("native close failed")

    class FakeConnection:
        def close(self):
            pass

    database = FailingDatabase()
    database_creations = 0

    class FakeBackend:
        @staticmethod
        def Database(*_args, **_kwargs):
            nonlocal database_creations
            database_creations += 1
            return database

        @staticmethod
        def Connection(_database):
            return FakeConnection()

    manager = object.__new__(KuzuDBManager)
    manager._lock = threading.Lock()
    manager.db_path = str(tmp_path / "db" / "kuzudb")
    manager._db = None
    manager._pool = None
    manager._conn = None
    manager._ownership_lease = None
    manager._initialize_schema = lambda: (_ for _ in ()).throw(
        RuntimeError("database lock contention")
    )
    monkeypatch.setattr(
        database_embedded_kuzu.importlib,
        "import_module",
        lambda _name: FakeBackend,
    )
    monkeypatch.setattr(
        database_embedded_kuzu, "resolve_embedded_buffer_pool_size", lambda: 0
    )
    monkeypatch.setattr(database_embedded_kuzu.time, "sleep", lambda _delay: None)

    try:
        with pytest.raises(RuntimeError, match="Could not close"):
            manager.get_driver()
        lease = manager._ownership_lease
        assert database_creations == 1
        assert manager._db is database
        assert lease is not None and lease.is_acquired
        with pytest.raises(DatabaseOwnershipError):
            DatabaseOwnershipLease(manager.db_path, "kuzudb").acquire()
    finally:
        if manager._ownership_lease is not None:
            manager._ownership_lease.release()
        manager._db = None
        manager._pool = None
        manager._ownership_lease = None
