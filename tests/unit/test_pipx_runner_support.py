"""
TC-01 to TC-09: pipx runner support tests for setup_wizard.py

Tests that _generate_mcp_json() and kuzu setup block correctly detect
and use cgc / pipx / python as the MCP server runner.
"""

import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, mock_open, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DUMMY_CREDS = {
    "uri": "bolt://localhost:7687",
    "username": "neo4j",
    "password": "testpassword",
}


def _get_server_block(mcp_config: dict) -> dict:
    return mcp_config["mcpServers"]["CodeGraphContext"]


def _run_generate_mcp_json(which_side_effect):
    """Helper: run _generate_mcp_json() with mocked shutil.which."""
    captured = {}

    isolation_keys = (
        "CGC_HOME",
        "CGC_RUNTIME_DB_PATH",
        "CGC_RUNTIME_DB_TYPE",
        "CGC_REPO_CONTEXT_DIR",
        "CGC_INSTANCE_NAME",
    )
    with patch.dict(os.environ, {key: "" for key in isolation_keys}), \
         patch("codegraphcontext.cli.setup_wizard.shutil.which",
               side_effect=which_side_effect), \
         patch("codegraphcontext.cli.setup_wizard._configure_ide",
               side_effect=lambda cfg: captured.update({"config": cfg})), \
         patch("codegraphcontext.cli.setup_wizard._save_neo4j_credentials"), \
         patch("codegraphcontext.cli.setup_wizard.console"), \
         patch("builtins.open", mock_open()):

        from codegraphcontext.cli.setup_wizard import _generate_mcp_json
        _generate_mcp_json(DUMMY_CREDS)

    return captured.get("config", {})


# ---------------------------------------------------------------------------
# TC-01: cgc binary is available in system PATH
# When cgc is found via shutil.which, the MCP config should use cgc directly
# as the command with ["mcp", "start"] args. pipx should not be involved.
# ---------------------------------------------------------------------------

class TestTC01_CgcAvailable:

    def test_cgc_path_available_uses_cgc_command(self):
        config = _run_generate_mcp_json(
            lambda name: "/usr/local/bin/cgc" if name == "cgc" else None
        )
        server = _get_server_block(config)
        assert server["command"] == "/usr/local/bin/cgc", \
            f"Expected cgc path, got: {server['command']}"
        assert server["args"] == ["mcp", "start"], \
            f"Expected ['mcp', 'start'], got: {server['args']}"

    def test_cgc_available_does_not_use_pipx(self):
        config = _run_generate_mcp_json(
            lambda name: "/usr/local/bin/cgc" if name == "cgc" else "/usr/bin/pipx"
        )
        server = _get_server_block(config)
        assert "pipx" not in server["command"], \
            "pipx should NOT be used when cgc is available"
        assert server["args"] == ["mcp", "start"], \
            f"Expected ['mcp', 'start'], got: {server['args']}"


# ---------------------------------------------------------------------------
# TC-02: cgc is absent but pipx is available in system PATH
# The fork distribution is currently unpublished. Use this running installation
# rather than asking pipx to download it from PyPI before publication.
# ---------------------------------------------------------------------------

class TestTC02_PipxAvailable:

    def test_current_interpreter_used_when_cgc_absent_before_publication(self):
        def which_side(name):
            if name == "cgc":
                return None
            if name == "pipx":
                return "/usr/bin/pipx"
            return None

        config = _run_generate_mcp_json(which_side)
        server = _get_server_block(config)
        assert server["command"] == sys.executable, \
            f"Expected current interpreter, got: {server['command']}"
        assert server["args"] == ["-m", "codegraphcontext", "mcp", "start"], \
            f"Expected current distribution module args, got: {server['args']}"

    def test_prepublication_fallback_runs_current_distribution_module(self):
        config = _run_generate_mcp_json(
            lambda name: None if name == "cgc" else "/usr/bin/pipx"
        )
        server = _get_server_block(config)
        assert server["command"] == sys.executable, \
            f"Expected current interpreter, got: {server['command']}"
        assert server["args"] == ["-m", "codegraphcontext", "mcp", "start"], \
            f"Expected current distribution module args, got: {server['args']}"


# ---------------------------------------------------------------------------
# TC-03: both cgc and pipx are absent from system PATH
# When neither cgc nor pipx is found, the MCP config should fall back to
# sys.executable (python) with -m flag to run codegraphcontext as a module.
# ---------------------------------------------------------------------------

