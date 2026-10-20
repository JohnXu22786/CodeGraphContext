"""Tests for API /health endpoint (BUG-008)."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock
import pytest
from fastapi.testclient import TestClient

from codegraphcontext.api import app as api_app
from codegraphcontext.api import router as api_router
from codegraphcontext.core.database_ownership import (
    DatabaseOwnershipLease,
    GatewayRequestError,
    find_active_gateway,
    register_configured_gateway,
)
from codegraphcontext.cli.config_manager import ResolvedContext
from codegraphcontext.api.app import create_app
import codegraphcontext.server as server_module


def test_health_endpoint_returns_ok():
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_remote_database_outage_does_not_block_api_liveness_startup(
    monkeypatch, tmp_path
):
    connect_calls = []

    class UnavailableRemoteManager:
        def get_driver(self):
            connect_calls.append("connect")
            raise RuntimeError("remote database is unavailable")

        def close_driver(self):
            pass

    manager = UnavailableRemoteManager()
    context = ResolvedContext(
        mode="global",
        context_name="",
        database="falkordb-remote",
        db_path=str(tmp_path / "remote-db"),
        cgcignore_path="",
        is_local=True,
    )
    monkeypatch.setattr(
        server_module, "resolve_context", lambda cwd: context
    )
    monkeypatch.setattr(
        server_module,
        "get_database_manager",
        lambda db_path=None: manager,
    )
    monkeypatch.setattr(api_app, "shutdown_server", lambda: None)
    monkeypatch.setattr(api_router, "_server_instance", None)

    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200

    assert connect_calls == []
    api_router._server_instance = None


def test_asgi_lifespan_initializes_gateway_without_cli_endpoint(monkeypatch):
    calls = []

    async def get_server():
        calls.append("startup")

    monkeypatch.delenv("CGC_GATEWAY_ENDPOINT", raising=False)
    monkeypatch.setattr(api_app, "get_server_for_gateway_startup", get_server)
    monkeypatch.setattr(api_app, "shutdown_server", lambda: calls.append("shutdown"))

    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200

    assert calls == ["startup", "shutdown"]


def test_asgi_startup_registers_owner_before_first_request(monkeypatch, tmp_path):
    lease = DatabaseOwnershipLease(tmp_path / "ladybugdb", "ladybugdb").acquire()

    class FakeManager:
        _ownership_lease = lease

    class FakeServer:
        db_manager = FakeManager()

    async def get_server():
        api_router._server_instance = FakeServer()
        return api_router._server_instance

    monkeypatch.delenv("CGC_GATEWAY_ENDPOINT", raising=False)
    monkeypatch.setattr(api_app, "get_server_for_gateway_startup", get_server)
    monkeypatch.setattr(api_app, "shutdown_server", lambda: None)
    api_router._server_instance = None

    try:
        with TestClient(create_app()):
            registration = find_active_gateway(lease.database_path, "ladybugdb")
            assert registration is not None

        assert registration.endpoint.startswith("http://127.0.0.1:")
        assert registration.port > 0
    finally:
        lease.release()
        api_router._server_instance = None


def test_asgi_endpoint_registers_embedded_manager_opened_after_startup(
    monkeypatch, tmp_path
):
    class FakeManager:
        _ownership_lease = None

    class FakeServer:
        db_manager = FakeManager()

    fake_server = FakeServer()

    async def get_server():
        api_router._server_instance = fake_server
        return api_router._server_instance

    monkeypatch.setenv("CGC_GATEWAY_ENDPOINT", "http://127.0.0.1:9000")
    monkeypatch.setattr(api_app, "get_server_for_gateway_startup", get_server)
    monkeypatch.setattr(api_app, "shutdown_server", lambda: None)
    api_router._server_instance = None

    try:
        with TestClient(create_app()):
            later_lease = DatabaseOwnershipLease(
                tmp_path / "later-context" / "ladybugdb", "ladybugdb"
            ).acquire()
            fake_server.db_manager._ownership_lease = later_lease
            try:
                # EmbeddedGraphManager.get_driver calls this after opening a DB.
                register_configured_gateway(later_lease)
                registration = find_active_gateway(
                    later_lease.database_path, "ladybugdb"
                )
                assert registration is not None
                assert registration.port != 9000
            finally:
                later_lease.release()
    finally:
        api_router._server_instance = None


def test_ownership_handshake_stays_responsive_during_update_prework(
    monkeypatch, tmp_path
):
    lease = DatabaseOwnershipLease(tmp_path / "ladybugdb", "ladybugdb").acquire()
    update_started = Event()
    release_update = Event()

    class FakeManager:
        _ownership_lease = lease

    class BlockingCodeFinder:
        def list_indexed_repositories(self):
            update_started.set()
            if not release_update.wait(timeout=5):
                raise RuntimeError("Test did not release blocked update prework")
            raise RuntimeError("Stop after handshake responsiveness check")

    class FakeServer:
        db_manager = FakeManager()
        code_finder = BlockingCodeFinder()
        _context_transition_lock = RLock()

        class ResolvedContext:
            cgcignore_path = str(tmp_path / ".cgcignore")

        resolved_context = ResolvedContext()

    fake_server = FakeServer()

    async def get_server():
        api_router._server_instance = fake_server
        return fake_server

    monkeypatch.delenv("CGC_GATEWAY_ENDPOINT", raising=False)
    monkeypatch.setattr(api_app, "get_server_for_gateway_startup", get_server)
    monkeypatch.setattr(api_app, "shutdown_server", lambda: None)
    api_router._server_instance = None

    try:
        with TestClient(create_app()):
            registration = find_active_gateway(lease.database_path, "ladybugdb")
            assert registration is not None
            with ThreadPoolExecutor(max_workers=1) as executor:
                update = executor.submit(
                    registration.update_repository,
                    tmp_path,
                    cgcignore_path=str(tmp_path / ".cgcignore"),
                )
                try:
                    assert update_started.wait(timeout=2)
                    assert (
                        find_active_gateway(lease.database_path, "ladybugdb")
                        is not None
                    )
                finally:
                    release_update.set()
                with pytest.raises(GatewayRequestError):
                    update.result(timeout=5)
    finally:
        release_update.set()
        lease.release()
        api_router._server_instance = None
