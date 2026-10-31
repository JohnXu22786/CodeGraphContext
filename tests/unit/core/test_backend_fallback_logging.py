"""
Regression tests for #1331 — a backend fallback must name the backend it landed on.

The bug these pin down: when an explicitly requested backend was unavailable,
`get_database_manager` returned a *different* backend without logging which one.
The user's only signal was the banner naming the backend they *asked* for, so an
interpreter where FalkorDB Lite cannot load was indistinguishable from one where
it loaded fine — while queries silently ran against a different, often empty, DB.
"""
import sys
from types import SimpleNamespace

import pytest

import codegraphcontext.core as core


@pytest.fixture
def fallback_log(monkeypatch):
    """Capture warning_logger output emitted during backend selection."""
    messages = []

    import codegraphcontext.utils.debug_log as debug_log

    monkeypatch.setattr(debug_log, "warning_logger", messages.append)
    monkeypatch.setattr(debug_log, "info_logger", lambda *_a, **_k: None)
    return messages


def _only_backend(monkeypatch, available):
    """Make exactly one backend look available, and none of the others."""
    for name, flag in (
        ("_is_kuzudb_available", "kuzudb"),
        ("_is_ladybugdb_available", "ladybugdb"),
    ):
        monkeypatch.setattr(core, name, lambda f=flag: f == available)
    monkeypatch.setattr(core, "_is_neo4j_configured", lambda: available == "neo4j")
    monkeypatch.setattr(core, "_is_nornic_configured", lambda: available == "nornic")


def test_falkordb_to_kuzu_fallback_names_kuzu(monkeypatch, fallback_log):
    """The Failure Mode 1 from #1331: FalkorDB unusable, Kùzu takes over silently."""
    runtime_db_path = "/runtime/db/neo4j"
    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "falkordb")
    monkeypatch.setenv("CGC_RUNTIME_DB_PATH", runtime_db_path)
    monkeypatch.setattr(core, "is_falkordb_usable", lambda: False)
    _only_backend(monkeypatch, "kuzudb")

    sentinel = object()
    selected_paths = []
    fake = type(sys)("codegraphcontext.core.database_kuzu")
    fake.KuzuDBManager = lambda db_path=None: selected_paths.append(db_path) or sentinel
    monkeypatch.setitem(sys.modules, "codegraphcontext.core.database_kuzu", fake)

    assert core.get_database_manager(db_path=runtime_db_path) is sentinel
    assert selected_paths == [runtime_db_path]

    joined = " ".join(fallback_log)
    assert "fallback" in joined.lower(), f"no fallback logged: {fallback_log!r}"
    assert "Kùzu" in joined, f"fallback did not name the backend it landed on: {fallback_log!r}"
    assert "codegraphcontext-fork[falkordb-embedded]" in joined


@pytest.mark.parametrize(
    ("runtime_path", "db_path", "expected_path"),
    (
        (None, "/global/db/ladybugdb", "/global/db/falkordb"),
        (
            "/runtime/db/ladybugdb",
            "/runtime/db/ladybugdb",
            "/runtime/db/ladybugdb",
        ),
    ),
)
def test_implicit_falkordb_fallback_uses_its_path_and_preserves_runtime_override(
    monkeypatch, fallback_log, runtime_path, db_path, expected_path
):
    for name in ("CGC_RUNTIME_DB_TYPE", "DEFAULT_DATABASE", "FALKORDB_HOST"):
        monkeypatch.delenv(name, raising=False)
    if runtime_path is None:
        monkeypatch.delenv("CGC_RUNTIME_DB_PATH", raising=False)
    else:
        monkeypatch.setenv("CGC_RUNTIME_DB_PATH", runtime_path)

    monkeypatch.setattr(core, "_is_falkordb_remote_configured", lambda: False)
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: False)
    monkeypatch.setattr(core, "_is_kuzudb_available", lambda: False)
    monkeypatch.setattr(core, "is_falkordb_usable", lambda: True)
    monkeypatch.setattr(core, "_is_neo4j_configured", lambda: False)
    monkeypatch.setattr(core, "_is_nornic_configured", lambda: False)

    selected_paths = []
    class FakeFalkorDBManager:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def get_driver(self):
            return object()

    fake = type(sys)("codegraphcontext.core.database_falkordb")
    fake.FalkorDBManager = lambda db_path=None: (
        selected_paths.append(db_path) or FakeFalkorDBManager(db_path)
    )
    fake.FalkorDBUnavailableError = type("FalkorDBUnavailableError", (Exception,), {})
    monkeypatch.setitem(sys.modules, fake.__name__, fake)

    manager = core.get_database_manager(db_path=db_path)

    assert manager.db_path == expected_path
    assert selected_paths == [expected_path]


