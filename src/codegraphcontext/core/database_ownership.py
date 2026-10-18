"""Cross-process ownership for local embedded databases.

Database engines such as LadybugDB allow only one process to open a writable
database. This lease records which CGC process owns a canonical database path
and, for a local API gateway, how clients can safely delegate work to it. The
operating-system lock is authoritative; metadata is only a discovery aid.
"""

from __future__ import annotations

import errno
from http.client import IncompleteRead
import ipaddress
import json
import os
import secrets
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from codegraphcontext.runtime_paths import get_instance_name


_OWNER_RECORD_FORMAT = 1
_MAX_RESPONSE_BYTES = 64 * 1024
_process_gateway_endpoint: Optional[tuple[str, int]] = None


def local_gateway_endpoint(host: str, port: int) -> Optional[str]:
    """Return the loopback URL for a gateway reachable from this machine.

    Wildcard listeners are reachable through IPv4 loopback. A listener bound
    to a specific non-loopback interface is intentionally not advertised for
    local CLI delegation.
    """
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    host = str(host).strip().strip("[]")
    if host.lower() == "localhost":
        return f"http://127.0.0.1:{port}"
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    if address.is_unspecified:
        loopback = "127.0.0.1" if address.version == 4 else "::1"
    elif address.is_loopback:
        loopback = address.compressed
    else:
        return None
    if ":" in loopback:
        loopback = f"[{loopback}]"
    return f"http://{loopback}:{port}"


def _local_opener():
    """Build an opener that cannot send the owner token through proxies or redirects."""
    from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

    class NoRedirectHandler(HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    return build_opener(ProxyHandler({}), NoRedirectHandler())


def _canonical_database_path(database_path: str | Path) -> str:
    raw = os.path.expanduser(os.path.expandvars(str(database_path)))
    return str(Path(raw).resolve())


def owner_metadata_path(database_path: str | Path) -> Path:
    """Return the stable sidecar path used for the database lease and metadata."""
    return Path(_canonical_database_path(database_path) + ".cgc-owner")


def set_process_gateway_endpoint(
    endpoint: str, *, port: int
) -> Optional[tuple[str, int]]:
    """Set the in-process gateway used by embedded managers and return the old value."""
    parsed = urlsplit(endpoint)
    try:
        endpoint_port = parsed.port
    except ValueError as exc:
        raise ValueError("The local gateway endpoint has an invalid port") from exc
    if (
        parsed.scheme != "http"
        or not _is_loopback_host(parsed.hostname)
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not isinstance(port, int)
        or not 1 <= port <= 65535
        or endpoint_port != port
    ):
        raise ValueError("A local gateway must use a matching loopback HTTP endpoint")

    global _process_gateway_endpoint
    previous = _process_gateway_endpoint
    _process_gateway_endpoint = (endpoint.rstrip("/"), port)
    return previous


def restore_process_gateway_endpoint(
    endpoint: Optional[tuple[str, int]],
) -> None:
    """Restore the previous in-process gateway endpoint after API shutdown."""
    global _process_gateway_endpoint
    _process_gateway_endpoint = endpoint


def register_configured_gateway(lease: "DatabaseOwnershipLease") -> None:
    """Register this process's gateway after an embedded DB is fully opened."""
    configured = _process_gateway_endpoint
    if configured is not None:
        endpoint, port = configured
    else:
        endpoint = os.getenv("CGC_GATEWAY_ENDPOINT")
        if not endpoint:
            return
        try:
            port = urlsplit(endpoint).port
        except ValueError:
            return
        if port is None:
            return
    lease.register_gateway(endpoint, port=port)


def _read_record(database_path: str | Path) -> Optional[dict[str, Any]]:
    path = owner_metadata_path(database_path)
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_RESPONSE_BYTES:
            return None
        record = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("format") != _OWNER_RECORD_FORMAT:
        return None
    if record.get("database_path") != _canonical_database_path(database_path):
        return None
    return record


def read_database_owner(database_path: str | Path) -> Optional[dict[str, Any]]:
    """Read current owner metadata, or return ``None`` when no owner is recorded."""
    record = _read_record(database_path)
    owner = record.get("owner") if record else None
    return owner if isinstance(owner, dict) else None


def _write_record(fd: int, database_path: str, owner: Optional[dict[str, Any]]) -> None:
    record = {
        "format": _OWNER_RECORD_FORMAT,
        "database_path": database_path,
        "owner": owner,
    }
    data = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]
    os.fsync(fd)


