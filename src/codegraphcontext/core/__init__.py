# src/codegraphcontext/core/__init__.py
"""
Core database management module.

Supports Neo4j, FalkorDB Lite, remote FalkorDB, KùzuDB, and LadybugDB backends.

Explicit backend selection (see ``get_database_manager``):
- ``CGC_RUNTIME_DB_TYPE`` — per-invocation override (CLI ``--database`` / MCP resolved context).
- ``DEFAULT_DATABASE`` — configured default from ``cgc config db …`` / CodeGraphContext ``.env``.

When neither is set, implicit selection:
- Remote FalkorDB if ``FALKORDB_HOST`` is set (explicit remote signal).
- Else LadybugDB when its native engine is usable; it is included in the default installation.
- If LadybugDB is unavailable, fall back to usable FalkorDB Lite, then KùzuDB if installed.
- Neo4j or Nornic are selected when configured and their optional driver is installed.
"""
import os
import platform
import sys
from pathlib import Path
from typing import Union, Optional
import importlib
import importlib.util
import functools

# Retained for compatibility with callers that inspect this module attribute.
# FalkorDB startup failures are now tracked by FalkorDBManager per database
# configuration, rather than disabling the backend for the whole process.
_FALKORDB_DISABLED = False


def _fallback_db_path_for(db_path: Optional[str], target_backend: str) -> Optional[str]:
    """
    Adjust a backend-specific db_path when falling back to another backend.

    Default context DB paths embed the backend name as the last path segment
    (e.g. ``…/db/neo4j`` or ``…/db/falkordb``). Reusing such a path for another
    backend would make the embedded engine open a different backend's data
    directory, so swap the trailing segment for the target backend's name.
    Paths that don't end in a known backend name are treated as custom paths and
    returned unchanged.
    """
    if not db_path:
        return db_path
    p = Path(db_path)
    source_backend = p.name.lower()
    known_backend_paths = {
        'falkordb',
        'falkordb.db',
        'falkordb-remote',
        'kuzudb',
        'kuzudb.db',
        'ladybugdb',
        'neo4j',
        'nornic',
    }
    target_names = {
        'falkordb': {'falkordb', 'falkordb.db'},
        'falkordb-remote': {'falkordb-remote'},
        'kuzudb': {'kuzudb', 'kuzudb.db'},
        'ladybugdb': {'ladybugdb'},
        'neo4j': {'neo4j'},
        'nornic': {'nornic'},
    }
    if source_backend in known_backend_paths and source_backend not in target_names.get(
        target_backend.lower(), {target_backend.lower()}
    ):
        from codegraphcontext.utils.debug_log import warning_logger

        new_path = str(p.parent / target_backend)
        warning_logger(
            f"Database fallback: db_path '{db_path}' points at the '{source_backend}' data "
            f"directory; using '{new_path}' for backend '{target_backend}' instead."
        )
        return new_path
    return db_path


def _try_fallback_backends(
    db_path: Optional[str], candidates, *, reason: str, preserve_db_path: bool = False
):
    """
    Return the first available backend from ``candidates``, naming it in the log.

    Every fallback path funnels through here so the backend a user actually ends
    up on is always recorded. A silent switch is effectively undebuggable: the
    only trace left is that the *requested* backend was requested, so an
    interpreter where FalkorDB Lite cannot load looks identical to one where it
    loaded fine, and queries run against a different (often empty) database.

    Returns ``None`` when no candidate is available, so callers keep control of
    the error they raise.

    ``preserve_db_path`` is for an explicit runtime path override: use that exact
    path even when its final segment looks like a backend name.
    """
    from codegraphcontext.utils.debug_log import warning_logger

    for name in candidates:
        if name == 'kuzudb' and _is_kuzudb_available():
            from .database_kuzu import KuzuDBManager
            path = db_path if preserve_db_path else _fallback_db_path_for(db_path, 'kuzudb')
            warning_logger(
                f"Database backend fallback: {reason} "
                f"Now using KùzuDB at {path or 'default path'}."
            )
            return KuzuDBManager(db_path=path)
        if name == 'ladybugdb' and _is_ladybugdb_available():
            from .database_ladybug import LadybugDBManager
            path = db_path if preserve_db_path else _fallback_db_path_for(db_path, 'ladybugdb')
            warning_logger(
                f"Database backend fallback: {reason} "
                f"Now using LadybugDB at {path or 'default path'}."
            )
            return LadybugDBManager(db_path=path)
        if name == 'neo4j' and _is_neo4j_configured() and _is_neo4j_available():
            from .database import DatabaseManager
            warning_logger(f"Database backend fallback: {reason} Now using Neo4j Server.")
            return DatabaseManager()
        if name == 'nornic' and _is_nornic_configured() and _is_neo4j_available():
            from .database_nornic import NornicDBManager
            warning_logger(f"Database backend fallback: {reason} Now using Nornic DB.")
            return NornicDBManager()
    return None


