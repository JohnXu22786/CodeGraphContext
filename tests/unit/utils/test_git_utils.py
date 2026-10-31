from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

from codegraphcontext.utils.git_utils import (
    has_git_admin_entry,
    has_unborn_git_head,
    get_repo_branch_name,
    get_repo_commit_hash,
)


def test_get_repo_commit_hash_success():
    with patch("subprocess.check_output") as mock_check_output:
        mock_check_output.return_value = (
            b"1234567890abcdef1234567890abcdef12345678\n"
        )

        result = get_repo_commit_hash(Path("."))

        assert result == "1234567890abcdef1234567890abcdef12345678"
        mock_check_output.assert_called_once()


def test_get_repo_commit_hash_empty_output():
    with patch("subprocess.check_output") as mock_check_output:
        mock_check_output.return_value = b"\n"

        assert get_repo_commit_hash(Path(".")) is None


def test_get_repo_commit_hash_called_process_error():
    with patch(
        "subprocess.check_output",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert get_repo_commit_hash(Path(".")) is None


def test_get_repo_commit_hash_file_not_found():
    with patch(
        "subprocess.check_output",
        side_effect=FileNotFoundError,
    ):
        assert get_repo_commit_hash(Path(".")) is None


def test_get_repo_commit_hash_os_error():
    with patch(
        "subprocess.check_output",
        side_effect=OSError,
    ):
        assert get_repo_commit_hash(Path(".")) is None


def test_has_unborn_git_head_detects_a_missing_symbolic_branch_ref(tmp_path):
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    assert has_unborn_git_head(tmp_path)


def test_has_unborn_git_head_does_not_treat_git_errors_as_missing_refs(tmp_path):
    from types import SimpleNamespace

    with (
        patch(
            "codegraphcontext.utils.git_utils.subprocess.check_output",
            side_effect=[b"true\n", b"refs/heads/main\n"],
        ),
        patch(
            "codegraphcontext.utils.git_utils.subprocess.run",
            return_value=SimpleNamespace(returncode=128),
        ),
    ):
        assert not has_unborn_git_head(tmp_path)


def test_has_git_admin_entry_tracks_an_ancestor_git_directory(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    assert has_git_admin_entry(nested)

    shutil.rmtree(tmp_path / ".git")
    assert not has_git_admin_entry(nested)


def test_has_git_admin_entry_rejects_a_broken_worktree_git_pointer(tmp_path):
    repo = tmp_path / "worktree"
    repo.mkdir()
    git_admin_dir = tmp_path / "git-admin"
    git_admin_dir.mkdir()
    (repo / ".git").write_text(f"gitdir: {git_admin_dir}\n", encoding="utf-8")

    assert has_git_admin_entry(repo)

    shutil.rmtree(git_admin_dir)
    assert not has_git_admin_entry(repo)


def test_has_git_admin_entry_does_not_fall_through_a_broken_nested_pointer(tmp_path):
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    nested = tmp_path / "nested-worktree"
    nested.mkdir()
    (nested / ".git").write_text(
        f"gitdir: {tmp_path / 'missing-admin'}\n",
        encoding="utf-8",
    )

    assert not has_git_admin_entry(nested)


def test_get_repo_branch_name_success():
    with patch("subprocess.check_output") as mock_check_output:
        mock_check_output.return_value = b"main\n"

        result = get_repo_branch_name(Path("."))

        assert result == "main"
        mock_check_output.assert_called_once()


def test_get_repo_branch_name_empty_output():
    with patch("subprocess.check_output") as mock_check_output:
        mock_check_output.return_value = b"\n"

        assert get_repo_branch_name(Path(".")) is None


def test_get_repo_branch_name_called_process_error():
    with patch(
        "subprocess.check_output",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert get_repo_branch_name(Path(".")) is None


def test_get_repo_branch_name_file_not_found():
    with patch(
        "subprocess.check_output",
        side_effect=FileNotFoundError,
    ):
        assert get_repo_branch_name(Path(".")) is None


def test_get_repo_branch_name_os_error():
    with patch(
        "subprocess.check_output",
        side_effect=OSError,
    ):
        assert get_repo_branch_name(Path(".")) is None
