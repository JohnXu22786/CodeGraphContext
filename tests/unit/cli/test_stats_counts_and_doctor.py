"""Regression tests for stats counting, doctor honesty and report scoping."""
import inspect
import os
import re
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

import pytest

from codegraphcontext.cli import cli_helpers, config_manager, main as cli_main
from codegraphcontext.cli.main import app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    """Strip ANSI escapes and collapse whitespace.

    Rich emits colour codes (and soft-wraps) whenever it thinks the terminal
    supports them — which it does in CI but not always locally. Asserting on
    raw `result.output` therefore passes on a dev machine and fails in CI with
    the expected text plainly visible, just interleaved with \x1b[1;36m codes.
    """
    return " ".join(_ANSI_RE.sub("", text).split())



def _stats_source():
    return inspect.getsource(cli_helpers.stats_helper)


def test_stats_counts_are_distinct():
    """`count(x)` over a variable-length CONTAINS* match counts rows, not
    nodes: a method is reachable both as File->Function and
    File->Class->Function, so functions were over-reported by ~55%."""
    source = _stats_source()
    contains_counts = re.findall(r"CONTAINS\*\]->\((\w+):(\w+)\) RETURN count\((DISTINCT )?", source)
    assert contains_counts, "expected the CONTAINS* count queries to be present"
    for var, label, distinct in contains_counts:
        assert distinct, f"count over CONTAINS* for {label} must use DISTINCT"


def test_doctor_probes_the_connection_for_the_default_backend():
    """Section 2 is titled 'Checking Database Connection', but for the default
    falkordb backend it was only an import probe."""
    source = inspect.getsource(cli_main.doctor)
    assert "is_falkordb_usable" in source
    # It must now actually open a connection, like the neo4j / remote branches.
    assert "get_database_manager" in source
    assert "RETURN 1" in source
    assert "LadybugDB connection successful" in source


def test_troubleshooting_lists_ladybugdb_in_doctor_driver_checks():
    troubleshooting = (
        Path(__file__).resolve().parents[3]
        / "docs"
        / "docs"
        / "reference"
        / "troubleshooting.md"
    ).read_text(encoding="utf-8")

    diagnostics = troubleshooting.split("**Database Driver Availability**", 1)[1].split(
        "\n4.", 1
    )[0]
    assert "LadybugDB" in diagnostics


def test_doctor_probes_the_resolved_context_database_path(tmp_path, monkeypatch):
    import codegraphcontext.core as core

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    context_db = tmp_path / "named-context" / "db" / "ladybugdb"
    selected_paths = []
    context_resolution_options = []

    class ProbeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, query):
            assert query == "RETURN 1"

    class ProbeManager:
        def get_driver(self):
            return SimpleNamespace(session=lambda: ProbeSession())

        def close_driver(self):
            pass

    monkeypatch.setattr(cli_main, "_load_credentials", lambda: None)
    monkeypatch.setattr(config_manager, "ensure_first_run_bootstrap", lambda: None)
    monkeypatch.setattr(config_manager, "ensure_config_file", lambda: None)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", config_dir / ".env")
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", config_dir / "config.yaml")
    monkeypatch.setattr(config_manager, "load_config", lambda: {"DEFAULT_DATABASE": "ladybugdb"})
    monkeypatch.setattr(config_manager, "validate_config_value", lambda *_args: (True, ""))
    def resolve_context(**kwargs):
        context_resolution_options.append(kwargs)
        return SimpleNamespace(db_path=str(context_db))

    monkeypatch.setattr(config_manager, "resolve_context", resolve_context)
    monkeypatch.setattr(core, "ladybugdb_unavailable_reason", lambda: None)
    monkeypatch.setattr(
        core,
        "get_database_manager",
        lambda db_path=None: selected_paths.append(db_path) or ProbeManager(),
    )
    monkeypatch.setenv("CGC_SELECTED_DATABASE", "ladybugdb")
    monkeypatch.delenv("CGC_RUNTIME_DB_PATH", raising=False)

    cli_main.doctor()

    assert selected_paths == [str(context_db)]
    assert context_resolution_options == [
        {"cwd": Path.cwd(), "create_local_context": False, "read_only": True}
    ]