def mark_falkordb_unavailable() -> None:
    """Compatibility hook; startup failures are scoped by FalkorDBManager."""


def is_falkordb_usable() -> bool:
    """True when FalkorDB Lite is available on this system."""
    return _is_falkordb_available()

def _kuzudb_install_guidance() -> str:
    """Return an install hint that respects Kùzu's platform wheel support."""
    if sys.version_info >= (3, 14) and sys.platform != "linux":
        return (
            "The [kuzu] extra does not install Kùzu for Python 3.14+ on "
            "non-Linux platforms. Use the included LadybugDB backend, "
            "or use Python <3.14 or Linux."
        )
    return (
        "Install it with `python -m pip install "
        "'codegraphcontext-fork[kuzu]'` if needed."
    )


def _is_kuzudb_available() -> bool:
    """Check if KùzuDB is installed."""
    try:
        return importlib.util.find_spec("kuzu") is not None
    except ImportError:
        return False

def _is_neo4j_available() -> bool:
    """Check if the optional Neo4j/Nornic driver is installed."""
    try:
        return importlib.util.find_spec("neo4j") is not None
    except (ImportError, ValueError):
        return False

def _default_embedded_backend_name() -> str:
    """Return the default embedded backend, then an installed optional fallback."""
    if _is_ladybugdb_available():
        return "ladybugdb"
    if is_falkordb_usable():
        return "falkordb"
    if _is_kuzudb_available():
        return "kuzudb"
    return "ladybugdb"


def _default_backend_preview() -> tuple[str, tuple[str, ...]]:
    """Return automatic backend candidates without loading native engines.

    This preview is used while displaying configuration guidance, before a
    backend is selected. Probe package presence only here; native availability
    checks load the engine extension and can conflict with another embedded
    engine later in the same process. The factory performs the full check when
    it actually selects a backend.
    """
    if _is_falkordb_remote_configured():
        return "falkordb-remote", ()
    try:
        ladybug_installed = importlib.util.find_spec("ladybug") is not None
    except (ImportError, ValueError):
        ladybug_installed = False
    if ladybug_installed:
        # Do not load Ladybug's native engine to decide the preview: that can
        # conflict with another embedded engine later in the process. Keep the
        # possible factory fallbacks visible if the native probe fails.
        fallbacks = []
        if is_falkordb_usable():
            fallbacks.append("falkordb")
        if _is_kuzudb_available():
            fallbacks.append("kuzudb")
        if _is_neo4j_configured() and _is_neo4j_available():
            fallbacks.append("neo4j")
        if _is_nornic_configured() and _is_neo4j_available():
            fallbacks.append("nornic")
        return "ladybugdb", tuple(fallbacks)

    if is_falkordb_usable():
        fallbacks = []
        if _is_kuzudb_available():
            fallbacks.append("kuzudb")
        else:
            if _is_neo4j_configured() and _is_neo4j_available():
                fallbacks.append("neo4j")
            if _is_nornic_configured() and _is_neo4j_available():
                fallbacks.append("nornic")
        return "falkordb", tuple(fallbacks)
    if _is_kuzudb_available():
        return "kuzudb", ()
    if _is_neo4j_configured() and _is_neo4j_available():
        return "neo4j", ()
    if _is_nornic_configured() and _is_neo4j_available():
        return "nornic", ()
    return _default_embedded_backend_name(), ()

