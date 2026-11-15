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


def test_mcp_server_info_version_is_separate_from_fork_distribution_metadata():
    from codegraphcontext.mcp_service import MCP_SERVER_VERSION

    project = Path(__file__).resolve().parents[3] / "pyproject.toml"
    project_metadata = project.read_text(encoding="utf-8").split("[project]", 1)[1].split("\n[", 1)[0]
    version_match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', project_metadata)

    assert version_match, "the Python distribution version should come from project metadata"
    distribution_version = version_match.group(1)
    assert distribution_version == "0.7.0"
    assert distribution_version != MCP_SERVER_VERSION


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


def test_translated_readme_docs_badges_describe_repository_docs_links():
    repo_root = Path(__file__).resolve().parents[3]
    translated_readmes = (
        "docs/translations/README.kor.md",
        "docs/translations/README.ru-RU.md",
        "docs/translations/README.uk.md",
    )

    for relative_path in translated_readmes:
        content = (repo_root / relative_path).read_text(encoding="utf-8")
        docs_badge = re.search(
            r'<a href="https://github\.com/JohnXu22786/CodeGraphContext/tree/main/docs/"\s*>'
            r'\s*<img src="https://img\.shields\.io/badge/docs-([^?]+)\?style=flat-square"',
            content,
        )

        assert docs_badge, relative_path
        assert docs_badge.group(1) == "Repository%20Docs-blue", relative_path


def test_architecture_doc_labels_upstream_snapshot():
    repo_root = Path(__file__).resolve().parents[3]
    content = (repo_root / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")

    assert (
        "> **Status:** Historical snapshot of upstream CodeGraphContext 0.4.16, generated 2026-05-12; "
        "it does not describe the full architecture of the fork's 0.7.0 release."
    ) in content


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

        assert "is available on PyPI" in section, relative_path
        assert "not published on PyPI" not in section, relative_path
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


def test_ghcr_docs_reflect_published_public_image():
    repo_root = Path(__file__).resolve().parents[3]
    for relative_path in (
        "README.md",
        "docs/DOCKER.md",
        "CHANGELOG.md",
        "docs/docs/guides/side-by-side-installations.md",
    ):
        content = (repo_root / relative_path).read_text(encoding="utf-8")
        assert "ghcr.io/johnxu22786/codegraphcontext" in content, relative_path
        assert "anonymous pulls" in content, relative_path
        assert "not published yet" not in content, relative_path

    chinese = (repo_root / "docs/translations/README.zh-CN.md").read_text(encoding="utf-8")
    assert "fork 的 GHCR 镜像已发布且为公开软件包；无需登录即可拉取。" in chinese


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


def test_published_package_install_guidance_is_current():
    repo_root = Path(__file__).resolve().parents[3]
    expected_notes = {
        "README.md": "Version 0.7.0 is available on PyPI.",
        ".cursor/skills/codegraphcontext/SKILL.md": "Version 0.7.0 is available on PyPI",
        "docs/docs/getting-started/installation.md": "Version 0.7.0 of `codegraphcontext-fork` is available on PyPI.",
        "docs/docs/getting-started/mcp-setup.md": "Version 0.7.0 of `codegraphcontext-fork` is available on PyPI",
        "docs/CI_INTEGRATION.md": "Version 0.7.0 of `codegraphcontext-fork` is available on PyPI.",
        "src/codegraphcontext/cli/main.py": "Version 0.7.0 is available on PyPI",
        "src/codegraphcontext/utils/tree_sitter_manager.py": "Version 0.7.0 is available on PyPI",
        "docs/BUNDLE_QUICKREF.md": "`codegraphcontext-fork` 0.7.0 is available on PyPI.",
        "docs/docs/guides/bundles.md": "`codegraphcontext-fork` 0.7.0 is available on PyPI.",
        "scripts/create-bundle.sh": "Version 0.7.0 is available on PyPI",
        "scripts/post_install_fix.sh": "pip install codegraphcontext-fork in a dedicated virtual environment",
        "extensions/vscode/src/mcp/client.ts": "Version 0.7.0 is available on PyPI",
        "docs/translations/README.ja.md": "0.7.0 は PyPI で公開されています",
        "docs/translations/README.kor.md": "PyPI에서 제공됩니다",
        "docs/translations/README.ru-RU.md": "уже опубликован в PyPI",
        "docs/translations/README.ta.md": "PyPI-இல் வெளியிடப்பட்டுள்ளது",
        "docs/translations/README.uk.md": "опубліковано на PyPI",
        "docs/translations/README.zh-CN.md": "`codegraphcontext-fork` 0.7.0 已在 PyPI 发布。",
        "website/src/components/HeroSection.tsx": "Version 0.7.0 is available on PyPI",
        "website/src/components/InstallationSection.tsx": "Version 0.7.0 is available on PyPI",
        "website/src/components/BundleRegistrySection.tsx": "Version 0.7.0 is available on PyPI",
    }

    for relative_path, expected_note in expected_notes.items():
        content = (repo_root / relative_path).read_text(encoding="utf-8")
        assert expected_note in content, relative_path
        assert "not published on PyPI yet" not in content, relative_path
        assert "after the 0.7.0 PyPI publication" not in content, relative_path


def test_secondary_readme_install_examples_are_labeled_near_commands():
    repo_root = Path(__file__).resolve().parents[3]
    pipx_notes = {
        "README.md": "Version 0.7.0 is available on PyPI",
        "docs/translations/README.ja.md": "PyPI で公開されている 0.7.0 の `pipx` 設定を利用できます。",
        "docs/translations/README.ta.md": "PyPI-இல் வெளியிடப்பட்ட 0.7.0-க்கான கீழே உள்ள `pipx` அமைப்பைப் பயன்படுத்தலாம்.",
        "docs/translations/README.zh-CN.md": "PyPI 已发布的 0.7.0 可使用下面的 `pipx` 配置",
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
    assert "0.7.0 PyPI-இல் வெளியிடப்பட்டுள்ளது" in secondary_install_context