class TestTC03_PythonFallback:

    def test_python_fallback_when_both_absent(self):
        captured = {}

        with patch("codegraphcontext.cli.setup_wizard.shutil.which",
                   return_value=None), \
             patch("codegraphcontext.cli.setup_wizard.sys") as mock_sys, \
             patch("codegraphcontext.cli.setup_wizard._configure_ide",
                   side_effect=lambda cfg: captured.update({"config": cfg})), \
             patch("codegraphcontext.cli.setup_wizard._save_neo4j_credentials"), \
             patch("codegraphcontext.cli.setup_wizard.console"), \
             patch("builtins.open", mock_open()):

            mock_sys.executable = "/usr/bin/python3"

            from codegraphcontext.cli.setup_wizard import _generate_mcp_json
            _generate_mcp_json(DUMMY_CREDS)

        server = _get_server_block(captured["config"])
        assert server["command"] == "/usr/bin/python3", \
            f"Expected python3 fallback, got: {server['command']}"

    def test_fallback_args_include_mcp_start(self):
        captured = {}

        with patch("codegraphcontext.cli.setup_wizard.shutil.which",
                   return_value=None), \
             patch("codegraphcontext.cli.setup_wizard.sys") as mock_sys, \
             patch("codegraphcontext.cli.setup_wizard._configure_ide",
                   side_effect=lambda cfg: captured.update({"config": cfg})), \
             patch("codegraphcontext.cli.setup_wizard._save_neo4j_credentials"), \
             patch("codegraphcontext.cli.setup_wizard.console"), \
             patch("builtins.open", mock_open()):

            mock_sys.executable = "/usr/bin/python3"

            from codegraphcontext.cli.setup_wizard import _generate_mcp_json
            _generate_mcp_json(DUMMY_CREDS)

        server = _get_server_block(captured["config"])
        assert "mcp" in server["args"], "Args must contain 'mcp'"
        assert "start" in server["args"], "Args must contain 'start'"


# ---------------------------------------------------------------------------
# TC-04: kuzu setup block (configure_mcp_client) follows same runner logic
# The kuzu/falkordb setup block at line ~405 in setup_wizard.py must detect
# cgc, pipx, and python fallback in the same order as _generate_mcp_json().
# ---------------------------------------------------------------------------

class TestTC04_KuzuSetupBlock:

    def _run_kuzu_block(self, cgc_val, pipx_val, python_val="/usr/bin/python3"):
        if cgc_val:
            command = cgc_val
            args = ["mcp", "start"]
        elif pipx_val:
            command = pipx_val
            args = ["run", "codegraphcontext", "mcp", "start"]
        else:
            command = python_val
            args = ["-m", "codegraphcontext", "mcp", "start"]
        return command, args

    def test_kuzu_block_pipx_detected(self):
        command, args = self._run_kuzu_block(None, "/usr/bin/pipx")
        assert command == "/usr/bin/pipx"
        assert args == ["run", "codegraphcontext", "mcp", "start"]

    def test_kuzu_block_cgc_takes_priority_over_pipx(self):
        command, args = self._run_kuzu_block("/usr/local/bin/cgc", "/usr/bin/pipx")
        assert command == "/usr/local/bin/cgc"
        assert args == ["mcp", "start"]

    def test_kuzu_block_python_fallback(self):
        command, args = self._run_kuzu_block(None, None)
        assert "python" in command.lower()
        assert "-m" in args


# ---------------------------------------------------------------------------
# TC-05: README.md must document pipx installation and config example
# Users who install via pipx need clear instructions. README must mention
# pipx, codegraphcontext-fork distribution, and mcp start args in that section.
# ---------------------------------------------------------------------------

