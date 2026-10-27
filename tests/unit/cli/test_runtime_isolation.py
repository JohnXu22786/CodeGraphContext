"""Regression tests for running isolated CodeGraphContext installations side by side."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codegraphcontext import core
from codegraphcontext.cli import config_manager, main as cli_main, setup_wizard
from codegraphcontext.runtime_paths import (
    bootstrap_instance_environment,
    get_cgc_home,
    get_isolation_environment,
    get_instance_mcp_config,
    get_instance_name,
    get_repo_context_dir_name,
    get_repo_context_path,
)


def test_cgc_home_routes_user_state_away_from_legacy_root(tmp_path, monkeypatch):
    legacy_home = tmp_path / "legacy-home"
    isolated_home = tmp_path / "fork-state"
    monkeypatch.setenv("HOME", str(legacy_home))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(legacy_home))
    monkeypatch.setenv("CGC_HOME", str(isolated_home))

    assert get_cgc_home() == isolated_home.resolve()
    monkeypatch.delenv("CGC_HOME", raising=False)
    assert get_cgc_home() == (legacy_home / ".codegraphcontext").resolve()

    # A fresh process exercises import-time defaults exactly as the CLI does.
    repo_root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env.update(
        HOME=str(legacy_home),
        CGC_HOME=str(isolated_home),
        PYTHONPATH=str(repo_root / "src"),
    )
    for key in (
        "CGC_RUNTIME_DB_PATH",
        "CGC_RUNTIME_DB_TYPE",
        "CGC_REPO_CONTEXT_DIR",
        "CGC_INSTANCE_NAME",
        "FALKORDB_PATH",
        "FALKORDB_SOCKET_PATH",
        "KUZUDB_PATH",
        "LADYBUGDB_PATH",
    ):
        env.pop(key, None)
    if os.name == "nt":
        env["USERPROFILE"] = str(legacy_home)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from codegraphcontext.cli import config_manager as cm; "
                "from codegraphcontext.core.cgc_bundle import _default_bundle_install_root; "
                "print(json.dumps({'config': str(cm.CONFIG_DIR), "
                "'config_file': str(cm.CONFIG_FILE), "
                "'falkordb': cm.DEFAULT_CONFIG['FALKORDB_PATH'], "
                "'falkordb_socket': cm.DEFAULT_CONFIG['FALKORDB_SOCKET_PATH'], "
                "'kuzu': cm.DEFAULT_CONFIG['KUZUDB_PATH'], "
                "'ladybug': cm.DEFAULT_CONFIG['LADYBUGDB_PATH'], "
                "'log': cm.DEFAULT_CONFIG['LOG_FILE_PATH'], "
                "'debug_log': cm.DEFAULT_CONFIG['DEBUG_LOG_PATH'], "
                "'global_kuzu': cm._default_global_db_path('kuzudb'), "
                "'bundle': _default_bundle_install_root({'repo': 'example/project'})}))"
            ),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    paths = json.loads(result.stdout)
    isolated_root = isolated_home.resolve()
    legacy_root = (legacy_home / ".codegraphcontext").resolve()
    for key in (
        "config",
        "config_file",
        "falkordb",
        "falkordb_socket",
        "kuzu",
        "ladybug",
        "log",
        "debug_log",
        "global_kuzu",
        "bundle",
    ):
        assert Path(paths[key]).is_relative_to(isolated_root), key
        assert not Path(paths[key]).is_relative_to(legacy_root), key


def test_mcp_entry_root_is_applied_before_config_manager_import(tmp_path):
    repo_root = Path(__file__).resolve().parents[3]
    mcp_config = {
        "mcpServers": {
            "CodeGraphContext Fork": {
                "env": {
                    "CGC_HOME": "fork-home",
                    "CGC_REPO_CONTEXT_DIR": ".codegraphcontext-fork",
                    "CGC_IGNORE_PROJECT_ENV": "1",
                }
            }
        }
    }
    (tmp_path / "mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")
    env = os.environ.copy()
    env.update(
        HOME=str(tmp_path / "legacy-home"),
        CGC_INSTANCE_NAME="CodeGraphContext Fork",
        PYTHONPATH=str(repo_root / "src"),
    )
    for key in (
        "CGC_HOME",
        "CGC_RUNTIME_DB_PATH",
        "CGC_RUNTIME_DB_TYPE",
        "CGC_REPO_CONTEXT_DIR",
        "CGC_IGNORE_PROJECT_ENV",
        "CGC_LOAD_PROJECT_ENV",
    ):
        env.pop(key, None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from codegraphcontext.runtime_paths import bootstrap_instance_environment; "
                "bootstrap_instance_environment(Path.cwd() / 'mcp.json'); "
                "from codegraphcontext.cli import config_manager as cm; "
                "import json, os; "
                "print(json.dumps({'root': str(cm.CONFIG_DIR), 'db': cm.DEFAULT_CONFIG['KUZUDB_PATH'], "
                "'repo_context': os.environ['CGC_REPO_CONTEXT_DIR'], "
                "'ignore_project_env': os.environ['CGC_IGNORE_PROJECT_ENV']}))"
            ),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    paths = json.loads(result.stdout)
    assert paths["root"] == str((tmp_path / "fork-home").resolve())
    assert Path(paths["db"]).is_relative_to((tmp_path / "fork-home").resolve())
    assert paths["repo_context"] == ".codegraphcontext-fork"
    assert paths["ignore_project_env"] == "1"


@pytest.mark.parametrize("content", (b"{ invalid", b"\xff"))
def test_invalid_mcp_config_does_not_block_runtime_bootstrap(tmp_path, content, monkeypatch):
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_bytes(content)
    monkeypatch.delenv("CGC_HOME", raising=False)

    bootstrap_instance_environment(mcp_config)

    assert "CGC_HOME" not in os.environ


def test_project_context_override_ignores_upstream_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CGC_HOME", str(tmp_path / "fork-home"))
    monkeypatch.setenv("CGC_REPO_CONTEXT_DIR", ".codegraphcontext-fork")
    repo = tmp_path / "repo"
    upstream_context = repo / ".codegraphcontext"
    upstream_context.mkdir(parents=True)
    (upstream_context / "config.yaml").write_text("database: neo4j\n", encoding="utf-8")

    assert get_repo_context_dir_name() == ".codegraphcontext-fork"
    assert get_repo_context_path(repo) == repo / ".codegraphcontext-fork"
    assert config_manager.find_local_cgc_dir(repo) is None
    for invalid_name in ("../outside", "nested/context", r"nested\context", "C:outside"):
        monkeypatch.setenv("CGC_REPO_CONTEXT_DIR", invalid_name)
        with pytest.raises(ValueError, match="single directory name"):
            get_repo_context_dir_name()
    monkeypatch.setenv("CGC_REPO_CONTEXT_DIR", ".codegraphcontext-fork")

    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path / "fork-home")
    monkeypatch.setattr(config_manager, "CONFIG_FILE", tmp_path / "fork-home" / ".env")
    monkeypatch.setattr(
        config_manager,
        "load_context_config",
        lambda: config_manager.ContextConfig(mode="per-repo"),
    )
    monkeypatch.setattr(
        config_manager, "load_config", lambda **_kwargs: {"DEFAULT_DATABASE": "falkordb"}
    )

    resolved = config_manager.resolve_context(cwd=repo)
    fork_context = repo / ".codegraphcontext-fork"
    assert resolved.is_local
    assert resolved.db_path == str(fork_context / "db" / "falkordb")
    assert (fork_context / "config.yaml").exists()

    contexts = config_manager.discover_child_contexts(tmp_path)
    assert [Path(context.cgc_path) for context in contexts] == [fork_context]


def _read_only_database_preview(tmp_path, monkeypatch, *, remote_host=None):
    output = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", tmp_path / "global.env")
    monkeypatch.setattr(
        config_manager, "should_apply_project_dotenv", lambda **_kwargs: False
    )
    monkeypatch.setattr(
        config_manager,
        "resolve_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            mode="global", context_name="", database="ladybugdb"
        ),
    )
    monkeypatch.setattr(cli_main, "console", SimpleNamespace(print=output.append))
    monkeypatch.setattr(
        core,
        "get_database_manager",
        lambda **_kwargs: pytest.fail("read-only preview must not construct a database manager"),
    )
    for name in (
        "CGC_RUNTIME_DB_TYPE",
        "CGC_RUNTIME_DB_PATH",
        "DEFAULT_DATABASE",
        "DATABASE_TYPE",
        "CGC_SELECTED_DATABASE",
        "CGC_DB_SELECTION_SOURCE",
        "FALKORDB_HOST",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NORNIC_URI",
        "NORNIC_USERNAME",
        "NORNIC_PASSWORD",
    ):
        monkeypatch.setenv(name, "__test_value_to_remove__")
        monkeypatch.delenv(name, raising=False)
    if remote_host:
        monkeypatch.setenv("FALKORDB_HOST", remote_host)
    cli_main._load_credentials(read_only=True)
    return output


def test_read_only_backend_preview_selects_remote_falkordb(tmp_path, monkeypatch):
    output = _read_only_database_preview(
        tmp_path, monkeypatch, remote_host="graph.example"
    )

    assert os.environ["CGC_SELECTED_DATABASE"] == "falkordb-remote"
    assert any("falkordb-remote" in line for line in output)


def test_read_only_backend_preview_discloses_falkordb_startup_fallback(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(core, "_is_ladybugdb_available", lambda: False)
    monkeypatch.setattr(
        core.importlib.util, "find_spec", lambda _name: None
    )
    monkeypatch.setattr(core, "is_falkordb_usable", lambda: True)
    monkeypatch.setattr(core, "_is_kuzudb_available", lambda: True)

    output = _read_only_database_preview(tmp_path, monkeypatch)

    assert os.environ["CGC_SELECTED_DATABASE"] == "falkordb"
    assert any("kuzudb" in line and "startup" in line.lower() for line in output)


def test_mcp_registration_carries_isolation_and_uses_current_interpreter(tmp_path, monkeypatch):
    isolated_home = tmp_path / "fork-home"
    monkeypatch.setenv("HOME", str(tmp_path / "legacy-home"))
    monkeypatch.setenv("CGC_HOME", "fork-home")
    monkeypatch.setenv("CGC_RUNTIME_DB_PATH", "fork-db/graph")
    monkeypatch.setenv("CGC_REPO_CONTEXT_DIR", ".codegraphcontext-fork")
    monkeypatch.setenv("CGC_INSTANCE_NAME", "CodeGraphContext Fork")
    monkeypatch.setenv("CGC_IGNORE_PROJECT_ENV", "1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", isolated_home / ".env")
    monkeypatch.setattr(config_manager, "load_config", lambda **_kwargs: {})
    monkeypatch.setattr(setup_wizard, "_configure_ide", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        setup_wizard.shutil,
        "which",
        lambda name: "/usr/bin/cgc" if name == "cgc" else None,
    )
    mcp_file = tmp_path / "mcp.json"
    mcp_file.write_text(
        json.dumps(
            {
                "mcpServers": {"CodeGraphContext": {"command": "/upstream/cgc"}},
                "userSetting": "preserve-me",
            }
        ),
        encoding="utf-8",
    )

    setup_wizard.configure_mcp_client()

    registration = json.loads(mcp_file.read_text(encoding="utf-8"))
    assert registration["mcpServers"]["CodeGraphContext"]["command"] == "/upstream/cgc"
    assert registration["userSetting"] == "preserve-me"
    server = registration["mcpServers"]["CodeGraphContext Fork"]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "codegraphcontext", "mcp", "start"]
    assert server["env"]["CGC_HOME"] == str(isolated_home)
    assert server["env"]["CGC_RUNTIME_DB_PATH"] == str((tmp_path / "fork-db" / "graph").resolve())
    assert server["env"]["CGC_REPO_CONTEXT_DIR"] == ".codegraphcontext-fork"
    assert server["env"]["CGC_INSTANCE_NAME"] == "CodeGraphContext Fork"
    assert server["env"]["CGC_IGNORE_PROJECT_ENV"] == "1"
    assert get_instance_name() == "CodeGraphContext Fork"

    monkeypatch.setattr(setup_wizard, "_save_neo4j_credentials", lambda _creds: None)
    setup_wizard._generate_mcp_json({"uri": "bolt://localhost:7687"})
    registration = json.loads(mcp_file.read_text(encoding="utf-8"))
    assert registration["mcpServers"]["CodeGraphContext"]["command"] == "/upstream/cgc"
    assert registration["mcpServers"]["CodeGraphContext Fork"]["env"]["CGC_HOME"] == str(isolated_home)
    assert registration["userSetting"] == "preserve-me"
    server = registration["mcpServers"]["CodeGraphContext Fork"]

    server["tools"]["disabledTools"] = ["find_code"]
    assert get_instance_mcp_config(registration)["tools"]["disabledTools"] == ["find_code"]
    assert get_instance_mcp_config({"mcpServers": {"CodeGraphContext": {}}}) == {}
    assert get_instance_mcp_config({"mcpServers": []}) == {}

    invalid_settings = "{ existing client config"
    mcp_file.write_text(invalid_settings, encoding="utf-8")
    setup_wizard.configure_mcp_client()
    assert mcp_file.read_text(encoding="utf-8") == invalid_settings

    invalid_utf8_settings = b"\xff"
    mcp_file.write_bytes(invalid_utf8_settings)
    setup_wizard.configure_mcp_client()
    assert mcp_file.read_bytes() == invalid_utf8_settings


@pytest.mark.parametrize("name", ("CGC_IGNORE_PROJECT_ENV", "CGC_LOAD_PROJECT_ENV"))
def test_project_env_overrides_are_forwarded_to_child(name, monkeypatch):
    monkeypatch.setenv(name, "1")

    assert get_isolation_environment()[name] == "1"
