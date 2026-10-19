from io import StringIO
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import typer
from rich.console import Console

from codegraphcontext.cli import cli_helpers
from codegraphcontext.core.database_errors import Neo4jConnectionError


def _neo4j_initialization_failure(monkeypatch, tmp_path, *, allow_fallback):
    manager = MagicMock()
    manager.get_driver.side_effect = Neo4jConnectionError(
        "Neo4j is not running", source="test"
    )
    ctx = SimpleNamespace(
        mode="global",
        database="neo4j",
        db_path=str(tmp_path / "db" / "neo4j"),
    )
    output = StringIO()
    monkeypatch.setenv("DEFAULT_DATABASE", "neo4j")
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DATABASE_TYPE", raising=False)
    monkeypatch.delenv("CGC_RUNTIME_DB_PATH", raising=False)
    if allow_fallback:
        monkeypatch.setenv("CGC_ALLOW_NEO4J_FALLBACK", "true")
    else:
        monkeypatch.delenv("CGC_ALLOW_NEO4J_FALLBACK", raising=False)

    with (
        patch.object(cli_helpers, "ensure_first_run_bootstrap"),
        patch.object(cli_helpers, "resolve_context", return_value=ctx),
        patch.object(cli_helpers, "get_database_manager", return_value=manager),
        patch.object(
            cli_helpers,
            "console",
            Console(file=output, force_terminal=False, width=120),
        ),
        patch.object(cli_helpers, "_embedded_fallback_manager", return_value=None),
        pytest.raises(typer.Exit) as exc_info,
    ):
        cli_helpers._initialize_services()

    assert exc_info.value.exit_code == 1
    return " ".join(output.getvalue().split())


def test_initialize_services_explains_embedded_database_lock():
    db_manager = MagicMock()
    db_manager.get_driver.side_effect = RuntimeError(
        "IO exception: Could not set lock on file : /tmp/ladybugdb "
        "(Error: Resource temporarily unavailable)"
    )
    ctx = SimpleNamespace(
        mode="global",
        database="ladybugdb",
        database_is_explicit=False,
        db_path="/tmp/ladybugdb",
    )
    output = StringIO()

    with (
        patch.object(cli_helpers, "ensure_first_run_bootstrap"),
        patch.object(cli_helpers, "resolve_context", return_value=ctx),
        patch.object(cli_helpers, "get_database_manager", return_value=db_manager),
        patch.object(
            cli_helpers,
            "console",
            Console(file=output, force_terminal=False, width=120),
        ),
        pytest.raises(typer.Exit) as exc_info,
    ):
        cli_helpers._initialize_services()

    message = " ".join(output.getvalue().split())
    assert exc_info.value.exit_code == 1
    assert "Another CGC process" in message
    assert "Gateway or watcher" in message
    assert "use its MCP/HTTP interface or stop it" in message
    assert "Please ensure your database is configured correctly" not in message


def test_neo4j_fallback_failure_explains_how_to_install_embedded_backend(
    monkeypatch, tmp_path
):
    message = _neo4j_initialization_failure(
        monkeypatch, tmp_path, allow_fallback=True
    )

    assert "No embedded database backend is available" in message
    assert "codegraphcontext-fork[kuzu]" in message
    assert "--db kuzudb" in message
    assert "--db ladybugdb" in message


def test_neo4j_failure_tip_accounts_for_optional_kuzu_extra(monkeypatch, tmp_path):
    message = _neo4j_initialization_failure(
        monkeypatch, tmp_path, allow_fallback=False
    )

    assert "codegraphcontext-fork[kuzu]" in message
    assert "--db kuzudb" in message
    assert "--db ladybugdb" in message

@pytest.mark.parametrize("platform", ("darwin", "win32"))
def test_embedded_backend_unavailable_guidance_matches_kuzu_platform_marker(
    monkeypatch, platform
):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "version_info", (3, 14, 0))
    output = StringIO()
    with patch.object(
        cli_helpers,
        "console",
        Console(file=output, force_terminal=False, width=120),
    ):
        cli_helpers._print_embedded_backend_unavailable()
    message = output.getvalue()
    assert "does not install Kùzu" in message
    assert "Python <3.14 or Linux" in message