def test_load_credentials_uses_resolved_global_backend_for_doctor(
    tmp_path, monkeypatch
):
    import codegraphcontext.core as core
    from codegraphcontext.cli.config_manager import ResolvedContext

    profile = tmp_path / "profile"
    profile.mkdir()
    global_db_path = profile / "global" / "db" / "falkordb"
    global_db_path.mkdir(parents=True)
    resolved = ResolvedContext(
        mode="global",
        context_name="",
        database="falkordb",
        db_path=str(global_db_path),
        cgcignore_path=str(profile / "global" / ".cgcignore"),
    )

    monkeypatch.setattr(config_manager, "CONFIG_DIR", profile)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", profile / ".env")
    monkeypatch.setattr(
        config_manager, "CONTEXT_CONFIG_FILE", profile / "config.yaml"
    )
    monkeypatch.setattr(config_manager, "ensure_config_dir", lambda: None)
    monkeypatch.setattr(
        config_manager, "should_apply_project_dotenv", lambda **_kwargs: False
    )
    monkeypatch.setattr(config_manager, "resolve_context", lambda *_a, **_k: resolved)
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "CGC_RUNTIME_DB_PATH",
        "DEFAULT_DATABASE",
        "DATABASE_TYPE",
        "CGC_SELECTED_DATABASE",
        "CGC_DB_SELECTION_SOURCE",
    ):
        monkeypatch.setenv(name, "__test_value_to_remove__")
        monkeypatch.delenv(name, raising=False)

    class DefaultFactoryManager:
        def get_backend_type(self):
            return "ladybugdb"

    monkeypatch.setattr(
        core, "get_database_manager", lambda *_args, **_kwargs: DefaultFactoryManager()
    )

    cli_main._load_credentials()

    assert os.environ["CGC_SELECTED_DATABASE"] == "falkordb"
    assert os.environ["DEFAULT_DATABASE"] == "falkordb"


def test_doctor_reports_neo4j_extra_for_nornic_without_driver(monkeypatch):
    import codegraphcontext.core as core

    monkeypatch.setattr(cli_main, "_load_credentials", lambda: None)
    monkeypatch.setattr(config_manager, "ensure_first_run_bootstrap", lambda: None)
    monkeypatch.setattr(config_manager, "ensure_config_file", lambda: None)
    monkeypatch.setattr(config_manager, "load_config", lambda: {"DEFAULT_DATABASE": "nornic"})
    monkeypatch.setattr(config_manager, "validate_config_value", lambda *_args: (True, ""))
    monkeypatch.setattr(core, "_is_neo4j_available", lambda: False)
    monkeypatch.setenv("CGC_SELECTED_DATABASE", "nornic")

    result = runner.invoke(app, ["doctor"])

    assert "Nornic support requires the Neo4j driver" in result.output
    assert "codegraphcontext-fork[neo4j]" in result.output


def test_neo4j_setup_names_optional_extra_when_driver_is_missing(monkeypatch):
    import codegraphcontext.core as core

    monkeypatch.setattr(core, "_is_neo4j_available", lambda: False)
    monkeypatch.setattr(
        cli_main,
        "run_neo4j_setup_wizard",
        lambda: (_ for _ in ()).throw(AssertionError("wizard should not run")),
    )

    result = runner.invoke(app, ["neo4j", "setup"])

    assert result.exit_code == 1
    assert "codegraphcontext-fork[neo4j]" in result.output


def test_doctor_does_not_claim_health_when_warnings_were_printed():
    source = inspect.getsource(cli_main.doctor)
    assert "warnings_found = False" in source
    assert "if all_checks_passed and not warnings_found:" in source
    # Every ⚠ line in doctor should set the flag.
    warn_lines = [
        line for line in source.splitlines()
        if "[yellow]⚠[/yellow]" in line and "console.print" in line
    ]
    assert len(warn_lines) >= 4
    assert source.count("warnings_found = True") >= len(warn_lines)


def test_report_accepts_a_repo_flag():
    """`cgc report` had no way to target a repo; with several indexed it
    silently picked the one with the most files."""
    result = runner.invoke(app, ["report", "--help"])
    assert result.exit_code == 0
    normalised = _plain(result.output)
    assert "--repo" in normalised

    source = inspect.getsource(cli_main.report)
    assert "repo_path=scoped_repo" in source

@pytest.mark.parametrize(
    ("platform", "version", "unsupported"),
    (
        ("darwin", (3, 14, 0), True),
        ("win32", (3, 14, 0), True),
        ("linux", (3, 14, 0), False),
        ("darwin", (3, 13, 0), False),
    ),
)
def test_doctor_kuzu_install_guidance_matches_platform_marker(
    monkeypatch, platform, version, unsupported
):
    monkeypatch.setattr(cli_main.sys, "platform", platform)
    monkeypatch.setattr(cli_main.sys, "version_info", version)
    monkeypatch.setattr(config_manager, "load_config", lambda: {"DEFAULT_DATABASE": "kuzudb"})
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: None)
    monkeypatch.setattr(config_manager, "ensure_first_run_bootstrap", lambda: None)
    monkeypatch.setattr(config_manager, "ensure_config_file", lambda: None)
    monkeypatch.setattr(cli_main, "_load_credentials", lambda: None)
    monkeypatch.setenv("CGC_SELECTED_DATABASE", "kuzudb")
    result = runner.invoke(app, ["doctor"])
    message = _plain(result.output)
    assert "KuzuDB is not installed" in message
    if unsupported:
        assert "does not install Kùzu" in message
        assert "Python <3.14 or Linux" in message
    else:
        assert "pip install" in message
        assert "codegraphcontext-fork[kuzu]" in message