@functools.lru_cache(maxsize=1)
def ladybugdb_unavailable_reason() -> Optional[str]:
    """Return None when LadybugDB can actually run here, else why it cannot.

    An installed ``ladybug`` package is not enough: its engine is either a
    bundled pybind extension or the ``lbug`` C-API shared library loaded via
    ctypes, and some wheels (Windows, #1731) ship neither. Probing only the
    package made such an interpreter look ready, so explicit selection and
    fallback both picked a backend that died on first use with
    "Could not find lbug C API shared library".
    """
    try:
        if importlib.util.find_spec("ladybug") is None:
            return "LadybugDB is not installed"
    except (ImportError, ValueError):
        return "LadybugDB is not installed"
    try:
        from ladybug._backend import get_capi_module, get_pybind_module
    except ImportError:
        # Package layout without the dual-backend loader: importing it is the
        # best available signal.
        try:
            import ladybug  # noqa: F401
        except Exception as e:
            return f"LadybugDB failed to import: {e}"
        return None
    try:
        if get_pybind_module() is not None:
            return None
    except Exception:
        pass
    # ladybug swallows the extension's ImportError before falling back to the
    # C-API library, so the fallback's "Could not find lbug C API shared
    # library" hides the real cause (e.g. a DLL load failure). Re-import once
    # to recover it for the message.
    try:
        importlib.import_module("ladybug._lbug")
        pybind_error = None
    except Exception as e:
        pybind_error = f"{type(e).__name__}: {e}"
    try:
        get_capi_module()
    except Exception as e:
        detail = f"C-API: {e}"
        if pybind_error:
            detail = f"extension: {pybind_error}; {detail}"
        return f"LadybugDB is installed but its native engine could not be loaded ({detail})"
    return None


def _is_ladybugdb_available() -> bool:
    """Check if LadybugDB is installed and its native engine loads."""
    return ladybugdb_unavailable_reason() is None

def _is_falkordb_available() -> bool:
    """Check if FalkorDB Lite is installed (Unix only)."""
    if platform.system() == "Windows":
        return False

    import sys
    if sys.version_info < (3, 12):
        return False
    try:
        import redislite
        return hasattr(redislite, 'falkordb_client')
    except ImportError:
        return False

def _is_falkordb_remote_configured() -> bool:
    """Check if a remote FalkorDB host is configured."""
    return bool(os.getenv('FALKORDB_HOST'))

def _is_neo4j_configured() -> bool:
    """Check if Neo4j is configured with credentials."""
    return all([
        os.getenv('NEO4J_URI'),
        os.getenv('NEO4J_USERNAME'),
        os.getenv('NEO4J_PASSWORD')
    ])

def _is_nornic_configured() -> bool:
    """Check if Nornic is configured with credentials."""
    return all([
        os.getenv('NORNIC_URI'),
        os.getenv('NORNIC_USERNAME'),
        os.getenv('NORNIC_PASSWORD')
    ])

