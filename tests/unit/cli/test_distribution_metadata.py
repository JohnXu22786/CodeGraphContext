"""Regression coverage for the fork's distribution identity and version output."""

import importlib
import importlib.metadata
import json
import re
import zipfile
from pathlib import Path

from typer.testing import CliRunner


def test_root_version_uses_fork_distribution_metadata(monkeypatch):
    cli_main = importlib.import_module("codegraphcontext.cli.main")
    distributions = []

    def fake_version(distribution):
        distributions.append(distribution)
        return "0.7.0"

    monkeypatch.setattr(cli_main, "pkg_version", fake_version)

    result = CliRunner().invoke(cli_main.app, ["--version"])

    assert result.exit_code == 0, result.exception
    assert "0.7.0" in result.output
    assert distributions == ["codegraphcontext-fork"]


def test_mcp_server_version_uses_fork_distribution_metadata(monkeypatch):
    server_module = importlib.import_module("codegraphcontext.server")
    distributions = []

    def fake_version(distribution):
        distributions.append(distribution)
        return "0.7.0"

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    server = object.__new__(server_module.MCPServer)

    assert server._get_version() == "0.7.0"
    assert distributions == ["codegraphcontext-fork"]


def test_bundle_generator_records_fork_distribution_version(tmp_path, monkeypatch):
    from codegraphcontext.core.cgc_bundle import CGCBundle

    distributions = []

    def fake_version(distribution):
        distributions.append(distribution)
        return "0.7.0"

    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    bundle = CGCBundle(db_manager=None)
    monkeypatch.setattr(bundle, "_extract_metadata", lambda repo_path: {"repo": "test/repo"})
    monkeypatch.setattr(bundle, "_extract_schema", lambda: {})
    monkeypatch.setattr(bundle, "_create_manifest", lambda bundle_dir: {})

    def write_nodes(path, repo_path, exclude_labels=None):
        path.write_text("{}\n", encoding="utf-8")
        return 1

    def write_edges(path, repo_path):
        path.write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(bundle, "_extract_nodes", write_nodes)
    monkeypatch.setattr(bundle, "_extract_edges", write_edges)

    output = tmp_path / "version.cgc"
    success, _ = bundle.export_to_bundle(output, include_stats=False)

    assert success
    assert distributions == ["codegraphcontext-fork"]
    with zipfile.ZipFile(output) as archive:
        metadata = json.loads(archive.read("metadata.json"))
    assert metadata["generator"] == "PYv0.7.0"


def test_installation_guides_show_valid_windows_venv_activation_commands():
    repo_root = Path(__file__).resolve().parents[3]
    installation_guides = (
        "README.md",
        "docs/docs/getting-started/installation.md",
        "docs/translations/README.ja.md",
        "docs/translations/README.kor.md",
        "docs/translations/README.ru-RU.md",
        "docs/translations/README.ta.md",
        "docs/translations/README.uk.md",
        "docs/translations/README.zh-CN.md",
    )
    power_shell = r"`.\.venv\Scripts\Activate.ps1`"
    command_prompt = r"`.venv\Scripts\activate.bat`"
    invalid_windows_hint = r"On Windows: .venv\Scripts\activate"

    for relative_path in installation_guides:
        content = (repo_root / relative_path).read_text(encoding="utf-8")
        assert power_shell in content, relative_path
        assert command_prompt in content, relative_path
        assert invalid_windows_hint not in content, relative_path


def test_bundle_registry_install_dialog_requires_an_isolated_environment():
    repo_root = Path(__file__).resolve().parents[3]
    content = (repo_root / "website/src/components/BundleRegistrySection.tsx").read_text(encoding="utf-8")

    assert "Use a dedicated virtual environment." in content
    assert "upstream and fork distributions share the" in content
    assert "installing both in one environment can overwrite them" in content
    assert 'href="#installation"' in content


def test_pypi_download_widget_uses_cached_fork_stats_for_empty_api_data():
    repo_root = Path(__file__).resolve().parents[3]
    content = (repo_root / "website/src/components/ShowDownloads.tsx").read_text(encoding="utf-8")

    assert "function getCachedStats()" in content
    assert "if (!data?.data)" in content
    assert "const cachedStats = getCachedStats();" in content
    assert 'package: "codegraphcontext-fork"' in content
    assert re.search(
        r"function getCachedStats\(\): PypiStats \| null \{\s*try\s*\{[\s\S]*?localStorage\.getItem[\s\S]*?\}\s*catch\s*\{\s*return null;\s*\}",
        content,
    )
    assert re.search(
        r"function cacheLastMonth\(lastMonth: number\): void \{\s*try\s*\{[\s\S]*?localStorage\.setItem[\s\S]*?\}\s*catch\s*\{",
        content,
    )