def test_nonfunctional_falkordb_to_kuzu_fallback_preserves_runtime_path(
    monkeypatch, fallback_log
):
    runtime_db_path = "/runtime/db/neo4j"
    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "falkordb")
    monkeypatch.setenv("CGC_RUNTIME_DB_PATH", runtime_db_path)
    monkeypatch.setattr(core, "is_falkordb_usable", lambda: True)
    _only_backend(monkeypatch, "kuzudb")

    error_type = type("FalkorDBUnavailableError", (Exception,), {})

    class FakeFalkorDBManager:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def get_driver(self):
            raise error_type("FalkorDB is unavailable")

    fake_falkordb = type(sys)("codegraphcontext.core.database_falkordb")
    fake_falkordb.FalkorDBManager = FakeFalkorDBManager
    fake_falkordb.FalkorDBUnavailableError = error_type
    monkeypatch.setitem(sys.modules, "codegraphcontext.core.database_falkordb", fake_falkordb)

    selected_paths = []
    sentinel = object()
    fake_kuzu = type(sys)("codegraphcontext.core.database_kuzu")
    fake_kuzu.KuzuDBManager = lambda db_path=None: selected_paths.append(db_path) or sentinel
    monkeypatch.setitem(sys.modules, "codegraphcontext.core.database_kuzu", fake_kuzu)

    assert core.get_database_manager(db_path=runtime_db_path) is sentinel
    assert selected_paths == [runtime_db_path]
    assert any("FalkorDB Lite was requested but is not functional" in m for m in fallback_log)


def test_ladybug_fallback_remaps_existing_kuzudb_db_file():
    assert core._fallback_db_path_for(
        "/profile/global/db/kuzudb.db", "ladybugdb"
    ) == "/profile/global/db/ladybugdb"


@pytest.mark.parametrize("failure_mode", ("unavailable", "nonfunctional"))
def test_explicit_falkordb_fallback_preserves_kuzu_precedence(
    monkeypatch, fallback_log, failure_mode
):
    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "falkordb")
    monkeypatch.delenv("CGC_RUNTIME_DB_PATH", raising=False)
    monkeypatch.delenv("FALKORDB_HOST", raising=False)
    monkeypatch.setattr(core, "_is_falkordb_remote_configured", lambda: False)
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: True)
    monkeypatch.setattr(core, "_is_kuzudb_available", lambda: True)
    monkeypatch.setattr(core, "_is_neo4j_configured", lambda: False)
    monkeypatch.setattr(core, "_is_nornic_configured", lambda: False)

    selected = []
    ladybug_sentinel = object()
    kuzu_sentinel = object()
    ladybug = type(sys)("codegraphcontext.core.database_ladybug")
    ladybug.LadybugDBManager = lambda db_path=None: selected.append("ladybugdb") or ladybug_sentinel
    monkeypatch.setitem(sys.modules, ladybug.__name__, ladybug)
    kuzu = type(sys)("codegraphcontext.core.database_kuzu")
    kuzu.KuzuDBManager = lambda db_path=None: selected.append("kuzudb") or kuzu_sentinel
    monkeypatch.setitem(sys.modules, kuzu.__name__, kuzu)

    if failure_mode == "unavailable":
        monkeypatch.setattr(core, "is_falkordb_usable", lambda: False)
    else:
        monkeypatch.setattr(core, "is_falkordb_usable", lambda: True)
        error_type = type("FalkorDBUnavailableError", (Exception,), {})

        class NonfunctionalFalkorDBManager:
            def __init__(self, db_path=None):
                self.db_path = db_path

            def get_driver(self):
                raise error_type("startup probe failed")

        falkor = type(sys)("codegraphcontext.core.database_falkordb")
        falkor.FalkorDBManager = NonfunctionalFalkorDBManager
        falkor.FalkorDBUnavailableError = error_type
        monkeypatch.setitem(sys.modules, falkor.__name__, falkor)

    # An explicit FalkorDB selection can fall back when its startup probe fails,
    # but keep the pre-existing Kùzu fallback ahead of LadybugDB. The parser
    # golden path depends on that behavior when FalkorDB Lite cannot start.
    assert core.get_database_manager() is kuzu_sentinel
    assert selected == ["kuzudb"]


