"""#1317: named-context repo registration must run BEFORE the skip guard.

A graph populated by a path that skipped registration (MCP tool, interrupted
run) left config.yaml permanently desynced — every later `cgc index` hit
"already indexed. Skipping." above the registration line, so `cgc context
list` showed "Repos Linked: 0" forever. Hermetic: per-test HOME, embedded db.
"""
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

CGC = f"{shlex.quote(sys.executable)} -m codegraphcontext.cli.main"


def _env(home: Path):
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["DEFAULT_DATABASE"] = "kuzudb"
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    env.pop("CGC_CONTEXT_MODE", None)
    # The Actions job may choose a runtime backend globally. This regression
    # exercises the named context's configured database and must be hermetic.
    env.pop("CGC_RUNTIME_DB_TYPE", None)
    env.pop("CGC_RUNTIME_DB_PATH", None)
    return env


def _run(cmd: str, env, cwd=None):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env, cwd=cwd)
    return r.stdout + r.stderr


def _config_path(home: Path) -> Path:
    return home / ".codegraphcontext" / "config.yaml"


def _linked_repos(home: Path, ctx_name: str):
    cfg = yaml.safe_load(_config_path(home).read_text()) or {}
    ctx = (cfg.get("contexts") or {}).get(ctx_name) or {}
    return ctx.get("repos") or []


def _mark_generation_current(repo: Path, env):
    """Model a verified populated index so the CLI reaches its skip guard."""
    script = """
from pathlib import Path
import sys
from codegraphcontext.cli.cli_helpers import _initialize_services
from codegraphcontext.cli.snapshot_commands import effective_snapshot_identity
from codegraphcontext.core.index_generation import write_generation_state
repo = Path(sys.argv[1])
services = _initialize_services("proj", cwd=repo)
db_manager, _, _, context = services
identity, _ = effective_snapshot_identity(context, repo)
write_generation_state(
    db_manager.db_path,
    identity,
    state="complete",
    generation_id=f"{identity['storage_key']}:uncommitted",
)
db_manager.close_driver()
"""
    subprocess.run(
        [sys.executable, "-c", script, str(repo)],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_reindex_of_deregistered_repo_restores_context_registration(tmp_path: Path):
    home = tmp_path / "home"; home.mkdir()
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    env = _env(home)

    out = _run(f"{CGC} context create proj --database kuzudb", env)
    assert "proj" in out, out
    out = _run(f"{CGC} config set mode named", env)
    out = _run(f"{CGC} context default proj", env)

    # First index: registers and indexes.
    out = _run(f"{CGC} index {shlex.quote(str(repo))} --context proj", env)
    assert "Successfully finished indexing" in out or "already indexed" in out, out
    assert _linked_repos(home, "proj"), "first index did not register the repo"

    # Simulate the desync: graph stays populated, config loses the repo.
    cfg_file = _config_path(home)
    cfg = yaml.safe_load(cfg_file.read_text())
    cfg["contexts"]["proj"]["repos"] = []
    cfg_file.write_text(yaml.safe_dump(cfg))
    assert _linked_repos(home, "proj") == []

    # Restore a valid completion marker for the populated shared context, so
    # this invocation exercises the already-indexed skip path exactly.
    _mark_generation_current(repo, env)
    out = _run(f"{CGC} index {shlex.quote(str(repo))} --context proj", env)
    assert "Skipping" in out or "already indexed" in out, out
    assert _linked_repos(home, "proj"), (
        "the skip guard returned before restoring named-context registration:\n" + out
    )


def test_default_named_context_registers_without_explicit_flag(tmp_path: Path):
    home = tmp_path / "home"; home.mkdir()
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    env = _env(home)

    _run(f"{CGC} context create proj --database kuzudb", env)
    _run(f"{CGC} config set mode named", env)
    _run(f"{CGC} context default proj", env)

    # No --context flag: the resolved default context must still register.
    out = _run(f"{CGC} index {shlex.quote(str(repo))}", env)
    assert _linked_repos(home, "proj"), (
        "indexing under the default named context did not register the repo:\n" + out
    )