def _try_lock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _is_loopback_host(host: Optional[str]) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _pid_is_alive(pid: Any, hostname: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if hostname != socket.gethostname():
        # A shared filesystem can expose another host's metadata, but it cannot
        # prove that the remote process or lock is safe to reuse locally.
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


class DatabaseOwnershipError(RuntimeError):
    """Raised when a second CGC process tries to own an embedded DB path."""

    def __init__(self, database_path: str, owner: Optional[dict[str, Any]] = None):
        self.database_path = database_path
        self.owner = owner or {}
        pid = self.owner.get("pid")
        gateway = self.owner.get("gateway")
        if pid and isinstance(gateway, dict) and gateway.get("endpoint"):
            message = (
                f"Embedded database at {database_path} is already owned by CGC "
                f"process {pid} (registered gateway: {gateway['endpoint']}). "
                "Use that gateway if it is reachable, or stop the owner before "
                "running another database-backed command."
            )
        elif pid:
            message = (
                f"Embedded database at {database_path} is already owned by CGC "
                f"process {pid}. Stop that process before running another "
                "database-backed command."
            )
        else:
            message = (
                f"Embedded database at {database_path} is locked by another "
                "process. It may be an older CGC process or another database "
                "client; close that owner before retrying."
            )
        super().__init__(message)


class GatewayUnavailableError(RuntimeError):
    """Raised when a registered gateway cannot complete a local request."""


class GatewayRequestError(RuntimeError):
    """Raised when a live gateway rejects a delegated local operation."""


class DatabaseOwnershipLease:
    """Non-blocking process lease held while an embedded DB is open."""

    def __init__(self, database_path: str | Path, backend: str):
        self.database_path = _canonical_database_path(database_path)
        self.backend = str(backend).lower()
        self.instance_name = get_instance_name()
        self.lease_id = uuid.uuid4().hex
        self._token = secrets.token_urlsafe(32)
        self._fd: Optional[int] = None
        self._owner: Optional[dict[str, Any]] = None
        self.metadata_path = owner_metadata_path(self.database_path)

    @property
    def is_acquired(self) -> bool:
        return self._fd is not None

    @property
    def token(self) -> str:
        return self._token

    @property
    def owner(self) -> Optional[dict[str, Any]]:
        return dict(self._owner) if self._owner is not None else None

    def acquire(self) -> "DatabaseOwnershipLease":
        if self._fd is not None:
            return self
        self.metadata_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.metadata_path, flags, 0o600)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            _try_lock_fd(fd)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            if exc.errno not in {
                errno.EACCES,
                errno.EAGAIN,
                errno.EDEADLK,
                errno.EWOULDBLOCK,
            }:
                raise
            raise DatabaseOwnershipError(
                self.database_path, read_database_owner(self.database_path)
            ) from exc

        owner = {
            "backend": self.backend,
            "database_path": self.database_path,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "instance_name": self.instance_name,
            "lease_id": self.lease_id,
            "started_at": time.time(),
            "gateway": None,
        }
        try:
            _write_record(fd, self.database_path, owner)
        except BaseException:
            try:
                _unlock_fd(fd)
            finally:
                os.close(fd)
            raise
        self._fd = fd
        self._owner = owner
        return self

    def register_gateway(self, endpoint: str, *, port: int) -> dict[str, Any]:
        if self._fd is None or self._owner is None:
            raise RuntimeError("The database ownership lease must be acquired first")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or not _is_loopback_host(parsed.hostname)
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("A local gateway must use a loopback HTTP endpoint")
        try:
            endpoint_port = parsed.port
        except ValueError as exc:
            raise ValueError("The local gateway endpoint has an invalid port") from exc
        if not isinstance(port, int) or not 1 <= port <= 65535 or endpoint_port != port:
            raise ValueError("The gateway endpoint port must match its listening port")

        current = self._owner.get("gateway")
        token = (
            current.get("token")
            if isinstance(current, dict)
            and current.get("endpoint") == endpoint.rstrip("/")
            and current.get("port") == port
            else self._token
        )
        self._token = token
        gateway = {
            "endpoint": endpoint.rstrip("/"),
            "port": port,
            "token": token,
        }
        self._owner["gateway"] = gateway
        _write_record(self._fd, self.database_path, self._owner)
        return dict(gateway)

    def clear_gateway(self) -> None:
        if self._fd is None or self._owner is None:
            return
        self._owner["gateway"] = None
        _write_record(self._fd, self.database_path, self._owner)

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            _write_record(fd, self.database_path, None)
        except OSError:
            # The OS lock remains authoritative. A later owner can safely
            # replace stale metadata after acquiring that lock.
            pass
        finally:
            self._owner = None
            self._fd = None
            try:
                _unlock_fd(fd)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


