"""Runtime path and instance identity overrides for isolated installations."""

import json
import os
from pathlib import Path, PureWindowsPath


DEFAULT_CGC_HOME_NAME = ".codegraphcontext"
DEFAULT_REPO_CONTEXT_DIR = ".codegraphcontext"
DEFAULT_INSTANCE_NAME = "CodeGraphContext"
ISOLATION_ENVIRONMENT_KEYS = (
    "CGC_HOME",
    "CGC_RUNTIME_DB_PATH",
    "CGC_RUNTIME_DB_TYPE",
    "CGC_REPO_CONTEXT_DIR",
    "CGC_INSTANCE_NAME",
    "CGC_IGNORE_PROJECT_ENV",
    "CGC_LOAD_PROJECT_ENV",
)


def get_cgc_home() -> Path:
    """Return the user state root, honoring an optional isolated CGC_HOME."""
    configured = os.environ.get("CGC_HOME", "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured))).resolve()
    return Path.home() / DEFAULT_CGC_HOME_NAME


def get_default_debug_log_path() -> Path:
    """Keep the legacy log path unless a distinct CGC_HOME is selected."""
    default_home = (Path.home() / DEFAULT_CGC_HOME_NAME).resolve()
    cgc_home = get_cgc_home().resolve()
    if os.environ.get("CGC_HOME", "").strip() and cgc_home != default_home:
        return cgc_home / "logs" / "mcp_debug.log"
    return Path.home() / "mcp_debug.log"


def get_repo_context_dir_name() -> str:
    """Return the repository-local state directory name.

    The override must be one path component so a typo cannot redirect context
    operations outside the selected repository.
    """
    configured = os.environ.get("CGC_REPO_CONTEXT_DIR", "").strip()
    if not configured:
        return DEFAULT_REPO_CONTEXT_DIR

    path = Path(configured)
    windows_path = PureWindowsPath(configured)
    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or path.name != configured
        or windows_path.name != configured
        or configured in (".", "..")
    ):
        raise ValueError("CGC_REPO_CONTEXT_DIR must be a single directory name")
    return configured


def get_repo_context_path(repo_root: Path) -> Path:
    """Return the selected repository-local state directory under *repo_root*."""
    return Path(repo_root) / get_repo_context_dir_name()


def get_instance_name() -> str:
    """Return the MCP instance display name, defaulting to the legacy identity."""
    configured = os.environ.get("CGC_INSTANCE_NAME", "").strip()
    return configured or DEFAULT_INSTANCE_NAME


def get_instance_mcp_config(mcp_config: object) -> dict[str, object]:
    """Return this installation's MCP entry from a parsed client config."""
    if not isinstance(mcp_config, dict):
        return {}
    servers = mcp_config.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    server_config = servers.get(get_instance_name())
    return server_config if isinstance(server_config, dict) else {}


def bootstrap_instance_environment(mcp_config_path: Path) -> None:
    """Apply selected MCP runtime settings before modules resolve default paths.

    Shell-provided values win. ``CGC_INSTANCE_NAME`` must be set before startup
    when selecting a non-default entry; the entry's other runtime settings are
    then available before config and database modules are imported.
    """
    try:
        mcp_config = json.loads(Path(mcp_config_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return

    server_env = get_instance_mcp_config(mcp_config).get("env")
    if not isinstance(server_env, dict):
        return

    for key in ISOLATION_ENVIRONMENT_KEYS:
        if os.environ.get(key, "").strip():
            continue
        value = server_env.get(key)
        if not isinstance(value, str) or not value:
            continue
        if key in ("CGC_HOME", "CGC_RUNTIME_DB_PATH"):
            value = str(Path(os.path.expandvars(os.path.expanduser(value))).resolve())
        os.environ[key] = value


def get_isolation_environment() -> dict[str, str]:
    """Return explicitly configured runtime settings for child MCP processes."""
    environment = {}
    for key in ISOLATION_ENVIRONMENT_KEYS:
        value = os.environ.get(key)
        if not value:
            continue
        if key == "CGC_REPO_CONTEXT_DIR":
            value = get_repo_context_dir_name()
        if key in ("CGC_HOME", "CGC_RUNTIME_DB_PATH"):
            value = str(Path(os.path.expandvars(os.path.expanduser(value))).resolve())
        environment[key] = value
    return environment