def get_database_manager(db_path: Optional[str] = None) -> Union['DatabaseManager', 'FalkorDBManager', 'FalkorDBRemoteManager', 'KuzuDBManager', 'NornicDBManager', 'LadybugDBManager']:
    """
    Factory function to get the appropriate database manager based on configuration.

    Selection logic:
    1. Runtime override: ``CGC_RUNTIME_DB_TYPE`` (CLI ``--database``, MCP context).
    2. Configured default: ``DEFAULT_DATABASE`` (``cgc config db …``, CodeGraphContext ``.env``).
    3. Implicit: ``FALKORDB_HOST`` → remote FalkorDB; otherwise LadybugDB, then
       optional FalkorDB Lite and KùzuDB fallbacks. Configured Neo4j/Nornic need the ``neo4j`` extra.
    """
    from codegraphcontext.utils.debug_log import info_logger

    db_type = os.getenv("CGC_RUNTIME_DB_TYPE") or os.getenv("DEFAULT_DATABASE")
    runtime_db_path = os.getenv("CGC_RUNTIME_DB_PATH")
    preserve_db_path = bool(runtime_db_path and runtime_db_path == db_path)

    if db_type:
        db_type = db_type.lower()
        if db_type == 'kuzudb':
            if not _is_kuzudb_available():
                kuzu_install_guidance = _kuzudb_install_guidance()
                mgr = _try_fallback_backends(
                    db_path,
                    ('ladybugdb', 'neo4j', 'nornic'),
                    reason=(
                        "database was set to 'kuzudb' but Kùzu is not installed. "
                        f"{kuzu_install_guidance}"
                    ),
                    preserve_db_path=preserve_db_path,
                )
                if mgr is not None:
                    return mgr
                raise ValueError(
                    "Database set to 'kuzudb' but Kùzu is not installed.\n"
                    f"{kuzu_install_guidance}"
                )
            from .database_kuzu import KuzuDBManager
            info_logger(f"Using KùzuDB (explicit) at {db_path or 'default path'}")
            return KuzuDBManager(db_path=db_path)

        elif db_type == 'falkordb':
            if not is_falkordb_usable():
                mgr = _try_fallback_backends(
                    db_path,
                    ('kuzudb', 'ladybugdb', 'neo4j', 'nornic'),
                    reason=(
                        "FalkorDB Lite is not supported or not installed here. "
                        "Install it with `python -m pip install 'codegraphcontext-fork[falkordb-embedded]'` if needed."
                    ),
                    preserve_db_path=preserve_db_path,
                )
                if mgr is not None:
                    return mgr
                raise ValueError(
                    "Database set to 'falkordb' but FalkorDB Lite is not installed or not supported on this OS.\n"
                    "Run `python -m pip install 'codegraphcontext-fork[falkordb-embedded]'`, or choose an installed backend."
                )
            
            from .database_falkordb import FalkorDBManager, FalkorDBUnavailableError
            try:
                mgr = FalkorDBManager(db_path=db_path)
                mgr.get_driver()
                info_logger(f"Using FalkorDB Lite (explicit) at {db_path or 'default path'}")
                return mgr
            except FalkorDBUnavailableError as falkor_err:
                mark_falkordb_unavailable()
                mgr = _try_fallback_backends(
                    db_path,
                    ('kuzudb', 'ladybugdb', 'neo4j', 'nornic'),
                    reason=(
                        f"FalkorDB Lite was requested but is not functional ({falkor_err}). "
                        "Install it with `python -m pip install 'codegraphcontext-fork[falkordb-embedded]'` if needed."
                    ),
                    preserve_db_path=preserve_db_path,
                )
                if mgr is not None:
                    return mgr
                raise

        elif db_type == 'falkordb-remote':
            if not _is_falkordb_remote_configured():
                raise ValueError(
                    "Database set to 'falkordb-remote' but FALKORDB_HOST is not set.\n"
                    "Set the FALKORDB_HOST environment variable to your remote FalkorDB host."
                )
            from .database_falkordb_remote import FalkorDBRemoteManager
            info_logger("Using remote FalkorDB (explicit)")
            return FalkorDBRemoteManager()

        elif db_type == 'neo4j':
            if not _is_neo4j_configured():
                raise ValueError("Database set to 'neo4j' but it is not configured.\nRun 'cgc neo4j setup' to configure Neo4j.")
            if not _is_neo4j_available():
                raise ValueError(
                    "Neo4j support is not installed. Run `python -m pip install 'codegraphcontext-fork[neo4j]'`."
                )
            from .database import DatabaseManager
            info_logger("Using Neo4j Server (explicit)")
            return DatabaseManager()

        elif db_type == 'nornic':
            if not _is_nornic_configured():
                raise ValueError("Database set to 'nornic' but it is not configured.")
            if not _is_neo4j_available():
                raise ValueError(
                    "Nornic support requires the Neo4j driver. "
                    "Run `python -m pip install 'codegraphcontext-fork[neo4j]'`."
                )
            from .database_nornic import NornicDBManager
            info_logger("Using Nornic DB (explicit)")
            return NornicDBManager()
        elif db_type == 'ladybugdb':
            if not _is_ladybugdb_available():
                raise ValueError(
                    f"Database set to 'ladybugdb' but {ladybugdb_unavailable_reason() or 'LadybugDB is not available'}.\n"
                    "Run `python -m pip install 'codegraphcontext-fork[ladybug]'`, "
                    f"or {_kuzudb_install_guidance()}"
                )
            from .database_ladybug import LadybugDBManager
            info_logger(f"Using LadybugDB (explicit) at {db_path or 'default path'}")
            return LadybugDBManager(db_path=db_path)
        else:
            raise ValueError(f"Unknown database type: '{db_type}'. Use 'kuzudb', 'ladybugdb', 'falkordb', 'falkordb-remote', 'neo4j', or 'nornic'.")

    # Implicit: remote FalkorDB when FALKORDB_HOST is set (explicit infra signal)
    if _is_falkordb_remote_configured():
        from .database_falkordb_remote import FalkorDBRemoteManager
        info_logger("Using remote FalkorDB (auto-detected via FALKORDB_HOST)")
        return FalkorDBRemoteManager()

    # Implicit: LadybugDB is the embedded default, even when optional backend
    # extras are installed. Optional engines are fallback choices only when
    # LadybugDB's native engine is unavailable on this interpreter.
    if _is_ladybugdb_available():
        from .database_ladybug import LadybugDBManager
        ladybug_path = (
            db_path
            if preserve_db_path
            else _fallback_db_path_for(db_path, 'ladybugdb')
        )
        info_logger(f"Using LadybugDB (default) at {ladybug_path or 'default path'}")
        return LadybugDBManager(db_path=ladybug_path)

    # Implicit fallback: use FalkorDB Lite when its optional extra is usable.
    if is_falkordb_usable():
        from .database_falkordb import FalkorDBManager, FalkorDBUnavailableError
        falkordb_path = (
            db_path
            if preserve_db_path
            else _fallback_db_path_for(db_path, 'falkordb')
        )
        try:
            mgr = FalkorDBManager(db_path=falkordb_path)
            mgr.get_driver()
            info_logger(
                f"Using FalkorDB Lite (automatic selection) at "
                f"{falkordb_path or 'default path'}"
            )
            return mgr
        except FalkorDBUnavailableError as falkor_err:
            mark_falkordb_unavailable()
            info_logger(
                f"FalkorDB Lite not functional in this environment ({falkor_err}). "
                "Falling back to an installed embedded backend."
            )
            # fall through to KùzuDB below

    # Implicit fallback: use KùzuDB when its optional extra is installed.
    if _is_kuzudb_available():
        from .database_kuzu import KuzuDBManager
        kuzu_path = (
            db_path
            if preserve_db_path
            else _fallback_db_path_for(db_path, 'kuzudb')
        )
        info_logger(f"Using KùzuDB (automatic selection) at {kuzu_path or 'default path'}")
        return KuzuDBManager(db_path=kuzu_path)

    # Implicit: Neo4j when configured
    if _is_neo4j_configured() and _is_neo4j_available():
        from .database import DatabaseManager
        info_logger("Using Neo4j Server (auto-detected)")
        return DatabaseManager()

    # Implicit: Nornic when configured
    if _is_nornic_configured() and _is_neo4j_available():
        from .database_nornic import NornicDBManager
        info_logger("Using Nornic DB (auto-detected)")
        return NornicDBManager()

    error_msg = "No database backend available.\n"
    error_msg += (
        "LadybugDB is included by default. "
        f"{_kuzudb_install_guidance()}\n"
    )

    if platform.system() != "Windows":
        error_msg += "Alternative: `python -m pip install 'codegraphcontext-fork[falkordb-embedded]'`.\n"

    error_msg += (
        "For Neo4j or Nornic, install `python -m pip install 'codegraphcontext-fork[neo4j]'` "
        "and configure the service."
    )

    raise ValueError(error_msg)

# Lazy backward-compatibility exports — avoids crashing when optional
# database drivers (neo4j, falkordb, real_ladybug, …) are not installed.
# Uses PEP 562 module-level __getattr__ so that:
#   from codegraphcontext.core import DatabaseManager
# still works, but only triggers the real import when actually accessed.
_LAZY_IMPORTS = {
    'DatabaseManager': '.database',
    'FalkorDBManager': '.database_falkordb',
    'FalkorDBRemoteManager': '.database_falkordb_remote',
    'KuzuDBManager': '.database_kuzu',
    'LadybugDBManager': '.database_ladybug',
    'NornicDBManager': '.database_nornic',
}

def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        import importlib
        module = importlib.import_module(_LAZY_IMPORTS[name], __package__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    'DatabaseManager',
    'FalkorDBManager',
    'FalkorDBRemoteManager',
    'KuzuDBManager',
    'LadybugDBManager',
    'NornicDBManager',
    'get_database_manager',
]
