"""End-to-end embedded database ownership checks using disposable paths."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from codegraphcontext.core.database_ownership import (
    find_active_gateway,
    owner_metadata_path,
    read_database_owner,
)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _runtime_env(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "CGC_HOME": str(tmp_path / "home"),
            "CGC_INSTANCE_NAME": "H4OwnershipTest",
            "CGC_RUNTIME_DB_TYPE": "ladybugdb",
            "CGC_RUNTIME_DB_PATH": str(tmp_path / "db" / "ladybugdb"),
            "CGC_EMBEDDED_BUFFER_POOL_MB": "64",
            "CGC_ALLOWED_ROOTS": str(tmp_path),
        }
    )
    env.pop("CGC_API_KEY", None)
    return env


def _run_cli(command, repo, env, timeout=120):
    return subprocess.run(
        [sys.executable, "-m", "codegraphcontext", *command],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _start_gateway(tmp_path, repo, env, port=None, expected_database="ladybugdb"):
    port = port or _free_port()
    log_path = tmp_path / f"gateway-{port}.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "codegraphcontext",
            "api",
            "start",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=repo,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log.close()

    deadline = time.monotonic() + 45
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/v1/status", timeout=2
            ) as response:
                status = json.loads(response.read())
            assert status["status"] == "ok"
            assert status["data"]["database"] == expected_database
            return process, port, log_path
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.2)

    process_output = log_path.read_text(encoding="utf-8", errors="replace")
    if process.poll() is None:
        process.kill()
        process.wait(timeout=10)
    pytest.fail(
        f"Gateway did not become ready ({last_error}); log tail:\n"
        + "\n".join(process_output.splitlines()[-30:])
    )


def _start_asgi_gateway(tmp_path, repo, env, port):
    log_path = tmp_path / f"asgi-gateway-{port}.log"
    log = log_path.open("w", encoding="utf-8")
    process_env = env.copy()
    process_env.pop("CGC_GATEWAY_ENDPOINT", None)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "codegraphcontext.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=repo,
        env=process_env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log.close()

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        owner = read_database_owner(env["CGC_RUNTIME_DB_PATH"])
        if owner and owner.get("pid") == process.pid and owner.get("gateway"):
            return process, log_path
        time.sleep(0.05)

    process_output = log_path.read_text(encoding="utf-8", errors="replace")
    if process.poll() is None:
        process.kill()
        process.wait(timeout=10)
    pytest.fail(
        "Standard ASGI gateway did not register its owner endpoint; log tail:\n"
        + "\n".join(process_output.splitlines()[-30:])
    )


def _stop_gateway(process):
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=20)


def _make_repo(tmp_path, file_count=1):
    repo = tmp_path / "repo"
    repo.mkdir()
    for index in range(file_count):
        (repo / f"module_{index}.py").write_text(
            f"def function_{index}():\n    return {index}\n", encoding="utf-8"
        )
    return repo


@pytest.mark.parametrize("quiet", [False, True], ids=["cli", "git-hook"])
def test_update_delegates_to_the_active_gateway(tmp_path, quiet):
    repo = _make_repo(tmp_path)
    env = _runtime_env(tmp_path)
    gateway, port, _log_path = _start_gateway(tmp_path, repo, env)
    try:
        owner = read_database_owner(env["CGC_RUNTIME_DB_PATH"])
        assert owner["pid"] == gateway.pid
        registration = find_active_gateway(
            env["CGC_RUNTIME_DB_PATH"], "ladybugdb"
        )
        assert registration is not None
        assert owner["gateway"]["port"] == registration.port
        assert owner["gateway"]["endpoint"] == registration.endpoint

        command = ["update"]
        if quiet:
            command.append("--quiet")
        command.append(str(repo))
        result = _run_cli(command, repo, env)

        assert result.returncode == 0, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "Could not set lock on file" not in output
        if not quiet:
            assert "gateway" in output.lower()

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/v1/status", timeout=5
        ) as response:
            assert json.loads(response.read())["message"] == "Connected"
    finally:
        _stop_gateway(gateway)


def test_standard_asgi_startup_allows_update_before_public_request(tmp_path):
    repo = _make_repo(tmp_path)
    env = _runtime_env(tmp_path)
    port = _free_port()
    gateway, _log_path = _start_asgi_gateway(tmp_path, repo, env, port)
    try:
        registration = find_active_gateway(
            env["CGC_RUNTIME_DB_PATH"], "ladybugdb"
        )
        assert registration is not None

        result = _run_cli(["update", str(repo)], repo, env)

        assert result.returncode == 0, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "gateway" in output.lower()
        assert "Could not set lock on file" not in output
    finally:
        _stop_gateway(gateway)


def test_update_delegates_when_configured_backend_falls_back(tmp_path):
    repo = _make_repo(tmp_path)
    env = _runtime_env(tmp_path)
    configured_path = tmp_path / "db" / "falkordb"
    env["CGC_RUNTIME_DB_TYPE"] = "falkordb"
    env["CGC_RUNTIME_DB_PATH"] = str(configured_path)

    force_fallback = tmp_path / "force-falkordb-fallback"
    force_fallback.mkdir()
    (force_fallback / "sitecustomize.py").write_text(
        "import codegraphcontext.core as core\n"
        "core.is_falkordb_usable = lambda: False\n"
        "core._is_kuzudb_available = lambda: False\n",
        encoding="utf-8",
    )
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(force_fallback), env.get("PYTHONPATH")) if part
    )

    gateway, _port, _log_path = _start_gateway(
        tmp_path, repo, env, expected_database="ladybugdb"
    )
    try:
        owner = read_database_owner(str(configured_path))
        assert owner["backend"] == "ladybugdb"
        assert owner["database_path"] == str(configured_path.resolve())

        result = _run_cli(["update", str(repo)], repo, env)

        assert result.returncode == 0, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "gateway" in output.lower()
        assert "Could not set lock on file" not in output
    finally:
        _stop_gateway(gateway)


def test_gateway_discovery_uses_factory_backend_before_database_type_env(tmp_path):
    repo = _make_repo(tmp_path)
    env = _runtime_env(tmp_path)
    env.pop("CGC_RUNTIME_DB_TYPE")
    env["DEFAULT_DATABASE"] = "ladybugdb"
    env["DATABASE_TYPE"] = "falkordb"
    env["CGC_RUNTIME_DB_PATH"] = str(tmp_path / "db" / "falkordb")
    gateway, _port, _log_path = _start_gateway(tmp_path, repo, env)
    try:
        owner = read_database_owner(env["CGC_RUNTIME_DB_PATH"])
        assert owner["backend"] == "ladybugdb"

        result = _run_cli(["update", str(repo)], repo, env)

        assert result.returncode == 0, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "gateway" in output.lower()
        assert "Could not set lock on file" not in output
    finally:
        _stop_gateway(gateway)


def test_update_delegates_paths_outside_gateway_api_roots(tmp_path):
    repo = _make_repo(tmp_path)
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    env = _runtime_env(tmp_path)
    env["CGC_ALLOWED_ROOTS"] = str(allowed_root)
    gateway, _port, _log_path = _start_gateway(tmp_path, allowed_root, env)
    try:
        result = _run_cli(["update", str(repo)], repo, env)

        assert result.returncode == 0, result.stdout + result.stderr
        output = result.stdout + result.stderr
        assert "gateway" in output.lower()
        assert "outside this gateway's allowed roots" not in output
    finally:
        _stop_gateway(gateway)


@pytest.mark.parametrize("quiet", [False, True], ids=["cli", "git-hook"])
def test_failed_gateway_connection_does_not_open_a_second_writer(tmp_path, quiet):
    repo = _make_repo(tmp_path)
    env = _runtime_env(tmp_path)
    gateway, _port, _log_path = _start_gateway(tmp_path, repo, env)
    database_path = env["CGC_RUNTIME_DB_PATH"]
    try:
        metadata_path = owner_metadata_path(database_path)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["owner"]["gateway"]["endpoint"] = "http://127.0.0.1:1"
        metadata["owner"]["gateway"]["port"] = 1
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        assert find_active_gateway(database_path, "ladybugdb", timeout=0.1) is None
        command = ["update"]
        if quiet:
            command.append("--quiet")
        command.append(str(repo))
        result = _run_cli(command, repo, env)

        assert result.returncode == 1
        output = result.stdout + result.stderr
        assert "owned by" in output.lower()
        assert str(gateway.pid) in output
        assert "Could not set lock on file" not in output
        if quiet:
            assert "Database Ownership Error" in result.stderr
    finally:
        _stop_gateway(gateway)


def test_crashed_gateway_stale_registration_is_reclaimed_safely(tmp_path):
    repo = _make_repo(tmp_path)
    env = _runtime_env(tmp_path)
    # Keep the crash window read-only for the embedded engine. LadybugDB may
    # leave an incomplete WAL if SIGKILL interrupts initial schema creation.
    initialized = _run_cli(["update", str(repo)], repo, env)
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr

    gateway, _port, _log_path = _start_gateway(tmp_path, repo, env)
    database_path = env["CGC_RUNTIME_DB_PATH"]

    gateway.kill()
    gateway.wait(timeout=10)
    assert read_database_owner(database_path)["pid"] == gateway.pid
    assert find_active_gateway(database_path, "ladybugdb", timeout=0.1) is None

    result = _run_cli(["update", str(repo)], repo, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert read_database_owner(database_path) is None


def test_gateway_releases_database_for_shutdown_and_restart(tmp_path):
    repo = _make_repo(tmp_path)
    env = _runtime_env(tmp_path)
    port = _free_port()

    first, _, _ = _start_gateway(tmp_path, repo, env, port=port)
    database_path = env["CGC_RUNTIME_DB_PATH"]
    assert read_database_owner(database_path)["pid"] == first.pid
    _stop_gateway(first)
    assert read_database_owner(database_path) is None

    second, _, _ = _start_gateway(tmp_path, repo, env, port=port)
    try:
        assert second.pid != first.pid
        assert read_database_owner(database_path)["pid"] == second.pid
    finally:
        _stop_gateway(second)


def test_simultaneous_cli_updates_never_open_two_embedded_writers(tmp_path):
    repo = _make_repo(tmp_path, file_count=80)
    env = _runtime_env(tmp_path)
    command = [
        sys.executable,
        "-m",
        "codegraphcontext",
        "update",
        str(repo),
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(2)
    ]
    results = []
    for process in processes:
        output, _ = process.communicate(timeout=180)
        results.append((process.returncode, output))

    assert any(code == 0 for code, _ in results), results
    combined = "\n".join(output for _, output in results)
    assert "Could not set lock on file" not in combined
    for code, output in results:
        if code != 0:
                assert any(
                    message in output.lower()
                    for message in ("owned by", "locked by another process")
                ), output