def test_context_fallback_remaps_path_when_runtime_override_is_from_another_context(
    monkeypatch, fallback_log
):
    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "falkordb")
    monkeypatch.setenv("CGC_RUNTIME_DB_PATH", "/runtime/db/neo4j")
    monkeypatch.setattr(core, "is_falkordb_usable", lambda: False)
    _only_backend(monkeypatch, "kuzudb")

    selected_paths = []
    sentinel = object()
    fake = type(sys)("codegraphcontext.core.database_kuzu")
    fake.KuzuDBManager = lambda db_path=None: selected_paths.append(db_path) or sentinel
    monkeypatch.setitem(sys.modules, "codegraphcontext.core.database_kuzu", fake)

    assert core.get_database_manager(db_path="/contexts/repo/db/falkordb") is sentinel
    assert selected_paths == ["/contexts/repo/db/kuzudb"]


def test_kuzu_missing_fallback_names_the_replacement(monkeypatch, fallback_log):
    """Explicit kuzudb with Kùzu absent must still say what it fell back to."""
    runtime_db_path = "/runtime/db/neo4j"
    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "kuzudb")
    monkeypatch.setenv("CGC_RUNTIME_DB_PATH", runtime_db_path)
    _only_backend(monkeypatch, "ladybugdb")

    sentinel = object()
    selected_paths = []
    fake = type(sys)("codegraphcontext.core.database_ladybug")
    fake.LadybugDBManager = lambda db_path=None: selected_paths.append(db_path) or sentinel
    monkeypatch.setitem(sys.modules, "codegraphcontext.core.database_ladybug", fake)

    assert core.get_database_manager(db_path=runtime_db_path) is sentinel
    assert selected_paths == [runtime_db_path]
    assert any("LadybugDB" in m for m in fallback_log), fallback_log
    assert any("codegraphcontext-fork[kuzu]" in m for m in fallback_log), fallback_log


@pytest.mark.parametrize("backend", ("kuzudb", "ladybugdb"))
def test_neo4j_context_fallback_uses_backend_path_and_runtime_override(
    monkeypatch, tmp_path, fallback_log, backend
):
    from codegraphcontext.cli.cli_helpers import _embedded_fallback_manager

    context_db_path = tmp_path / "contexts" / "example" / "db" / "neo4j"
    runtime_db_path = tmp_path / "runtime" / "db" / "neo4j"
    selected_paths = []
    manager = object()

    monkeypatch.setattr(core, "_is_kuzudb_available", lambda: backend == "kuzudb")
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: backend == "ladybugdb")
    module_name = f"codegraphcontext.core.database_{backend.removesuffix('db')}"
    manager_name = "KuzuDBManager" if backend == "kuzudb" else "LadybugDBManager"
    fake_backend = type(sys)(module_name)
    setattr(fake_backend, manager_name, lambda db_path=None: selected_paths.append(db_path) or manager)
    monkeypatch.setitem(sys.modules, module_name, fake_backend)

    context = SimpleNamespace(db_path=str(context_db_path))
    monkeypatch.delenv("CGC_RUNTIME_DB_PATH", raising=False)
    assert _embedded_fallback_manager(context, "Neo4j failed") is manager
    assert selected_paths == [str(context_db_path.parent / backend)]

    monkeypatch.setenv("CGC_RUNTIME_DB_PATH", str(runtime_db_path))
    assert _embedded_fallback_manager(context, "Neo4j failed") is manager
    assert selected_paths[-1] == str(runtime_db_path)


def test_neo4j_context_fallback_prefers_ladybug_when_both_engines_are_available(
    monkeypatch, tmp_path, fallback_log
):
    from codegraphcontext.cli.cli_helpers import _embedded_fallback_manager

    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("CGC_RUNTIME_DB_PATH", raising=False)
    monkeypatch.setattr(core, "_is_kuzudb_available", lambda: True)
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: True)
    selected = []
    ladybug_sentinel = object()
    kuzu = type(sys)("codegraphcontext.core.database_kuzu")
    kuzu.KuzuDBManager = lambda db_path=None: selected.append("kuzudb") or object()
    monkeypatch.setitem(sys.modules, kuzu.__name__, kuzu)
    ladybug = type(sys)("codegraphcontext.core.database_ladybug")
    ladybug.LadybugDBManager = lambda db_path=None: selected.append("ladybugdb") or ladybug_sentinel
    monkeypatch.setitem(sys.modules, ladybug.__name__, ladybug)

    context = SimpleNamespace(db_path=str(tmp_path / "example" / "db" / "neo4j"))

    assert _embedded_fallback_manager(context, "Neo4j failed") is ladybug_sentinel
    assert selected == ["ladybugdb"]