def test_bundle_upgrade_examples_require_dedicated_environment():
    repo_root = Path(__file__).resolve().parents[3]
    relative_paths = (
        "docs/BUNDLE_QUICKREF.md",
        "docs/docs/guides/bundles.md",
    )

    for relative_path in relative_paths:
        content = (repo_root / relative_path).read_text(encoding="utf-8")
        section = content.split("### Version Mismatch", 1)[1].split("\n## ", 1)[0]

        assert "not published on PyPI" in section, relative_path
        assert "dedicated virtual environment" in section, relative_path
        assert "share the `codegraphcontext` import and CLI files" in section, relative_path
        assert "python -m pip install --upgrade codegraphcontext-fork" in section, relative_path
        assert "\npip install --upgrade codegraphcontext-fork" not in section, relative_path


def test_ci_install_example_requires_a_dedicated_virtual_environment():
    repo_root = Path(__file__).resolve().parents[3]
    content = (repo_root / "docs/CI_INTEGRATION.md").read_text(encoding="utf-8")

    assert "dedicated virtual environment" in content
    assert "python -m venv .venv" in content
    assert "source .venv/bin/activate" in content
    assert "shell: bash" in content


def test_readme_virtualenv_troubleshooting_does_not_use_global_path_fix():
    repo_root = Path(__file__).resolve().parents[3]
    readmes = (
        "README.md",
        "docs/translations/README.ja.md",
        "docs/translations/README.kor.md",
        "docs/translations/README.ru-RU.md",
        "docs/translations/README.ta.md",
        "docs/translations/README.uk.md",
        "docs/translations/README.zh-CN.md",
    )

    for relative_path in readmes:
        content = (repo_root / relative_path).read_text(encoding="utf-8")
        assert "post_install_fix.sh" not in content, relative_path


def test_manual_mcp_configs_use_isolated_distribution_runner():
    repo_root = Path(__file__).resolve().parents[3]
    config_docs = (
        "README.md",
        "docs/docs/getting-started/mcp-setup.md",
        "docs/translations/README.ja.md",
        "docs/translations/README.kor.md",
        "docs/translations/README.ru-RU.md",
        "docs/translations/README.ta.md",
        "docs/translations/README.uk.md",
        "docs/translations/README.zh-CN.md",
    )

    for relative_path in config_docs:
        content = (repo_root / relative_path).read_text(encoding="utf-8")
        assert '"command": "cgc"' not in content, relative_path
        assert '"command": "codegraphcontext"' not in content, relative_path
        assert '"command": "pipx"' in content, relative_path
        assert '"--spec"' in content, relative_path
        assert '"codegraphcontext-fork"' in content, relative_path

    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    assert "Desktop clients do not inherit the active virtual environment" in readme
    assert "client's own `PATH`" in readme


