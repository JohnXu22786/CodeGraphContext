"""Regression coverage for index previews and the indexing file boundary."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codegraphcontext.cli import cli_helpers, config_manager, main as cli_main
from codegraphcontext.cli.main import app
from codegraphcontext.tools.handlers.indexing_handlers import add_code_to_graph
from codegraphcontext.tools.indexing.discovery import (
    discover_files_to_index,
    inspect_files_to_index,
)
from codegraphcontext.utils.path_sandbox import is_path_allowed
from codegraphcontext.cli.config_manager import DEFAULT_CGCIGNORE_PATTERNS


def _relative_paths(root: Path, paths: list[Path]) -> list[str]:
    return [path.relative_to(root).as_posix() for path in paths]


def test_preview_matches_index_discovery_and_explains_exclusions(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text(
        "*.py\nsrc/**/generated/\n", encoding="utf-8"
    )
    (repo / ".cgcignore").write_text("!keep.py\nsecret.txt\n", encoding="utf-8")

    (repo / "keep.py").write_text("pass\n", encoding="utf-8")
    (repo / "ignored.py").write_text("pass\n", encoding="utf-8")
    (repo / "README.md").write_text("notes\n", encoding="utf-8")
    (repo / "secret.txt").write_text("private\n", encoding="utf-8")
    (repo / "unknown.bin").write_bytes(b"binary")
    (repo / "logo.png").write_bytes(b"image")
    generated = repo / "src" / "nested" / "generated"
    generated.mkdir(parents=True)
    (generated / "Auto.cs").write_text("class Auto {}\n", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "module.py").write_text("cached\n", encoding="utf-8")
    (repo / ".codegraphcontext" / "db").mkdir(parents=True)
    (repo / ".codegraphcontext" / "db" / "state.bin").write_bytes(b"state")

    monkeypatch.setenv(
        "IGNORE_DIRS",
        "node_modules,venv,.venv,env,.env,dist,build,target,out,.git,.idea,.vscode,__pycache__",
    )
    preview = inspect_files_to_index(repo, supported_extensions={".py"})
    assert inspect_files_to_index(repo, supported_extensions={".py"}) == preview
    indexed_files, _ = discover_files_to_index(repo, supported_extensions={".py"})

    assert preview.files == indexed_files
    assert ".codegraphcontext/" in DEFAULT_CGCIGNORE_PATTERNS.splitlines()
    assert set(_relative_paths(repo, preview.files)) == {
        ".gitignore",
        "README.md",
        "keep.py",
    }

    excluded = {
        path.relative_to(repo).as_posix(): reason
        for path, reason in preview.excluded
    }
    assert ".gitignore" not in excluded
    assert "ignored.py" in excluded
    assert ".gitignore" in excluded["ignored.py"]
    assert "*.py" in excluded["ignored.py"]
    assert ".cgcignore" in excluded["secret.txt"]
    assert "secret.txt" in excluded["secret.txt"]
    assert "unknown.bin" in excluded and "no parser" in excluded["unknown.bin"]
    assert "logo.png" in excluded and "*.png" in excluded["logo.png"]
    assert "src/nested/generated" in excluded
    assert ".gitignore" in excluded["src/nested/generated"]
    assert ".codegraphcontext" in excluded
    assert ".codegraphcontext/" in excluded[".codegraphcontext"]
    assert "__pycache__" in excluded
    assert "IGNORE_DIRS" in excluded["__pycache__"]


def test_preview_uses_global_defaults_when_configured_ignore_file_is_missing(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    cache_report = repo / ".pytest_cache" / "report.json"
    cache_report.parent.mkdir()
    cache_report.write_text("{}\n", encoding="utf-8")
    missing_global_ignore = tmp_path / "cgc-home" / "global" / ".cgcignore"
    monkeypatch.setenv("IGNORE_DIRS", "")

    preview = inspect_files_to_index(
        repo,
        cgcignore_path=str(missing_global_ignore),
        supported_extensions={".json"},
    )
    indexed, _ = discover_files_to_index(
        repo,
        cgcignore_path=str(missing_global_ignore),
        supported_extensions={".json"},
    )

    excluded = {path: reason for path, reason in preview.excluded}
    assert cache_report not in preview.files
    assert ".pytest_cache" in excluded[cache_report.parent]
    assert indexed == preview.files
    assert not missing_global_ignore.exists()

    preview_after_index_bootstrap = inspect_files_to_index(
        repo,
        cgcignore_path=str(missing_global_ignore),
        supported_extensions={".json"},
    )
    indexed_after_bootstrap, _ = discover_files_to_index(
        repo,
        cgcignore_path=str(missing_global_ignore),
        supported_extensions={".json"},
    )

    assert cache_report not in preview_after_index_bootstrap.files
    assert indexed_after_bootstrap == preview_after_index_bootstrap.files


def test_missing_configured_defaults_do_not_override_gitignore_negations(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text("!dist/\n", encoding="utf-8")
    release_file = repo / "dist" / "release.json"
    release_file.parent.mkdir()
    release_file.write_text("{}\n", encoding="utf-8")
    missing_global_ignore = tmp_path / "cgc-home" / "global" / ".cgcignore"
    monkeypatch.setenv("IGNORE_DIRS", "")

    preview = inspect_files_to_index(
        repo,
        cgcignore_path=str(missing_global_ignore),
        supported_extensions={".json"},
    )
    indexed, _ = discover_files_to_index(
        repo,
        cgcignore_path=str(missing_global_ignore),
        supported_extensions={".json"},
    )

    assert release_file in preview.files
    assert indexed == preview.files
    assert not missing_global_ignore.exists()


def test_bootstrapped_global_defaults_match_dry_run_and_index_negations(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text("!dist/\nignored.json\n", encoding="utf-8")
    release_file = repo / "dist" / "release.json"
    release_file.parent.mkdir()
    release_file.write_text("{}\n", encoding="utf-8")
    ignored_file = repo / "ignored.json"
    ignored_file.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path / "cgc-home")
    monkeypatch.setenv("IGNORE_DIRS", "")

    assert config_manager.ensure_global_cgcignore()
    global_ignore = config_manager.CONFIG_DIR / "global" / ".cgcignore"
    with global_ignore.open("a", encoding="utf-8") as ignore_file:
        ignore_file.write("!ignored.json\n")

    preview = inspect_files_to_index(
        repo,
        cgcignore_path=str(global_ignore),
        supported_extensions={".json"},
    )
    indexed, _ = discover_files_to_index(
        repo,
        cgcignore_path=str(global_ignore),
        supported_extensions={".json"},
    )

    assert release_file in preview.files
    assert ignored_file in preview.files
    assert indexed == preview.files


def test_generated_local_defaults_do_not_override_gitignore_negations(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text("!*.png\n", encoding="utf-8")
    image = repo / "logo.png"
    image.write_bytes(b"image")
    monkeypatch.setenv("IGNORE_DIRS", "")

    preview = inspect_files_to_index(repo, supported_extensions={".png"})
    indexed, _ = discover_files_to_index(repo, supported_extensions={".png"})

    assert image in preview.files
    assert indexed == preview.files
    assert image in indexed
    assert (repo / ".cgcignore").is_file()

    user_rule_file = repo / "notes.txt"
    user_rule_file.write_text("user rule\n", encoding="utf-8")
    with (repo / ".cgcignore").open("a", encoding="utf-8") as ignore_file:
        ignore_file.write("notes.txt\n")

    customized_preview = inspect_files_to_index(repo, supported_extensions={".png"})
    customized_index, _ = discover_files_to_index(repo, supported_extensions={".png"})
    customized_exclusions = {path: reason for path, reason in customized_preview.excluded}

    assert customized_index == customized_preview.files
    assert user_rule_file not in customized_preview.files
    assert "notes.txt" in customized_exclusions[user_rule_file]


def test_preview_is_read_only_deterministic_and_handles_empty_and_file_roots(
    tmp_path: Path, monkeypatch
):
    empty_repo = tmp_path / "empty"
    empty_repo.mkdir()

    first = inspect_files_to_index(empty_repo, supported_extensions={".py"})
    second = inspect_files_to_index(empty_repo, supported_extensions={".py"})

    assert first == second
    assert first.files == []
    assert not (empty_repo / ".cgcignore").exists()

    single_file = tmp_path / "single.py"
    single_file.write_text("pass\n", encoding="utf-8")
    single_preview = inspect_files_to_index(single_file, supported_extensions={".py"})
    assert single_preview.files == [single_file]

    local_context = empty_repo / ".codegraphcontext"
    monkeypatch.setattr(
        config_manager,
        "load_context_config",
        lambda **_kwargs: SimpleNamespace(mode="per-repo", contexts={}, default_context=None),
    )
    monkeypatch.setattr(config_manager, "find_local_cgc_dir", lambda _cwd: None)
    monkeypatch.setattr(config_manager, "get_repo_context_path", lambda _cwd: local_context)
    monkeypatch.setattr(
        config_manager,
        "load_config",
        lambda **_kwargs: {"DEFAULT_DATABASE": "kuzudb"},
    )
    monkeypatch.delenv("CGC_RUNTIME_DB_TYPE", raising=False)
    monkeypatch.delenv("DEFAULT_DATABASE", raising=False)
    resolved_context = config_manager.resolve_context(
        cwd=empty_repo, create_local_context=False, read_only=True
    )

    assert resolved_context.cgcignore_path == str(local_context / ".cgcignore")
    assert not local_context.exists()


def test_read_only_context_resolution_reads_legacy_config_without_migrating(
    tmp_path: Path, monkeypatch
):
    legacy_config = tmp_path / "cgc_config.yaml"
    current_config = tmp_path / "config.yaml"
    legacy_config.write_text(
        "version: 1\nmode: per-repo\ndefault_context: ''\ncontexts: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_manager, "CONTEXT_CONFIG_FILE", current_config)
    monkeypatch.setattr(config_manager, "_LEGACY_CONTEXT_CONFIG_FILE", legacy_config)

    loaded = config_manager.load_context_config(create_if_missing=False)

    assert loaded.mode == "per-repo"
    assert not current_config.exists()


def test_preview_does_not_follow_symlinks_outside_the_repository(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("pass\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.py"
    secret.write_text("secret\n", encoding="utf-8")

    try:
        (repo / "external.py").symlink_to(secret)
        (repo / "external-dir").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")

    preview = inspect_files_to_index(repo, supported_extensions={".py"})

    assert preview.files == [repo / "main.py"]
    excluded = {path.name: reason for path, reason in preview.excluded}
    assert "outside" in excluded["external.py"]
    assert "symlink" in excluded["external-dir"]
    assert secret not in preview.files


def test_single_file_external_symlink_is_excluded_before_cli_initialization(
    tmp_path: Path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.py"
    secret.write_text("secret\n", encoding="utf-8")
    alias = repo / "external.py"
    try:
        alias.symlink_to(secret)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")

    preview = inspect_files_to_index(alias, supported_extensions={".py"})
    indexed, _ = discover_files_to_index(alias, supported_extensions={".py"})

    assert preview.ignore_root == repo
    assert preview.files == indexed == []
    assert preview.excluded == [
        (alias, "symbolic-link target is outside the repository root")
    ]

    monkeypatch.setenv("IGNORE_DIRS", "")
    with patch.object(
        cli_helpers,
        "resolve_context",
        return_value=SimpleNamespace(cgcignore_path=None),
    ):
        cli_helpers.dry_run_index_helper(str(alias))
    output = capsys.readouterr().out
    assert "Files to index (0)" in output
    assert "external.py: symbolic-link target is outside the repository root" in output
    assert "secret.py" not in output

    for helper in (cli_helpers.index_helper, cli_helpers.reindex_helper):
        with patch.object(cli_helpers, "_initialize_services") as initialize_mock:
            with pytest.raises(cli_helpers.typer.Exit):
                helper(str(alias))
        initialize_mock.assert_not_called()


def test_dry_run_external_symlink_through_symlinked_parent_reports_exclusion(
    tmp_path: Path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    repo_alias = tmp_path / "repo-alias"
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.py"
    secret.write_text("secret\n", encoding="utf-8")
    link = repo / "external.py"
    try:
        repo_alias.symlink_to(repo, target_is_directory=True)
        link.symlink_to(secret)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")
    requested_path = repo_alias / "external.py"

    preview = inspect_files_to_index(requested_path, supported_extensions={".py"})
    assert preview.ignore_root == repo
    assert preview.files == []
    assert preview.excluded == [
        (repo / "external.py", "symbolic-link target is outside the repository root")
    ]

    monkeypatch.setenv("IGNORE_DIRS", "")
    with patch.object(
        cli_helpers,
        "resolve_context",
        return_value=SimpleNamespace(cgcignore_path=None),
    ):
        cli_helpers.dry_run_index_helper(str(requested_path))

    output = capsys.readouterr().out
    assert "external.py: symbolic-link target is outside the repository root" in output
    assert "secret.py" not in output


def test_index_and_reindex_reject_missing_root_before_service_initialization(
    tmp_path: Path,
):
    missing = tmp_path / "missing-repository"
    for helper in (cli_helpers.index_helper, cli_helpers.reindex_helper):
        with patch.object(
            cli_helpers,
            "_initialize_services",
            return_value=(None, None, None, None),
        ) as initialize_mock:
            with pytest.raises(cli_helpers.typer.Exit):
                helper(str(missing))
        initialize_mock.assert_not_called()


def test_preview_collapses_in_repository_file_symlink_aliases(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "target.py"
    target.write_text("pass\n", encoding="utf-8")
    alias = repo / "alias.py"
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")

    preview = inspect_files_to_index(repo, supported_extensions={".py"})
    indexed, _ = discover_files_to_index(repo, supported_extensions={".py"})

    assert preview.files == indexed == [target]


def test_preview_does_not_exclude_target_indexed_through_supported_alias(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "target.bin"
    target.write_bytes(b"data")
    alias = repo / "alias.py"
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")

    preview = inspect_files_to_index(repo, supported_extensions={".py"})
    indexed, _ = discover_files_to_index(repo, supported_extensions={".py"})
    excluded_paths = {path for path, _reason in preview.excluded}

    assert preview.files == [target]
    assert indexed == [alias]
    assert target not in excluded_paths


def test_index_discovery_keeps_ignore_rules_when_default_file_cannot_be_created(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (repo / "main.py").write_text("pass\n", encoding="utf-8")
    ignored = repo / "ignored" / "generated.py"
    ignored.parent.mkdir()
    ignored.write_text("pass\n", encoding="utf-8")
    cached = repo / ".pytest_cache" / "cached.py"
    cached.parent.mkdir()
    cached.write_text("pass\n", encoding="utf-8")

    def fail_default_creation(_path, _patterns):
        raise PermissionError("repository is read-only")

    monkeypatch.setattr(
        "codegraphcontext.core.cgcignore.ensure_default_cgcignore",
        fail_default_creation,
    )
    global_ignore = tmp_path / "global" / ".cgcignore"
    preview = inspect_files_to_index(
        repo, cgcignore_path=str(global_ignore), supported_extensions={".py"}
    )
    indexed, _ = discover_files_to_index(
        repo, cgcignore_path=str(global_ignore), supported_extensions={".py"}
    )

    assert preview.files == [repo / ".gitignore", repo / "main.py"]
    assert indexed == preview._indexing_files
    assert not (repo / ".cgcignore").exists()


def test_dry_run_quoted_tilde_path_matches_index_path(
    tmp_path: Path, monkeypatch, capsys
):
    workdir = tmp_path / "work"
    literal_repo = workdir / "~" / "repo"
    literal_repo.mkdir(parents=True)
    (literal_repo / "literal.py").write_text("pass\n", encoding="utf-8")
    home = tmp_path / "home"
    home_repo = home / "repo"
    home_repo.mkdir(parents=True)
    (home_repo / "home.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("IGNORE_DIRS", "")

    with patch.object(
        cli_helpers,
        "resolve_context",
        return_value=SimpleNamespace(cgcignore_path=None),
    ) as resolve_context_mock:
        cli_helpers.dry_run_index_helper("~/repo")

    assert "literal.py" in capsys.readouterr().out
    resolve_context_mock.assert_called_once_with(
        None,
        cwd=literal_repo,
        create_local_context=False,
        read_only=True,
    )
    with (
        patch.object(cli_helpers, "_initialize_services", return_value=(None, None, None, None))
        as initialize_mock,
        patch.object(
            cli_helpers,
            "_fail_services_init",
            side_effect=cli_helpers.typer.Exit(code=1),
        ),
    ):
        with pytest.raises(cli_helpers.typer.Exit):
            cli_helpers.index_helper("~/repo")

    initialize_mock.assert_called_once_with(None, cwd=literal_repo)


@pytest.mark.parametrize(
    ("ignore_file", "ignore_pattern", "ignore_dirs"),
    [
        (".gitignore", "ignored/target.py", ""),
        (".cgcignore", "ignored/target.py", ""),
        (None, None, "ignored"),
    ],
)
def test_preview_honors_ignore_rules_for_symlink_targets(
    tmp_path: Path,
    monkeypatch,
    ignore_file: str | None,
    ignore_pattern: str | None,
    ignore_dirs: str,
):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    ignored_target = repo / "ignored" / "target.py"
    ignored_target.parent.mkdir()
    ignored_target.write_text("pass\n", encoding="utf-8")
    if ignore_file:
        (repo / ignore_file).write_text(f"{ignore_pattern}\n", encoding="utf-8")
    monkeypatch.setenv("IGNORE_DIRS", ignore_dirs)
    alias = repo / "alias.py"
    try:
        alias.symlink_to(ignored_target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")

    preview = inspect_files_to_index(repo, supported_extensions={".py"})
    indexed, _ = discover_files_to_index(repo, supported_extensions={".py"})
    excluded = {path: reason for path, reason in preview.excluded}

    expected_files = [repo / ".gitignore"] if ignore_file == ".gitignore" else []
    assert preview.files == indexed == expected_files
    if ignore_file:
        assert "ignored/target.py" in excluded[ignored_target]
        assert ignore_file in excluded[alias]
    else:
        assert "IGNORE_DIRS" in excluded[repo / "ignored"]
        assert "IGNORE_DIRS" in excluded[alias]
    assert "target" in excluded[alias].lower()


def test_preview_does_not_treat_ignored_directory_names_as_target_filenames(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "out"
    target.write_text("pass\n", encoding="utf-8")
    alias = repo / "alias.py"
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")
    monkeypatch.setenv("IGNORE_DIRS", "out")

    preview = inspect_files_to_index(repo, supported_extensions={".py"})
    indexed, _ = discover_files_to_index(repo, supported_extensions={".py"})

    assert preview.files == [target]
    assert indexed == [alias]


def test_preview_explains_walk_permission_errors(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    unreadable = repo / "locked"
    unreadable.mkdir()

    def failing_walk(path, topdown=True, onerror=None):
        onerror(PermissionError(13, "permission denied", str(unreadable)))
        return iter(())

    monkeypatch.setattr("codegraphcontext.tools.indexing.discovery.os.walk", failing_walk)

    preview = inspect_files_to_index(repo, supported_extensions={".py"})
    excluded = {path: reason for path, reason in preview.excluded}

    assert unreadable in excluded
    assert "permission denied" in excluded[unreadable].lower()


def test_cli_dry_run_prints_scope_without_initializing_database(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("pass\n", encoding="utf-8")

    monkeypatch.setenv("IGNORE_DIRS", "")
    with (
        patch.object(cli_helpers, "ensure_first_run_bootstrap"),
        patch.object(
            cli_helpers,
            "resolve_context",
            return_value=SimpleNamespace(cgcignore_path=None),
        ) as resolve_context_mock,
        patch.object(
            cli_helpers,
            "get_database_manager",
            side_effect=AssertionError("dry-run must not initialize database services"),
        ),
        patch("codegraphcontext.cli.main._load_credentials") as load_credentials_mock,
    ):
        result = CliRunner().invoke(app, ["index", "--dry-run", str(repo)])

    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower()
    assert "main.py" in result.output
    assert not (repo / ".cgcignore").exists()
    load_credentials_mock.assert_called_once_with(read_only=True)
    resolve_context_mock.assert_called_once_with(
        None, cwd=repo, create_local_context=False, read_only=True
    )


def test_direct_index_call_defaults_to_real_indexing(tmp_path: Path):
    with (
        patch.object(cli_main, "_load_credentials") as load_credentials_mock,
        patch.object(cli_main, "index_helper") as index_helper_mock,
        patch.object(cli_main, "dry_run_index_helper") as dry_run_helper_mock,
    ):
        cli_main.index(
            str(tmp_path),
            force=False,
            context=None,
            summarize=False,
            no_progress=False,
        )

    load_credentials_mock.assert_called_once_with(read_only=False)
    index_helper_mock.assert_called_once_with(str(tmp_path), None, no_progress=False)
    dry_run_helper_mock.assert_not_called()


def test_cli_dry_run_without_database_setting_skips_database_manager(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("pass\n", encoding="utf-8")
    isolated_home = tmp_path / "cgc-home"
    context = SimpleNamespace(mode="global", database=None, cgcignore_path=None)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CGC_HOME", str(isolated_home))
    monkeypatch.setenv("CGC_IGNORE_PROJECT_ENV", "1")
    monkeypatch.setenv("IGNORE_DIRS", "")
    for key in ("CGC_RUNTIME_DB_TYPE", "DATABASE_TYPE", "DEFAULT_DATABASE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", isolated_home)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", isolated_home / ".env")

    with (
        patch.object(cli_helpers, "ensure_first_run_bootstrap"),
        patch.object(cli_helpers, "resolve_context", return_value=context),
        patch.object(config_manager, "resolve_context", return_value=context),
        patch(
            "codegraphcontext.core.get_database_manager",
            side_effect=AssertionError("dry-run must not initialize database services"),
        ) as get_database_manager_mock,
    ):
        result = CliRunner().invoke(app, ["index", "--dry-run", str(repo)])

    assert result.exit_code == 0, result.output
    assert "main.py" in result.output
    get_database_manager_mock.assert_not_called()
    assert not isolated_home.exists()


def test_cli_dry_run_does_not_create_global_config_home(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("pass\n", encoding="utf-8")
    isolated_home = tmp_path / "cgc-home"
    env = os.environ.copy()
    env.update(
        {
            "CGC_HOME": str(isolated_home),
            "CGC_RUNTIME_DB_TYPE": "falkordb",
            "DEFAULT_DATABASE": "falkordb",
            "DEBUG_LOGS": "true",
            "IGNORE_DIRS": "",
        }
    )
    env.pop("CGC_IGNORE_PROJECT_ENV", None)
    env.pop("CGC_LOAD_PROJECT_ENV", None)
    source_root = Path(__file__).resolve().parents[3] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(source_root), env.get("PYTHONPATH")))
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "codegraphcontext.cli.main",
            "index",
            "--dry-run",
            str(repo),
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Index dry run" in result.stdout
    assert not isolated_home.exists()


def test_mcp_indexing_rejects_a_symlink_resolved_outside_allowed_roots(
    tmp_path: Path, monkeypatch
):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("secret\n", encoding="utf-8")
    link = allowed / "external"

    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")

    monkeypatch.chdir(allowed)
    monkeypatch.delenv("CGC_ALLOW_ALL_PATHS", raising=False)
    monkeypatch.delenv("CGC_ALLOWED_ROOTS", raising=False)
    graph_builder = MagicMock()
    result = add_code_to_graph(
        graph_builder,
        MagicMock(),
        MagicMock(),
        MagicMock(),
        path=str(link),
    )

    assert "outside the allowed roots" in result["error"]
    graph_builder.estimate_processing_time.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="UNC shares require Windows-specific setup")
def test_network_style_path_is_rejected_without_mounting_a_share(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CGC_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.delenv("CGC_ALLOW_ALL_PATHS", raising=False)

    assert not is_path_allowed(Path("//server/share/project"))