def test_explicit_falkordb_startup_fallback_prefers_kuzu_when_both_engines_are_available(
    monkeypatch, tmp_path, fallback_log
):
    from codegraphcontext.cli.cli_helpers import _embedded_fallback_manager

    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "falkordb")
    monkeypatch.delenv("CGC_RUNTIME_DB_PATH", raising=False)
    monkeypatch.setattr(core, "_is_kuzudb_available", lambda: True)
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: True)

    selected = []
    kuzu_sentinel = object()
    kuzu = type(sys)("codegraphcontext.core.database_kuzu")
    kuzu.KuzuDBManager = lambda db_path=None: selected.append("kuzudb") or kuzu_sentinel
    monkeypatch.setitem(sys.modules, kuzu.__name__, kuzu)
    ladybug = type(sys)("codegraphcontext.core.database_ladybug")
    ladybug.LadybugDBManager = lambda db_path=None: selected.append("ladybugdb") or object()
    monkeypatch.setitem(sys.modules, ladybug.__name__, ladybug)

    context = SimpleNamespace(
        db_path=str(tmp_path / "contexts" / "example" / "db" / "falkordb"),
        database="falkordb",
    )

    assert _embedded_fallback_manager(context, "FalkorDB startup failed") is kuzu_sentinel
    assert selected == ["kuzudb"]


def test_falkordb_gateway_candidates_preserve_kuzu_fallback_order():
    from codegraphcontext.cli.cli_helpers import _embedded_gateway_candidates

    candidates = _embedded_gateway_candidates("/runtime/db/falkordb", "falkordb")

    assert candidates == [
        ("/runtime/db/falkordb", "falkordb"),
        ("/runtime/db/kuzudb", "kuzudb"),
        ("/runtime/db/ladybugdb", "ladybugdb"),
    ]


def test_no_backend_available_still_raises(monkeypatch, fallback_log):
    """Funnelling through the helper must not swallow the 'nothing installed' error."""
    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "kuzudb")
    _only_backend(monkeypatch, "none-of-them")

    with pytest.raises(ValueError, match="Kùzu is not installed") as error:
        core.get_database_manager()
    assert "codegraphcontext-fork[kuzu]" in str(error.value)


@pytest.mark.parametrize("platform", ("darwin", "win32"))
def test_missing_kuzu_on_python314_non_linux_explains_extra_limit(
    monkeypatch, fallback_log, platform
):
    """The Kùzu install hint must account for the extra's Python/platform marker."""
    monkeypatch.setenv("CGC_RUNTIME_DB_TYPE", "kuzudb")
    monkeypatch.setattr(sys, "version_info", (3, 14, 0))
    monkeypatch.setattr(sys, "platform", platform)
    _only_backend(monkeypatch, "none-of-them")

    with pytest.raises(ValueError) as error:
        core.get_database_manager()

    message = str(error.value)
    assert "Python 3.14" in message
    assert "non-Linux" in message
    assert "LadybugDB" in message
    assert "pip install" not in message

    _only_backend(monkeypatch, "ladybugdb")
    ladybug = type(sys)("codegraphcontext.core.database_ladybug")
    sentinel = object()
    ladybug.LadybugDBManager = lambda db_path=None: sentinel
    monkeypatch.setitem(sys.modules, ladybug.__name__, ladybug)

    assert core.get_database_manager() is sentinel
    fallback_message = " ".join(fallback_log)
    assert "Python 3.14" in fallback_message
    assert "non-Linux" in fallback_message
    assert "pip install" not in fallback_message


def test_default_backend_uses_ladybug_without_optional_embedded_drivers(monkeypatch):
    """A fresh lean install still has a working embedded backend."""
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "DEFAULT_DATABASE",
        "FALKORDB_HOST",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NORNIC_URI",
        "NORNIC_USERNAME",
        "NORNIC_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(core, "is_falkordb_usable", lambda: False)
    monkeypatch.setattr(core, "_is_kuzudb_available", lambda: False)
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: True)

    sentinel = object()
    fake = type(sys)("codegraphcontext.core.database_ladybug")
    fake.LadybugDBManager = lambda db_path=None: sentinel
    monkeypatch.setitem(sys.modules, "codegraphcontext.core.database_ladybug", fake)

    assert core.get_database_manager() is sentinel


def test_explicit_neo4j_without_driver_explains_extra(monkeypatch):
    monkeypatch.setenv("DEFAULT_DATABASE", "neo4j")
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "test")

    find_spec = core.importlib.util.find_spec
    monkeypatch.setattr(
        core.importlib.util,
        "find_spec",
        lambda name, *args, **kwargs: None if name == "neo4j" else find_spec(name, *args, **kwargs),
    )

    with pytest.raises(ValueError, match=r"codegraphcontext-fork\[neo4j\]"):
        core.get_database_manager()