def test_docker_build_context_includes_package_provenance_markdown():
    repo_root = Path(__file__).resolve().parents[3]
    ignore_lines = (repo_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8").splitlines()
    markdown_ignore = ignore_lines.index("*.md")
    docker_copy = next(line for line in dockerfile if line.startswith("COPY pyproject.toml README.md"))

    for filename in ("README.md", "CHANGELOG.md", "UPSTREAM.md"):
        exception = f"!{filename}"
        assert exception in ignore_lines, filename
        assert ignore_lines.index(exception) > markdown_ignore, filename
        assert filename in docker_copy.split(), filename


def test_ghcr_docs_explain_public_visibility_step():
    repo_root = Path(__file__).resolve().parents[3]
    english_note = (
        "As part of the first image publication, the owner will set the package visibility to Public; "
        "anonymous pulls will be available after that change takes effect."
    )
    for relative_path in (
        "README.md",
        "docs/DOCKER.md",
        "CHANGELOG.md",
        "docs/docs/guides/side-by-side-installations.md",
    ):
        content = (repo_root / relative_path).read_text(encoding="utf-8")
        assert "not published yet" in content, relative_path
        assert english_note in content, relative_path

    chinese = (repo_root / "docs/translations/README.zh-CN.md").read_text(encoding="utf-8")
    assert "目前尚未发布" in chinese
    assert "首次发布镜像时，所有者会将软件包可见性设为公开；此更改生效后即可匿名拉取。" in chinese


def test_runtime_pip_install_hints_require_isolated_environment_guidance():
    repo_root = Path(__file__).resolve().parents[3]
    installation_guide = (
        "https://github.com/JohnXu22786/CodeGraphContext/blob/main/"
        "docs/docs/getting-started/installation.md"
    )
    hint_files = (
        "src/codegraphcontext/cli/main.py",
        "src/codegraphcontext/utils/tree_sitter_manager.py",
        "scripts/create-bundle.sh",
        "scripts/post_install_fix.sh",
        "extensions/vscode/src/mcp/client.ts",
    )

    for relative_path in hint_files:
        content = (repo_root / relative_path).read_text(encoding="utf-8")
        assert "dedicated virtual environment" in content, relative_path
        assert installation_guide in content, relative_path
        assert re.search(r"pip install ['\"]?codegraphcontext-fork", content), relative_path

    tree_sitter_hint = (repo_root / "src/codegraphcontext/utils/tree_sitter_manager.py").read_text(
        encoding="utf-8"
    )
    assert "pip install 'codegraphcontext-fork[parsing]'" in tree_sitter_hint


def test_bundle_quickref_support_link_uses_fork_issue_tracker():
    repo_root = Path(__file__).resolve().parents[3]
    content = (repo_root / "docs/BUNDLE_QUICKREF.md").read_text(encoding="utf-8")

    assert "https://github.com/JohnXu22786/CodeGraphContext/issues" in content
    assert "https://github.com/CodeGraphContext/CodeGraphContext/issues" not in content


def test_unpublished_package_install_examples_are_labeled_for_publication():
    repo_root = Path(__file__).resolve().parents[3]
    expected_notes = {
        "README.md": "not published on PyPI yet",
        ".cursor/skills/codegraphcontext/SKILL.md": "After 0.7.0 is published on PyPI",
        "docs/docs/getting-started/installation.md": "not published on PyPI yet",
        "docs/docs/getting-started/mcp-setup.md": "currently unpublished",
        "docs/CI_INTEGRATION.md": "not published on PyPI yet",
        "src/codegraphcontext/cli/main.py": "after 0.7.0 is published on PyPI",
        "src/codegraphcontext/utils/tree_sitter_manager.py": "after 0.7.0 is published on PyPI",
        "docs/BUNDLE_QUICKREF.md": "not published on PyPI yet",
        "docs/docs/guides/bundles.md": "not published on PyPI yet",
        "scripts/create-bundle.sh": "After the 0.7.0 PyPI publication",
        "scripts/post_install_fix.sh": "after 0.7.0 is published on PyPI",
        "extensions/vscode/src/mcp/client.ts": "After the 0.7.0 PyPI publication",
        "docs/translations/README.ja.md": "まだ公開されていません",
        "docs/translations/README.kor.md": "아직 PyPI에 게시되지 않았습니다",
        "docs/translations/README.ru-RU.md": "ещё не опубликован в PyPI",
        "docs/translations/README.ta.md": "இன்னும் PyPI-இல் வெளியிடப்படவில்லை",
        "docs/translations/README.uk.md": "ще не опубліковано в PyPI",
        "docs/translations/README.zh-CN.md": "目前尚未在 PyPI 发布",
        "website/src/components/HeroSection.tsx": "not published yet",
        "website/src/components/InstallationSection.tsx": "not published yet",
        "website/src/components/BundleRegistrySection.tsx": "not published yet",
    }

    for relative_path, expected_note in expected_notes.items():
        content = (repo_root / relative_path).read_text(encoding="utf-8")
        assert expected_note in content, relative_path


def test_secondary_readme_install_examples_are_labeled_near_commands():
    repo_root = Path(__file__).resolve().parents[3]
    pipx_notes = {
        "README.md": "The `pipx` example below works after the 0.7.0 PyPI publication",
        "docs/translations/README.ja.md": "以下の `pipx` 設定は 0.7.0 の PyPI 公開後に利用できます。",
        "docs/translations/README.ta.md": "கீழே உள்ள `pipx` அமைப்பு 0.7.0 PyPI-இல் வெளியிடப்பட்ட பிறகு கிடைக்கும்.",
        "docs/translations/README.zh-CN.md": "下面的 `pipx` 配置将在 0.7.0 发布到 PyPI 后可用",
    }

    for relative_path, expected_note in pipx_notes.items():
        lines = (repo_root / relative_path).read_text(encoding="utf-8").splitlines()
        spec_lines = [index for index, line in enumerate(lines) if '"--spec"' in line]
        assert spec_lines, relative_path
        for spec_line in spec_lines:
            nearby_text = "\n".join(lines[max(0, spec_line - 12) : spec_line])
            assert expected_note in nearby_text, relative_path

    tamil_lines = (repo_root / "docs/translations/README.ta.md").read_text(encoding="utf-8").splitlines()
    install_lines = [
        index
        for index, line in enumerate(tamil_lines)
        if "python -m pip install codegraphcontext-fork" in line
    ]
    assert len(install_lines) == 2
    secondary_install_context = "\n".join(lines for lines in tamil_lines[install_lines[1] - 6 : install_lines[1]])
    assert "இந்த நிறுவல் கட்டளை" in secondary_install_context
    assert "0.7.0 PyPI-இல் வெளியிடப்பட்ட பிறகு" in secondary_install_context