@dataclass(frozen=True)
class GatewayRegistration:
    database_path: str
    backend: str
    pid: int
    hostname: str
    instance_name: str
    lease_id: str
    endpoint: str
    port: int
    token: str

    def update_repository(
        self,
        path: str | Path,
        *,
        cgcignore_path: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        payload = json.dumps(
            {
                "path": str(Path(path).resolve()),
                "cgcignore_path": cgcignore_path,
            }
        ).encode("utf-8")
        registration = self
        refreshed = False
        while True:
            request = Request(
                registration.endpoint + "/api/v1/local/update",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-CGC-Owner-Token": registration.token,
                },
                method="POST",
            )
            try:
                with _local_opener().open(request, timeout=timeout) as response:
                    data = _decode_json_response(response)
            except HTTPError as exc:
                detail = _http_error_detail(exc)
                if exc.code in {401, 403, 503} and not refreshed:
                    current = find_active_gateway(
                        registration.database_path, registration.backend
                    )
                    if current is None:
                        raise GatewayUnavailableError(
                            "The registered gateway is no longer available."
                        ) from exc
                    if current != registration:
                        registration = current
                        refreshed = True
                        continue
                raise GatewayRequestError(detail) from exc
            except (URLError, TimeoutError, OSError) as exc:
                if not refreshed:
                    current = find_active_gateway(
                        registration.database_path, registration.backend
                    )
                    if current is not None and current != registration:
                        registration = current
                        refreshed = True
                        continue
                raise GatewayUnavailableError(
                    f"The active gateway at {registration.endpoint} could not be reached."
                ) from exc
            break
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise GatewayRequestError(_response_error(data))
        return data


def _decode_json_response(response) -> Any:
    try:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except IncompleteRead as exc:
        raise GatewayRequestError(
            "The local gateway returned an incomplete response"
        ) from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise GatewayRequestError("The local gateway returned an oversized response")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayRequestError("The local gateway returned invalid JSON") from exc


def _http_error_detail(exc: HTTPError) -> str:
    try:
        raw = exc.read(_MAX_RESPONSE_BYTES + 1)
        payload = json.loads(raw.decode("utf-8")) if len(raw) <= _MAX_RESPONSE_BYTES else {}
    except (IncompleteRead, OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error")
        if detail:
            return str(detail)
    return f"The active gateway rejected the request with HTTP {exc.code}."


def _response_error(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error") or payload.get("message")
        if error:
            return str(error)
    return "The active gateway could not update the repository."


def find_active_gateway(
    database_path: str | Path,
    backend: Optional[str] = None,
    *,
    timeout: float = 0.5,
) -> Optional[GatewayRegistration]:
    """Return a verified same-host gateway for this DB path, if one is active."""
    owner = read_database_owner(database_path)
    if not owner:
        return None
    if owner.get("database_path") != _canonical_database_path(database_path):
        return None
    if backend and owner.get("backend") != str(backend).lower():
        return None
    if owner.get("hostname") != socket.gethostname():
        return None
    if not _pid_is_alive(owner.get("pid"), owner.get("hostname")):
        return None

    gateway = owner.get("gateway")
    if not isinstance(gateway, dict):
        return None
    endpoint = gateway.get("endpoint")
    token = gateway.get("token")
    try:
        port = int(gateway.get("port"))
        parsed = urlsplit(endpoint)
        endpoint_port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "http"
        or not _is_loopback_host(parsed.hostname)
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not isinstance(token, str)
        or not token
        or endpoint_port != port
    ):
        return None

    request = Request(
        endpoint.rstrip("/") + "/api/v1/local/ownership",
        headers={"X-CGC-Owner-Token": token},
    )
    try:
        with _local_opener().open(request, timeout=timeout) as response:
            status = _decode_json_response(response)
    except (HTTPError, URLError, TimeoutError, OSError, GatewayRequestError):
        return None
    if not isinstance(status, dict):
        return None

    expected_path = _canonical_database_path(database_path)
    matches = (
        status.get("database_path") == expected_path
        and status.get("backend") == owner.get("backend")
        and status.get("pid") == owner.get("pid")
        and status.get("hostname") == owner.get("hostname")
        and status.get("instance_name") == owner.get("instance_name")
        and status.get("lease_id") == owner.get("lease_id")
        and status.get("port") == port
    )
    if not matches:
        return None
    return GatewayRegistration(
        database_path=expected_path,
        backend=str(owner["backend"]),
        pid=int(owner["pid"]),
        hostname=str(owner["hostname"]),
        instance_name=str(owner["instance_name"]),
        lease_id=str(owner["lease_id"]),
        endpoint=endpoint.rstrip("/"),
        port=port,
        token=token,
    )
