"""Integration coverage for the built fork wheel's metadata and console scripts."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.integration
def test_installed_wheel_metadata_and_cli_versions(tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    configured_wheel = os.environ.get("CGC_DISTRIBUTION_WHEEL")
    if configured_wheel:
        wheel = Path(configured_wheel).resolve()
    else:
        wheels = sorted((repo_root / "dist").glob("codegraphcontext_fork-0.7.0-*.whl"))
        if not wheels:
            pytest.skip("Build the codegraphcontext-fork wheel before running this integration test")
        wheel = wheels[-1]
    assert wheel.is_file(), f"Configured fork wheel does not exist: {wheel}"

    installed = tmp_path / "site-packages"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(installed),
            "CGC_HOME": str(tmp_path / "cgc-home"),
            "CGC_RUNTIME_DB_PATH": str(tmp_path / "runtime-db"),
            "CGC_REPO_CONTEXT_DIR": ".codegraphcontext-fork",
            "CGC_INSTANCE_NAME": "Fork wheel integration test",
        }
    )
    metadata_script = """
import importlib.metadata
from pathlib import Path
import sys
import codegraphcontext

installed = Path(sys.argv[1]).resolve()
assert Path(codegraphcontext.__file__).resolve().is_relative_to(installed)
assert importlib.metadata.version("codegraphcontext-fork") == "0.7.0"
"""
    metadata_result = subprocess.run(
        [sys.executable, "-c", metadata_script, str(installed)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert metadata_result.returncode == 0, metadata_result.stdout + metadata_result.stderr

    script_dir = installed / ("Scripts" if os.name == "nt" else "bin")
    executable_suffix = ".exe" if os.name == "nt" else ""
    for command in ("cgc", "codegraphcontext"):
        executable = script_dir / f"{command}{executable_suffix}"
        assert executable.is_file(), f"Wheel is missing the {command} console entry point"
        result = subprocess.run(
            [str(executable), "--version"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "0.7.0" in output, output