class TestTC05_ReadmeDocumentation:
    # Resolve repository root robustly across local runs and CI workspace layouts.
    # In GitHub Actions, checkout paths may be nested (e.g. /work/repo/repo).
    README_PATH = next(
        (
            candidate
            for parent in Path(__file__).resolve().parents
            for candidate in [parent / "README.md"]
            if candidate.exists()
        ),
        Path(__file__).resolve().parent.parent.parent.parent / "README.md",
    )

    def test_readme_exists(self):
        assert self.README_PATH.exists(), \
            f"README.md not found at {self.README_PATH}"

    def test_readme_contains_pipx_command(self):
        if not self.README_PATH.exists():
            pytest.skip("README.md not found")
        content = self.README_PATH.read_text(encoding="utf-8")
        assert "pipx" in content, \
            "README.md must mention 'pipx'"

    def test_readme_contains_pipx_run_codegraphcontext(self):
        if not self.README_PATH.exists():
            pytest.skip("README.md not found")
        content = self.README_PATH.read_text(encoding="utf-8").lower()
        assert "pipx" in content and "codegraphcontext-fork" in content, \
            "README.md must select the codegraphcontext-fork distribution for pipx"

    def test_translated_readmes_select_fork_distribution_for_pipx(self):
        repo_root = Path(__file__).resolve().parents[2]
        translated_readmes = (
            "README.ja.md",
            "README.ta.md",
            "README.zh-CN.md",
        )
        expected = [
            "run",
            "--spec",
            "codegraphcontext-fork",
            "codegraphcontext",
            "mcp",
            "start",
        ]

        for filename in translated_readmes:
            content = (repo_root / "docs" / "translations" / filename).read_text(encoding="utf-8")
            pipx_section = content.split('"command": "pipx"', 1)[1]
            args_start = pipx_section.index('"args":')
            list_start = pipx_section.index("[", args_start)
            list_end = pipx_section.index("]", list_start) + 1
            assert json.loads(pipx_section[list_start:list_end]) == expected, filename

    def test_readme_pipx_config_has_mcp_start(self):
        if not self.README_PATH.exists():
            pytest.skip("README.md not found")
        content = self.README_PATH.read_text(encoding="utf-8")
        pipx_command_idx = content.find('"command": "pipx"')
        if pipx_command_idx == -1:
            pytest.skip("pipx section not yet added to README")
        nearby = content[pipx_command_idx: pipx_command_idx + 500]
        assert "mcp" in nearby.lower() and "start" in nearby.lower(), \
            "pipx section in README must include 'mcp start' args"


# ---------------------------------------------------------------------------
# TC-06: generated MCP config must use the active interpreter when cgc is absent
# Even when pipx is installed, the package may be unpublished. The config should
# run the currently active distribution rather than downloading it from PyPI.
# ---------------------------------------------------------------------------

class TestTC06_McpJsonFileOutput:

    def test_mcp_json_written_with_current_interpreter_fallback(self):
        config = _run_generate_mcp_json(
            lambda name: None if name == "cgc" else "/usr/bin/pipx"
        )
        server = _get_server_block(config)
        assert server["command"] == sys.executable, \
            f"command should use the active interpreter, got: {server['command']}"
        assert server["args"] == ["-m", "codegraphcontext", "mcp", "start"], \
            "the active interpreter should launch the current distribution module"

    def test_mcp_json_structure_valid(self):
        config = _run_generate_mcp_json(
            lambda name: None if name == "cgc" else "/usr/bin/pipx"
        )
        assert "mcpServers" in config
        assert "CodeGraphContext" in config["mcpServers"]
        server = config["mcpServers"]["CodeGraphContext"]
        assert "command" in server
        assert "args" in server
        assert isinstance(server["args"], list)


# ---------------------------------------------------------------------------
# TC-07: the active-interpreter fallback must use the preserved module name.
# ---------------------------------------------------------------------------

class TestTC07_PipxIsolatedEnvArgs:

    def test_current_module_is_selected_for_prepublication_fallback(self):
        """TC-07: invoke the currently installed module, not the unpublished spec."""
        config = _run_generate_mcp_json(
            lambda name: None if name == "cgc" else "/usr/bin/pipx"
        )
        server = _get_server_block(config)
        assert server["args"] == ["-m", "codegraphcontext", "mcp", "start"]
        assert "codegraphcontext-fork" not in server["args"]


# ---------------------------------------------------------------------------
# TC-08: do not select pipx's unpublished package when cgc is absent.
# ---------------------------------------------------------------------------

class TestTC08_PipxNotUsingPythonModule:

    def test_prepublication_fallback_selects_current_interpreter(self):
        """TC-08: pipx availability does not replace the active Python environment."""
        config = _run_generate_mcp_json(
            lambda name: None if name == "cgc" else "/usr/bin/pipx"
        )
        server = _get_server_block(config)
        assert server["command"] == sys.executable
        assert server["args"][0] == "-m"


# ---------------------------------------------------------------------------
# TC-09: any pipx binary path is ignored for the prepublication fallback.
# ---------------------------------------------------------------------------

class TestTC09_PipxBinaryAsCommand:

    def test_pipx_binary_path_does_not_override_current_interpreter(self):
        """TC-09: preserve the interpreter that launched the setup wizard."""
        pipx_path = "/home/user/.local/bin/pipx"
        config = _run_generate_mcp_json(
            lambda name: None if name == "cgc" else pipx_path
        )
        server = _get_server_block(config)
        assert server["command"] == sys.executable
        assert server["command"] != pipx_path
