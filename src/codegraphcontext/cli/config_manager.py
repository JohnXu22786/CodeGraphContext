# src/codegraphcontext/cli/config_manager.py
"""
Configuration management for CodeGraphContext.
Handles reading, writing, and validating configuration settings.
Also manages the context system (config.yaml) alongside the existing .env file.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any, List
try:
    from rich.console import Console
    from rich.table import Table
    console = Console()
except Exception:
    # Lightweight fallback when 'rich' is not installed (tests or minimal envs)
    class _TableFallback:
        def __init__(self, show_header=True, header_style=None):
            self._cols = []
            self._rows = []

        def add_column(self, name, **_kwargs):
            self._cols.append(name)

        def add_row(self, *cells):
            self._rows.append([str(c) for c in cells])

        def __str__(self) -> str:
            # Simple text table rendering
            out = []
            if self._cols:
                out.append(" | ".join(self._cols))
                out.append("-" * max(10, len(out[0])))
            for r in self._rows:
                out.append(" | ".join(r))
            return "\n".join(out)

    class _ConsoleFallback:
        def print(self, *args, **kwargs):
            # Mimic rich.console.Console.print by delegating to built-in print
            end = kwargs.get("end", "\n")
            sep = kwargs.get("sep", " ")
            for a in args:
                if isinstance(a, _TableFallback):
                    built = str(a)
                    print(built, end=end)
                else:
                    print(a, end=end)

    Table = _TableFallback
    console = _ConsoleFallback()
import os
import yaml
from codegraphcontext.runtime_paths import (
    get_cgc_home,
    get_default_debug_log_path,
    get_repo_context_path,
)


def _atomic_write_text(path: Path, content: str, *, secure: bool = False) -> None:
    """Write *content* to *path* atomically (temp file + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    if secure:
        os.chmod(path, 0o600)

# Configuration file location. CGC_HOME lets another installation keep its
# user configuration, logs, named contexts, bundles, and default databases apart.
CONFIG_DIR = get_cgc_home()
CONFIG_FILE = CONFIG_DIR / ".env"

# Values copied from a project dotenv into the process environment must be
# distinguishable from shell/global settings when restoring the global context.
_PROJECT_ENVIRONMENT_VALUES: Dict[str, str] = {}
_GLOBAL_ENVIRONMENT_VALUES: Dict[str, str] = {}


def _record_project_environment_values(
    values: Dict[str, str], runtime_env: Dict[str, str]
) -> None:
    """Track project dotenv values that were promoted into ``os.environ``."""
    for key, previous_value in list(_PROJECT_ENVIRONMENT_VALUES.items()):
        if os.environ.get(key) != previous_value:
            _PROJECT_ENVIRONMENT_VALUES.pop(key, None)

    for key, value in values.items():
        if key not in runtime_env and os.environ.get(key) == value:
            _PROJECT_ENVIRONMENT_VALUES[key] = value


def _record_global_environment_values(
    values: Dict[str, str], runtime_env: Dict[str, str]
) -> None:
    """Retain global dotenv values hidden by project overrides in the process."""
    _GLOBAL_ENVIRONMENT_VALUES.clear()
    for key, value in values.items():
        previous_project_value = _PROJECT_ENVIRONMENT_VALUES.get(key)
        if (
            key not in runtime_env
            or runtime_env.get(key) == previous_project_value
        ):
            _GLOBAL_ENVIRONMENT_VALUES[key] = value


@contextmanager
def _global_context_environment():
    """Temporarily expose global settings without project dotenv overrides."""
    project_values = {
        key: value
        for key, value in _PROJECT_ENVIRONMENT_VALUES.items()
        if os.environ.get(key) == value
    }
    missing = object()
    original_values: Dict[str, object] = {}

    def set_temporary(key: str, value: str) -> None:
        if key not in original_values:
            original_values[key] = os.environ.get(key, missing)
        os.environ[key] = value

    def remove_temporary(key: str) -> None:
        if key not in original_values:
            original_values[key] = os.environ.get(key, missing)
        os.environ.pop(key, None)

    for key in project_values:
        remove_temporary(key)
    for key, value in _GLOBAL_ENVIRONMENT_VALUES.items():
        if key in project_values or key not in os.environ:
            set_temporary(key, value)
    try:
        yield
    finally:
        for key, value in original_values.items():
            if value is missing:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)

# Keys that pin embedded DB directories; must not bleed across profiles via local .env
DB_PATH_ENV_KEYS = frozenset({
    "FALKORDB_PATH", "FALKORDB_SOCKET_PATH", "KUZUDB_PATH", "LADYBUGDB_PATH",
})

# Database credential keys (stored in same .env file but not managed as config)
DATABASE_CREDENTIAL_KEYS = {
    "NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE",
    "NORNIC_URI", "NORNIC_USERNAME", "NORNIC_PASSWORD", "NORNIC_DATABASE",
    "FALKORDB_HOST", "FALKORDB_PORT", "FALKORDB_PASSWORD", "FALKORDB_SSL",
    "FALKORDB_GRAPH_NAME",
}

# Default configuration values
DEFAULT_CONFIG = {
    "DEFAULT_DATABASE": "ladybugdb",
    "FALKORDB_PATH": str(CONFIG_DIR / "global" / "db" / "falkordb"),
    "FALKORDB_SOCKET_PATH": str(CONFIG_DIR / "global" / "db" / "falkordb.sock"),
    # Empty selects a deterministic port derived from each embedded DB socket.
    "FALKORDB_PORT": "",
    "LADYBUGDB_PATH": str(CONFIG_DIR / "global" / "db" / "ladybugdb"),
    "KUZUDB_PATH": str(CONFIG_DIR / "global" / "db" / "kuzudb"),
    "INDEX_VARIABLES": "true",
    "ALLOW_DB_DELETION": "false",
    "DEBUG_LOGS": "false",
    "DEBUG_LOG_PATH": str(get_default_debug_log_path()),
    "ENABLE_APP_LOGS": "CRITICAL",
    "LIBRARY_LOG_LEVEL": "WARNING",
    "LOG_FILE_PATH": str(CONFIG_DIR / "logs" / "cgc.log"),
    "MAX_FILE_SIZE_MB": "10",
    "IGNORE_TEST_FILES": "false",
    "IGNORE_HIDDEN_FILES": "true",
    "ENABLE_AUTO_WATCH": "false",
    "COMPLEXITY_THRESHOLD": "10",
    "MAX_DEPTH": "unlimited",
    "PARALLEL_WORKERS": "4",
    "CACHE_ENABLED": "true",
    "IGNORE_DIRS": "node_modules,venv,.venv,env,.env,dist,build,target,out,.git,.codegraphcontext,.idea,.vscode,__pycache__",
    "INDEX_SOURCE": "true",
    # SCIP indexer feature flag (default off — existing Tree-sitter behaviour unchanged)
    "SCIP_INDEXER": "false",
    "SCIP_LANGUAGES": "python,typescript,javascript,go,rust,java,dart,cpp,c,csharp",
    "SCIP_LOCAL_INDEXER_TIMEOUT_SECONDS": "300",
    "SKIP_EXTERNAL_RESOLUTION": "false",
    # 0 = unlimited; any positive integer caps MCP tool response size.
    "MAX_TOOL_RESPONSE_TOKENS": "0",
    # 0 = unlimited; any positive integer caps response size in raw characters.
    # When both MAX_TOOL_RESPONSE_TOKENS and MAX_PROMPT_CHARS are set, the
    # stricter (smaller) effective character budget wins.
    "MAX_PROMPT_CHARS": "0",
    # JSON object mapping tool names to integer result-count limits.
    # Example: {"find_code": 20, "analyze_code_relationships": 10, "find_dead_code": 30}
    "TOOL_RESULT_LIMITS": "{}",
    # Post-indexing resolution phases (default off)
    "ENABLE_INHERIT_RESOLVE": "false",
    "ENABLE_VECTOR_RESOLVE": "false",
    "CGC_EMBEDDING_MODEL": "local",
    "CGC_EMBEDDING_BATCH_SIZE": "256",
    # Default fuzzy matching behavior for `cgc find name` (overridable per-command with --fuzzy/--no-fuzzy)
    "FUZZY_SEARCH": "true",
    "REDACT_SECRETS": "false",
    # Default LLM model names used for graph queries when no value is explicitly configured
    "OPENAI_MODEL": "gpt-4o",
    "ANTHROPIC_MODEL": "claude-3-5-sonnet-20241022",
    # Optional API key for the HTTP API gateway. Empty = auth disabled
    # (backward compatible). When set, HTTP endpoints require the key via
    # `Authorization: Bearer <key>` or `X-API-Key`. May also be set via the
    # CGC_API_KEY environment variable (which takes priority).
    "CGC_API_KEY": "",
}

# Configuration key descriptions
CONFIG_DESCRIPTIONS = {
    "DEFAULT_DATABASE": "Default database backend (neo4j|falkordb|falkordb-remote|kuzudb|nornic|ladybugdb)",
    "FALKORDB_PATH": "Path to FalkorDB database file",
    "FALKORDB_SOCKET_PATH": "Path to FalkorDB Unix socket",
    "FALKORDB_PORT": "Optional FalkorDB port override (empty = per-database port)",
    "LADYBUGDB_PATH": "Path to LadybugDB database directory",
    "KUZUDB_PATH": "Path to KuzuDB database directory",
    "INDEX_VARIABLES": "Index variable nodes in the graph (lighter graph if false)",
    "ALLOW_DB_DELETION": "Allow full database deletion commands",
    "DEBUG_LOGS": "Enable debug logging (for development/troubleshooting)",
    "DEBUG_LOG_PATH": "Path to debug log file",
    "ENABLE_APP_LOGS": "Application log level (DEBUG|INFO|WARNING|ERROR|CRITICAL|DISABLED)",
    "LIBRARY_LOG_LEVEL": "Log level for third-party libraries (neo4j, asyncio, urllib3) (DEBUG|INFO|WARNING|ERROR|CRITICAL)",
    "LOG_FILE_PATH": "Path to application log file",
    "MAX_FILE_SIZE_MB": "Maximum file size to index (in MB)",
    "IGNORE_TEST_FILES": "Skip test files during indexing",
    "IGNORE_HIDDEN_FILES": "Skip hidden files/directories",
    "ENABLE_AUTO_WATCH": "Automatically watch directory after indexing",
    "COMPLEXITY_THRESHOLD": "Cyclomatic complexity warning threshold",
    "MAX_DEPTH": "Maximum directory depth for indexing (unlimited or number)",
    "PARALLEL_WORKERS": "Parallel parse workers for indexing and the file watcher (clamped to 32)",
    "CACHE_ENABLED": "Reserved compatibility setting; no file-hash or parsed-file cache currently reads this value",
    "IGNORE_DIRS": "Comma-separated list of directory names to ignore during indexing",
    "INDEX_SOURCE": "Store full source code in graph database (for faster indexing use false, for better performance use true)",
    "SCIP_INDEXER": "Use SCIP-based indexing for higher accuracy call/inheritance resolution (requires scip-<lang> tools installed)",
    "SCIP_LANGUAGES": "Comma-separated languages to index via SCIP when SCIP_INDEXER=true (python,typescript,javascript,go,rust,java,dart,cpp,c,csharp)",
    "SCIP_LOCAL_INDEXER_TIMEOUT_SECONDS": "Timeout in seconds for the local SCIP indexer subprocess (default 300). Raise it for large repositories whose indexer runs longer than 5 minutes; a value <= 0 or non-numeric falls back to 300.",
    "SKIP_EXTERNAL_RESOLUTION": "Skip resolution attempts for external library method calls (recommended for enterprise large Java/Spring codebases)",
    "MAX_TOOL_RESPONSE_TOKENS": "Maximum tokens per MCP tool response (0 = unlimited). Truncates oversized payloads and appends a notice.",
    "MAX_PROMPT_CHARS": "Maximum characters per MCP tool response (0 = unlimited). When set alongside MAX_TOOL_RESPONSE_TOKENS the stricter limit wins. Truncated payloads receive a visible [CGC] notice and a stdout warning is emitted.",
    "TOOL_RESULT_LIMITS": "JSON object mapping tool names to max result counts, e.g. {\"find_code\": 20, \"analyze_code_relationships\": 10}. Missing keys use built-in defaults.",
    # Post-indexing resolution phases
    "ENABLE_INHERIT_RESOLVE": (
        "[Phase 5] Re-resolve ambiguous same-file CALLS edges using the inheritance graph (INHERITS relationships). "
        "When enabled, methods called on an interface or abstract class are re-pointed to the correct concrete "
        "implementation based on the class hierarchy, reducing tier-7 fallback edges. "
        "WHEN TO ENABLE: any Java/Kotlin/C# codebase that uses inheritance or interface-based DI (e.g. Spring, OSGi). "
        "PREREQUISITES: run 'cgc index' first so INHERITS edges exist in the graph. No extra tools needed. "
        "COST: adds ~1-5 min per 50K functions at the end of each 'cgc index' run. Safe to toggle on/off — only adds new edges, never removes existing ones."
    ),
    "ENABLE_VECTOR_RESOLVE": (
        "[Phase 4 + Phase 5 tiebreaker] Generate semantic embeddings for all Function nodes and use vector "
        "similarity as a tiebreaker when inheritance resolution alone cannot distinguish between multiple candidates. "
        "Phase 4 writes a 384-dim embedding to every Function node; Phase 5 queries those embeddings during re-resolution. "
        "WHEN TO ENABLE: large codebases (>10K functions) where inheritance alone leaves many ambiguous calls "
        "(tier-7 fallbacks still high after ENABLE_INHERIT_RESOLVE). Also useful for cross-language repos. "
        "PREREQUISITES: (1) fastembed must be installed — run 'pip install fastembed'. "
        "(2) Neo4j must be the active database (vector index not supported on FalkorDB/KuzuDB). "
        "(3) ENABLE_INHERIT_RESOLVE should also be true — vector is a tiebreaker for Phase 5, not a replacement. "
        "COST: Phase 4 takes ~15 min per 50K functions on CPU (first run only; incremental updates are fast). "
        "Embedding model (~40 MB) is downloaded automatically on first use from HuggingFace."
    ),
    "CGC_EMBEDDING_MODEL": (
        "Embedding backend for ENABLE_VECTOR_RESOLVE. "
        "'local' uses fastembed (BAAI/bge-small-en-v1.5, 384-dim, runs on CPU, no GPU or API key needed). "
        "'openai' uses OpenAI text-embedding-3-small (requires OPENAI_API_KEY env var, costs money per token). "
        "Default: local"
    ),
    "CGC_EMBEDDING_BATCH_SIZE": (
        "Number of function texts to embed per batch when ENABLE_VECTOR_RESOLVE=true. "
        "Larger values are faster but use more RAM. Default: 256. Reduce to 64 if you hit memory errors."
    ),
    "FUZZY_SEARCH": (
        "Enable fuzzy matching by default for `cgc find name` (true|false). "
        "Per-invocation overrides are available via --fuzzy / --no-fuzzy."
    ),
    "REDACT_SECRETS": (
        "Scan string literals and variable values for likely secrets (API keys, tokens, "
        "passwords, connection strings) during indexing and replace them with [REDACTED] "
        "before writing to the graph database. When false (default), secrets are still "
        "detected and a warning is logged, but values are stored verbatim."
    ),
    "OPENAI_MODEL": (
        "Default OpenAI model used for graph queries. "
        "Requires OPENAI_API_KEY environment variable. "
        "Default: gpt-4o"
    ),
    "ANTHROPIC_MODEL": (
        "Default Anthropic model used for graph queries. "
        "Requires ANTHROPIC_API_KEY environment variable. "
        "Default: claude-3-5-sonnet-20241022"
    ),
    "CGC_API_KEY": (
        "Optional API key protecting the HTTP API gateway. Empty = "
        "unauthenticated (backward compatible). When set, HTTP endpoints "
        "require it via `Authorization: Bearer <key>` or the `X-API-Key` "
        "header. The CGC_API_KEY environment variable overrides this value."
    ),
}

# Valid values for each config key
CONFIG_VALIDATORS = {
    "DEFAULT_DATABASE": ["neo4j", "falkordb", "falkordb-remote", "kuzudb", "nornic", "ladybugdb"],
    "INDEX_VARIABLES": ["true", "false"],
    "ALLOW_DB_DELETION": ["true", "false"],
    "DEBUG_LOGS": ["true", "false"],
    "ENABLE_APP_LOGS": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "DISABLED"],
    "LIBRARY_LOG_LEVEL": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    "IGNORE_TEST_FILES": ["true", "false"],
    "IGNORE_HIDDEN_FILES": ["true", "false"],
    "ENABLE_AUTO_WATCH": ["true", "false"],
    "CACHE_ENABLED": ["true", "false"],
    "INDEX_SOURCE": ["true", "false"],
    "SCIP_INDEXER": ["true", "false"],
    "SKIP_EXTERNAL_RESOLUTION": ["true", "false"],
    "ENABLE_INHERIT_RESOLVE": ["true", "false"],
    "ENABLE_VECTOR_RESOLVE": ["true", "false"],
    "CGC_EMBEDDING_MODEL": ["local", "openai"],
    "FUZZY_SEARCH": ["true", "false"],
    "REDACT_SECRETS": ["true", "false"],
}

SUPPORTED_DATABASES: List[str] = CONFIG_VALIDATORS["DEFAULT_DATABASE"]
DATABASE_CLI_HELP = (
    "Database backend ("
    + "|".join(SUPPORTED_DATABASES)
    + "). Defaults to DEFAULT_DATABASE from config."
)

DEFAULT_CGCIGNORE_PATTERNS = """\
# Default .cgcignore patterns
# Lines starting with # are comments; blank lines are ignored.
# Patterns follow .gitignore syntax.

node_modules/
venv/
.venv/
env/
.env/
dist/
build/
target/
out/
obj/
.terraform/
.terragrunt-cache/
*.lock.hcl
.git/
.codegraphcontext/
.idea/
.vscode/
__pycache__/
*.pyc
*.pyo
*.egg-info/
.tox/
.mypy_cache/
.pytest_cache/
coverage/
.next/
"""


def resolve_model_name(provider: str, configured_value: Optional[str] = None) -> str:
    """Get the model it should use for an LLM provider.

    If the user configured a model, it uses that. Otherwise, it falls back to the default
    model for the provider (like OPENAI_MODEL or ANTHROPIC_MODEL).

    Args:
        provider: The name of the provider, like "openai" or "anthropic" (case-insensitive).
        configured_value: The model name from the user's config, if they set one.

    Returns:
        The model name it should use, or an empty string if it doesn't know the provider.
    """
    if configured_value and configured_value.strip():
        return configured_value.strip()
    key = f"{provider.upper()}_MODEL"
    return DEFAULT_CONFIG.get(key, "")


def normalize_config_path(value: str, *, absolute: bool = False, base_dir: Optional[Path] = None) -> str:
    """Normalize config path values.

    - Expands ``~`` and environment variables.
    - Optionally resolves to an absolute path.
    """
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    path_obj = Path(expanded)
    if absolute and not path_obj.is_absolute():
        path_obj = (base_dir or Path.cwd()) / path_obj
    if absolute:
        return str(path_obj.resolve())
    return str(path_obj)


def ensure_config_dir(path: Optional[Path] = None):
    """
    Ensure that the configuration directory exists.
    Creates the directory and a logs subdirectory if they do not already exist.
    """
    path = path or CONFIG_DIR
    path.mkdir(parents=True, exist_ok=True)
    (path / "logs").mkdir(parents=True, exist_ok=True)


def ensure_global_cgcignore() -> bool:
    """Create the global context's ``.cgcignore`` with sensible defaults
    if it does not already exist.  Returns True when a new file was created."""
    cgcignore_path = CONFIG_DIR / "global" / ".cgcignore"
    if cgcignore_path.exists():
        return False
    cgcignore_path.parent.mkdir(parents=True, exist_ok=True)
    cgcignore_path.write_text(DEFAULT_CGCIGNORE_PATTERNS)
    return True



def load_config(*, read_only: bool = False) -> Dict[str, str]:
    """
    Load configuration with priority support.
    Priority order (highest to lowest):
    1. Environment variables (always highest priority)
    2. Local .env file (ONLY in per-repo mode or when CGC_LOAD_PROJECT_ENV=1)
    3. Global CGC_HOME/.env (default: ~/.codegraphcontext/.env)
    
    BUG FIX: In global/named context mode, local repo .env files are now properly ignored
    to prevent silent database redirection when working inside cloned repositories.
    
    Note: Does NOT create config directory - caller must call ensure_config_dir() first if needed.
    """
    # Start with defaults
    config = DEFAULT_CONFIG.copy()
    
    # Load global config
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        config[key.strip()] = value.strip()
        except Exception as e:
            console.print(f"[red]Error loading global config: {e}[/red]")
    
    # Load local .env file ONLY if should_apply_project_dotenv() returns True
    # This respects context mode (per-repo vs global/named) and environment overrides
    local_env = find_local_env(read_only=True) if read_only else find_local_env()
    if local_env and local_env.exists():
        try:
            with open(local_env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip()
                        
                        # In per-repo mode: allow all config keys to be overridden
                        # In global/named mode: local .env should not be loaded at all (find_local_env returns None)
                        # But if it somehow gets through, only allow non-DB-path keys for safety
                        if key in DB_PATH_ENV_KEYS:
                            # CRITICAL: Never let local .env override DB paths in global mode
                            # This prevents silent database redirection
                            continue
                        
                        if key in DEFAULT_CONFIG or key in DATABASE_CREDENTIAL_KEYS:
                            config[key] = value
        except Exception as e:
            console.print(f"[yellow]Warning: Error loading local .env: {e}[/yellow]")
    
    # Environment variables have highest priority - always override everything
    for key in DEFAULT_CONFIG.keys():
        env_value = os.getenv(key)
        if env_value is not None:
            config[key] = env_value
    
    # Also check for database credential environment variables
    for key in DATABASE_CREDENTIAL_KEYS:
        env_value = os.getenv(key)
        if env_value is not None:
            config[key] = env_value
    
    return config


def _configured_default_database(*, read_only: bool = True) -> str:
    """Return the configured default backend, falling back to LadybugDB."""
    return load_config(read_only=read_only).get("DEFAULT_DATABASE") or "ladybugdb"


def _has_explicit_database_default(
    *,
    read_only: bool = True,
    include_runtime: bool = True,
    include_project_env: bool = True,
) -> bool:
    """Whether the active config explicitly selected ``DEFAULT_DATABASE``."""
    if os.getenv("DEFAULT_DATABASE") or (
        include_runtime and os.getenv("CGC_RUNTIME_DB_TYPE")
    ):
        return True

    sources = [CONFIG_FILE]
    if include_project_env:
        local_env = find_local_env(read_only=read_only)
        if local_env is not None:
            sources.append(local_env)

    for source in sources:
        try:
            with open(source, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    if key.strip() == "DEFAULT_DATABASE" and value.strip():
                        return True
        except OSError:
            continue
    return False


def _global_default_database(configured_default: str, *, read_only: bool = True) -> str:
    """Resolve explicit, remote, or legacy global backends in precedence order."""
    runtime_backend = os.getenv("CGC_RUNTIME_DB_TYPE")
    if runtime_backend:
        return runtime_backend
    if _has_explicit_database_default(read_only=read_only):
        return configured_default or "ladybugdb"

    config = load_config(read_only=read_only)
    if config.get("FALKORDB_HOST") or os.getenv("FALKORDB_HOST"):
        return "falkordb-remote"

    configured_falkordb_path = config.get("FALKORDB_PATH")
    if configured_falkordb_path:
        resolved_path = Path(
            normalize_config_path(configured_falkordb_path, absolute=True)
        )
        try:
            resolved_path.relative_to(CONFIG_DIR.resolve())
        except ValueError:
            pass
        else:
            if resolved_path.exists():
                return "falkordb"

    global_db_dir = CONFIG_DIR / "global" / "db"
    # FalkorDB was the earlier global default; prefer it if both historical
    # paths exist because there is no stored setting that can distinguish them.
    for backend in ("falkordb", "kuzudb"):
        if (global_db_dir / backend).exists() or (global_db_dir / f"{backend}.db").exists():
            return backend
    if _LEGACY_FALKORDB_PATH.exists():
        return "falkordb"
    return configured_default or "ladybugdb"


def should_apply_project_dotenv(*, read_only: bool = False) -> bool:
    """True when the selected repository context's ``.env`` should merge globally.

    Project env is loaded only in **per-repo** context mode (or when
    ``CGC_LOAD_PROJECT_ENV=1``). In **global** / **named** mode, ``CGC_HOME/.env``
    wins so clones with a checked-in repository-context ``.env`` do not hijack config.

    Set ``CGC_IGNORE_PROJECT_ENV=1`` to force skip; ``CGC_LOAD_PROJECT_ENV=1`` to force load.
    """
    if os.getenv("CGC_IGNORE_PROJECT_ENV", "").strip().lower() in ("1", "true", "yes"):
        return False
    if os.getenv("CGC_LOAD_PROJECT_ENV", "").strip().lower() in ("1", "true", "yes"):
        return True
    cfg = (
        load_context_config(create_if_missing=False)
        if read_only
        else load_context_config()
    )
    if cfg.mode != "per-repo":
        return False
    try:
        Path.cwd().resolve().relative_to(Path.home().resolve())
        return True
    except ValueError:
        # Per-repo indexing from /tmp with an isolated HOME (common in E2E/CI).
        return True


def find_local_env(*, read_only: bool = False) -> Optional[Path]:
    """Find project configuration in the current directory only.

    Prefer the selected repository-context ``.env`` to ``.env``. Parent directories are
    deliberately excluded, just as they are for local context resolution:
    starting CGC in one project must not import another project's settings.
    """
    applies_project_dotenv = (
        should_apply_project_dotenv(read_only=True)
        if read_only
        else should_apply_project_dotenv()
    )
    if not applies_project_dotenv:
        return None

    current = Path.cwd()
    for candidate in (get_repo_context_path(current) / ".env", current / ".env"):
        if candidate.is_file() and candidate != CONFIG_FILE:
            return candidate
    return None


def codegraphcontext_dotenv_at_cwd(cwd: Optional[Path] = None) -> Optional[Path]:
    """
    Return the selected repository-context ``.env`` if it exists, else None.

    *cwd* defaults to ``Path.cwd()``. Parent directories are **not** searched—same rule as
    local context resolution (``find_local_cgc_dir``).
    """
    root = (cwd or Path.cwd()).resolve()
    candidate = get_repo_context_path(root) / ".env"
    return candidate if candidate.exists() else None


def save_config(
    config: Dict[str, str],
    preserve_db_credentials: bool = True,
    *,
    persist_default_database: Optional[bool] = None,
):
    """
    Save configuration to file.
    If preserve_db_credentials is True, existing database credentials will be preserved.
    If preserve_db_credentials is False, credentials from config dict will be written.
    """
    ensure_config_dir()
    config_to_write = config.copy()
    if "DEFAULT_DATABASE" in config_to_write:
        if persist_default_database is False:
            config_to_write.pop("DEFAULT_DATABASE")
        elif (
            persist_default_database is None
            and config_to_write["DEFAULT_DATABASE"]
            == DEFAULT_CONFIG["DEFAULT_DATABASE"]
            and not _has_explicit_database_default(
                include_runtime=False, include_project_env=False
            )
        ):
            # Keep LadybugDB as the in-memory default without pinning it into
            # a generated .env. An explicit `cgc config set DEFAULT_DATABASE`
            # still persists the selection.
            config_to_write.pop("DEFAULT_DATABASE")
    
    # Determine which credentials to write
    credentials_to_write = {}
    
    if preserve_db_credentials and CONFIG_FILE.exists():
        # Load existing credentials from file to preserve them
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        if key in DATABASE_CREDENTIAL_KEYS:
                            credentials_to_write[key] = value.strip()
        except Exception:
            pass
        # Merge credentials from the config dict (handles both new and updated values)
        for key in DATABASE_CREDENTIAL_KEYS:
            if key in config:
                credentials_to_write[key] = config[key]
    else:
        # Use credentials from the config dict being passed in
        for key in DATABASE_CREDENTIAL_KEYS:
            if key in config:
                credentials_to_write[key] = config[key]
    
    try:
        lines = [
            "# CodeGraphContext Configuration",
            f"# Location: {CONFIG_FILE}",
            "",
        ]
        if credentials_to_write:
            lines.append("# ===== Database Credentials =====")
            for key in sorted(DATABASE_CREDENTIAL_KEYS):
                if key in credentials_to_write:
                    lines.append(f"{key}={credentials_to_write[key]}")
            lines.append("")
        lines.append("# ===== Configuration Settings =====")
        for key, value in sorted(config_to_write.items()):
            if key in DATABASE_CREDENTIAL_KEYS:
                continue
            description = CONFIG_DESCRIPTIONS.get(key, "")
            if description:
                lines.append(f"# {description}")
            lines.append(f"{key}={value}")
            lines.append("")
        _atomic_write_text(CONFIG_FILE, "\n".join(lines), secure=True)
        console.print(f"[green]✅ Configuration saved to {CONFIG_FILE}[/green]")
    except Exception as e:
        console.print(f"[red]Error saving config: {e}[/red]")


def validate_config_value(key: str, value: str) -> tuple[bool, Optional[str]]:
    """
    Validate a configuration value.
    Returns (is_valid, error_message)
    """
    # Skip validation for database credentials (they have their own validation elsewhere)
    if key in DATABASE_CREDENTIAL_KEYS:
        return True, None
    
    # Strip quotes that might be in the value
    value = value.strip().strip("'\"")
    
    # Check if key exists
    if key not in DEFAULT_CONFIG:
        available_keys = ", ".join(sorted(DEFAULT_CONFIG.keys()))
        return False, f"Unknown config key: {key}. Available keys: {available_keys}"
    
    # Validate against specific validators if they exist
    if key in CONFIG_VALIDATORS:
        valid_values = CONFIG_VALIDATORS[key]
        if value.lower() not in [v.lower() for v in valid_values]:
            return False, f"Invalid value for {key}. Must be one of: {', '.join(valid_values)}"
    
    # Specific validation for numeric values
    if key == "MAX_FILE_SIZE_MB":
        try:
            size = int(value)
            if size <= 0:
                return False, "MAX_FILE_SIZE_MB must be a positive number"
        except ValueError:
            return False, "MAX_FILE_SIZE_MB must be a number"
    
    if key == "COMPLEXITY_THRESHOLD":
        try:
            threshold = int(value)
            if threshold <= 0:
                return False, "COMPLEXITY_THRESHOLD must be a positive number"
        except ValueError:
            return False, "COMPLEXITY_THRESHOLD must be a number"
    
    if key == "PARALLEL_WORKERS":
        try:
            workers = int(value)
            if workers <= 0 or workers > 32:
                return False, "PARALLEL_WORKERS must be between 1 and 32"
        except ValueError:
            return False, "PARALLEL_WORKERS must be a number"

    if key == "MAX_TOOL_RESPONSE_TOKENS":
        try:
            limit = int(value)
            if limit < 0:
                return False, "MAX_TOOL_RESPONSE_TOKENS must be 0 (unlimited) or a positive integer"
        except ValueError:
            return False, "MAX_TOOL_RESPONSE_TOKENS must be an integer (0 = unlimited)"

    if key == "MAX_PROMPT_CHARS":
        try:
            limit = int(value)
            if limit < 0:
                return False, "MAX_PROMPT_CHARS must be 0 (unlimited) or a positive integer"
        except ValueError:
            return False, "MAX_PROMPT_CHARS must be an integer (0 = unlimited)"

    if key == "TOOL_RESULT_LIMITS":
        import json as _json
        try:
            parsed = _json.loads(value)
            if not isinstance(parsed, dict):
                return False, "TOOL_RESULT_LIMITS must be a JSON object, e.g. {\"find_code\": 20}"
            for k, v in parsed.items():
                if not isinstance(v, int) or v < 1:
                    return False, f"TOOL_RESULT_LIMITS: value for '{k}' must be a positive integer"
        except _json.JSONDecodeError:
            return False, "TOOL_RESULT_LIMITS must be valid JSON, e.g. {\"find_code\": 20, \"find_dead_code\": 30}"
    
    if key == "MAX_DEPTH":
        if value.lower() != "unlimited":
            try:
                depth = int(value)
                if depth <= 0:
                    return False, "MAX_DEPTH must be 'unlimited' or a positive number"
            except ValueError:
                return False, "MAX_DEPTH must be 'unlimited' or a number"
    
    if key in ("LOG_FILE_PATH", "DEBUG_LOG_PATH"):
        # Validate path is writable
        log_path = Path(normalize_config_path(value, absolute=True))
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return False, f"Cannot create log directory: {e}"
    
    if key in ("FALKORDB_PATH", "FALKORDB_SOCKET_PATH", "LADYBUGDB_PATH", "KUZUDB_PATH"):
        # Validate path is writable
        db_path = Path(normalize_config_path(value, absolute=True))
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return False, f"Cannot create directory for {key}: {e}"
        
        # Check if parent directory is writable
        if not os.access(db_path.parent, os.W_OK):
            return False, f"Directory {db_path.parent} is not writable"
    
    return True, None


def get_config_value(key: str, *, read_only: bool = False) -> Optional[str]:
    """Get a specific configuration value."""
    config = load_config(read_only=read_only)
    return config.get(key)


def is_db_deletion_allowed() -> bool:
    """True when destructive delete/clear operations are permitted."""
    return str(get_config_value("ALLOW_DB_DELETION") or "false").strip().lower() == "true"


def set_config_value(key: str, value: str) -> bool:
    """Set a configuration value. Returns True if successful.

    The special key ``mode`` is delegated to :func:`set_context_mode` so
    that ``cgc config set mode named`` works as expected.
    """
    if key.lower() == "mode":
        return set_context_mode(value.lower())

    # Ensure config directory exists
    ensure_config_dir()
    
    # Validate
    is_valid, error_msg = validate_config_value(key, value)
    if not is_valid:
        console.print(f"[red]❌ {error_msg}[/red]")
        return False
    
    # Load, update, and save
    config = load_config()
    config[key] = value
    save_config(
        config,
        persist_default_database=True if key == "DEFAULT_DATABASE" else None,
    )
    
    console.print(f"[green]✅ Set {key} = {value}[/green]")

    # Surface a missing embedding backend at the moment of intent, not after
    # an index run that silently generated nothing (#1597).
    if key == "ENABLE_VECTOR_RESOLVE" and value.lower() == "true":
        try:
            from codegraphcontext.tools.indexing.embeddings import probe_embedding_backend
            ok, detail = probe_embedding_backend()
            if not ok:
                console.print(
                    f"[bold yellow]⚠ Vector resolve is enabled but cannot run yet: "
                    f"{detail}. Indexing will proceed without embeddings until "
                    f"this is installed.[/bold yellow]"
                )
        except Exception:
            pass
    return True


def reset_config():
    """Reset configuration to defaults (preserves database credentials)."""
    import shutil
    from datetime import datetime

    ensure_config_dir()
    if CONFIG_FILE.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.{stamp}.bak")
        shutil.copy2(CONFIG_FILE, backup)
        console.print(f"[dim]Backed up current config to {backup}[/dim]")
    save_config(
        DEFAULT_CONFIG.copy(),
        preserve_db_credentials=True,
        persist_default_database=False,
    )
    console.print("[green]✅ Configuration reset to defaults[/green]")
    console.print("[cyan]Note: Database credentials were preserved[/cyan]")



_FIRST_RUN_MARKER = CONFIG_DIR / ".first_run_done"


def _print_welcome_banner() -> None:
    """Print a one-time welcome message explaining the context system."""
    console.print()
    console.print("[bold green]Welcome to CodeGraphContext![/bold green]")
    console.print()
    console.print("CGC organises your code graphs using [bold]contexts[/bold]:")
    console.print("  [cyan]global[/cyan]    - One shared graph for all projects (default)")
    console.print(
        f"  [cyan]per-repo[/cyan]  - Each repo gets its own {get_repo_context_path(Path('.')).name}/ folder"
    )
    console.print("  [cyan]named[/cyan]     - Create named workspaces (e.g. cgc index . --context MyProject)")
    console.print()
    console.print("Switch modes anytime:  [dim]cgc context mode <global|per-repo|named>[/dim]")
    console.print("Or:                    [dim]cgc config set mode <global|per-repo|named>[/dim]")
    console.print()


def ensure_first_run_bootstrap(show_welcome: bool = False) -> bool:
    """Run one-time setup for brand-new installs.

    Creates default config files and the global .cgcignore silently.
    Returns True when bootstrap was performed.
    """
    if _FIRST_RUN_MARKER.exists():
        return False

    ensure_config_dir()
    ensure_global_cgcignore()
    load_context_config()
    if show_welcome:
        _print_welcome_banner()

    _FIRST_RUN_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _FIRST_RUN_MARKER.write_text("1")
    return True


def ensure_config_file():
    """
    Create default .env config file on first run if it does not exist.
    """
    ensure_config_dir()

    if CONFIG_FILE.exists():
        return False  # file already exists

    save_config(
        DEFAULT_CONFIG.copy(),
        preserve_db_credentials=False,
        persist_default_database=False,
    )
    return True  # file was created




def show_config():
    """Display current configuration in a nice table."""
    created = ensure_config_file()
    if created:
        console.print(
            f"[green]🆕 Created default configuration at {CONFIG_FILE}[/green]\n"
        )
    config = load_config()
    
    # Separate database credentials from configuration
    db_creds = {k: v for k, v in config.items() if k in DATABASE_CREDENTIAL_KEYS}
    config_settings = {k: v for k, v in config.items() if k not in DATABASE_CREDENTIAL_KEYS}
    
    # Show database credentials if they exist
    if db_creds:
        console.print("\n[bold cyan]Database Credentials[/bold cyan]")
        db_table = Table(show_header=True, header_style="bold magenta")
        db_table.add_column("Credential", style="cyan", width=20)
        db_table.add_column("Value", style="green", width=30)
        
        for key in sorted(db_creds.keys()):
            value = db_creds[key]
            # Mask password
            if "PASSWORD" in key:
                value = "********" if value else "(not set)"  
            db_table.add_row(key, value)
        
        console.print(db_table)
    
    # Show configuration settings
    console.print("\n[bold cyan]Configuration Settings[/bold cyan]")
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Setting", style="cyan", width=25)
    table.add_column("Value", style="green", width=20)
    table.add_column("Description", style="dim", width=50)
    
    for key in sorted(config_settings.keys()):
        value = config_settings[key]
        description = CONFIG_DESCRIPTIONS.get(key, "")

        # Never print secret-like values (e.g. the HTTP API key) in plaintext.
        if "API_KEY" in key.upper() and value:
            value = "********"

        # Highlight non-default values
        if value != DEFAULT_CONFIG.get(key):
            value_style = "[bold yellow]" + value + "[/bold yellow]"
        else:
            value_style = value

        table.add_row(key, value_style, description)
    
    console.print(table)
    console.print(f"\n[cyan]Config file: {CONFIG_FILE}[/cyan]")


# =============================================================================
# CONTEXT SYSTEM  (config.yaml)
# =============================================================================

CONTEXT_CONFIG_FILE = CONFIG_DIR / "config.yaml"
_LEGACY_CONTEXT_CONFIG_FILE = CONFIG_DIR / "cgc_config.yaml"

# Valid mode values
VALID_MODES = ["global", "per-repo", "named"]


@dataclass
class ContextInfo:
    """Metadata for a single named context."""
    name: str
    database: str = "ladybugdb"        # neo4j | falkordb | kuzudb | ladybugdb
    db_path: str = ""                    # resolved at init if empty
    repos: List[str] = field(default_factory=list)
    cgcignore_path: str = ""            # resolved at init if empty
    database_is_explicit: bool = True


@dataclass
class ContextConfig:
    """Top-level structure stored in CGC_HOME/config.yaml."""
    version: int = 1
    mode: str = "global"                 # global | per-repo | named
    default_context: str = ""           # used when mode=named and no --context flag
    contexts: Dict[str, ContextInfo] = field(default_factory=dict)


def _default_db_path(context_name: str, database: str) -> str:
    """Return the canonical DB path for a named context."""
    return str(CONFIG_DIR / "contexts" / context_name / "db" / database)


def _context_database_setting(context_path: Path) -> Optional[str]:
    """Read an explicitly stored backend from a repository context, if present."""
    local_yaml = Path(context_path) / "config.yaml"
    if not local_yaml.exists():
        return None
    try:
        with open(local_yaml, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        database = raw.get("database") if isinstance(raw, dict) else None
        return str(database) if database else None
    except Exception:
        return None


def _infer_legacy_context_database(
    context_path: Path, default_database: str
) -> str:
    """Keep pre-backend-metadata repository contexts on existing graph data.

    Before contexts persisted a backend setting, FalkorDB was the implicit
    per-repository backend. Some contexts also used KùzuDB after FalkorDB
    fallback. Prefer existing embedded storage over a newer installation's
    LadybugDB default so those graphs remain visible. New contexts without
    legacy storage use the configured default.
    """
    db_dir = Path(context_path) / "db"
    if (db_dir / "kuzudb").exists() or (db_dir / "kuzudb.db").exists():
        return "kuzudb"
    if (db_dir / "falkordb").exists() or (db_dir / "falkordb.db").exists():
        return "falkordb"
    return default_database


def _context_database_for_path(
    context_path: Path, default_database: str
) -> str:
    """Resolve a repository context's stored or legacy backend consistently."""
    return _context_database_and_path_for_path(context_path, default_database)[0]


def _context_database_and_path_for_path(
    context_path: Path, default_database: str
) -> tuple[str, str]:
    """Resolve both backend and existing database path for a repository context."""
    configured_database = _context_database_setting(context_path)
    database = configured_database or _infer_legacy_context_database(
        context_path, default_database
    )
    return _existing_context_fallback_storage(
        context_path, database, database_is_explicit=configured_database is not None
    )


def _existing_context_fallback_storage(
    context_path: Path,
    database: str,
    db_path: Optional[str] = None,
    *,
    database_is_explicit: bool = False,
) -> tuple[str, str]:
    """Preserve existing KùzuDB paths and storage from historical fallbacks."""
    requested_path = Path(db_path) if db_path else Path(context_path) / "db" / database
    kuzu_path = Path(context_path) / "db" / "kuzudb"
    kuzu_file_path = kuzu_path.with_suffix(".db")

    if database == "kuzudb":
        if (
            requested_path == kuzu_path
            and not requested_path.exists()
            and kuzu_file_path.exists()
        ):
            requested_path = kuzu_file_path
        return database, str(requested_path)

    if database != "falkordb" or database_is_explicit:
        return database, str(requested_path)

    canonical_falkordb_path = Path(context_path) / "db" / "falkordb"
    if requested_path == canonical_falkordb_path and not requested_path.exists():
        if kuzu_path.exists():
            return "kuzudb", str(kuzu_path)
        if kuzu_file_path.exists():
            return "kuzudb", str(kuzu_file_path)
    return database, str(requested_path)


def _named_context_database_and_path(
    context_name: str,
    database: str,
    db_path: str,
    *,
    database_is_explicit: bool,
) -> tuple[str, str]:
    """Resolve named-context storage, including an implicit prior fallback."""
    context_path = CONFIG_DIR / "contexts" / context_name
    canonical_db_path = _default_db_path(context_name, database)
    if (
        not database_is_explicit
        and database == "ladybugdb"
        and Path(db_path) == Path(canonical_db_path)
    ):
        # A previous run may have selected an installed embedded fallback
        # while LadybugDB was unavailable. Keep opening that graph if the
        # fallback's optional driver is available again on a later run.
        database = _infer_legacy_context_database(context_path, database)
        if database != "ladybugdb":
            db_path = _default_db_path(context_name, database)
    return _existing_context_fallback_storage(
        context_path,
        database,
        db_path,
        database_is_explicit=database_is_explicit,
    )


_LEGACY_FALKORDB_PATH = CONFIG_DIR / "global" / "falkordb.db"


def _default_global_db_path(database: str, *, read_only: bool = False) -> str:
    """Return the canonical DB path for the global context.

    New layout: ``CGC_HOME/global/db/<backend>/`` (default CGC_HOME is ~/.codegraphcontext)
    For backward-compat, we check:
    1. CGC_RUNTIME_DB_PATH environment variable (highest priority — works for all DBs)
    2. FALKORDB_PATH or LADYBUGDB_PATH in config (within the active CGC_HOME profile)
    3. Legacy FalkorDB flat path
    4. New layout default
    """
    # Generic env var override — highest priority for any backend.
    # Useful for relocating the global DB to a path that avoids platform-specific
    # encoding issues (e.g. non-ASCII characters in the Windows user profile path).
    env_override = os.environ.get("CGC_RUNTIME_DB_PATH")
    if env_override:
        return env_override
    config_key = {
        "falkordb": "FALKORDB_PATH",
        "ladybugdb": "LADYBUGDB_PATH",
    }.get(database)
    if config_key:
        custom_path = load_config(read_only=read_only).get(config_key)
        if custom_path:
            resolved = Path(custom_path).resolve()
            # Ignore paths from another profile/repo that leaked via local .env
            try:
                resolved.relative_to(CONFIG_DIR.resolve())
            except ValueError:
                pass
            else:
                return str(resolved)
        if database == "falkordb" and _LEGACY_FALKORDB_PATH.exists():
            return str(_LEGACY_FALKORDB_PATH)
    if database == "kuzudb":
        canonical_path = CONFIG_DIR / "global" / "db" / "kuzudb"
        legacy_file_path = canonical_path.with_suffix(".db")
        if not canonical_path.exists() and legacy_file_path.exists():
            return str(legacy_file_path)
    return str(CONFIG_DIR / "global" / "db" / database)


def _migrate_legacy_config_yaml() -> None:
    """Rename cgc_config.yaml -> config.yaml if the old name exists and the new one does not."""
    if _LEGACY_CONTEXT_CONFIG_FILE.exists() and not CONTEXT_CONFIG_FILE.exists():
        import shutil
        shutil.copy2(_LEGACY_CONTEXT_CONFIG_FILE, CONTEXT_CONFIG_FILE)
        console.print(f"[dim]Migrated {_LEGACY_CONTEXT_CONFIG_FILE.name} -> {CONTEXT_CONFIG_FILE.name}[/dim]")


def load_context_config(*, create_if_missing: bool = True) -> ContextConfig:
    """
    Load CGC_HOME/config.yaml (default: ~/.codegraphcontext/config.yaml).
    Returns a ContextConfig with defaults if the file does not exist. Set
    ``create_if_missing=False`` for callers that must not create or migrate it.
    """
    if create_if_missing:
        _migrate_legacy_config_yaml()

    config_path = CONTEXT_CONFIG_FILE
    if not create_if_missing and not config_path.exists() and _LEGACY_CONTEXT_CONFIG_FILE.exists():
        config_path = _LEGACY_CONTEXT_CONFIG_FILE

    if not config_path.exists():
        cfg = ContextConfig()
        if create_if_missing:
            save_context_config(cfg)
        return cfg

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        contexts: Dict[str, ContextInfo] = {}
        for name, meta in raw.get("contexts", {}).items():
            meta = meta or {}
            # Contexts written before backend selection was persisted used
            # FalkorDB as their implicit storage location. Keep sparse legacy
            # records pointed at that data; newly created contexts use the
            # LadybugDB default through ContextInfo/create_context.
            db = meta.get("database", "falkordb")
            ctx = ContextInfo(
                name=name,
                database=db,
                db_path=meta.get("db_path") or _default_db_path(name, db),
                repos=[str(r) for r in meta.get("repos", [])],
                cgcignore_path=meta.get("cgcignore_path") or str(
                    CONFIG_DIR / "contexts" / name / ".cgcignore"
                ),
                database_is_explicit=bool(
                    meta.get("database_is_explicit", meta.get("database"))
                ),
            )
            contexts[name] = ctx

        return ContextConfig(
            version=raw.get("version", 1),
            mode=raw.get("mode", "global"),
            default_context=raw.get("default_context", ""),
            contexts=contexts,
        )
    except Exception as e:
        console.print(f"[yellow]Warning: Could not load config.yaml: {e}. Using defaults.[/yellow]")
        return ContextConfig()


def save_context_config(cfg: ContextConfig) -> None:
    """Persist ContextConfig to CGC_HOME/config.yaml."""
    ensure_config_dir()

    contexts_raw: Dict[str, Any] = {}
    for name, ctx in cfg.contexts.items():
        context_raw = {
            "database": ctx.database,
            "db_path": ctx.db_path,
            "repos": ctx.repos,
            "cgcignore_path": ctx.cgcignore_path,
        }
        if not ctx.database_is_explicit:
            context_raw["database_is_explicit"] = False
        contexts_raw[name] = context_raw

    # Read-merge the existing file first so that unrelated top-level sections
    # (e.g. ``workspace_mappings``) are preserved instead of being wiped out.
    raw: Dict[str, Any] = {}
    if CONTEXT_CONFIG_FILE.exists():
        try:
            with open(CONTEXT_CONFIG_FILE, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception:
            raw = {}

    raw["version"] = cfg.version
    raw["mode"] = cfg.mode
    raw["default_context"] = cfg.default_context
    raw["contexts"] = contexts_raw

    try:
        _atomic_write_text(
            CONTEXT_CONFIG_FILE,
            yaml.dump(raw, default_flow_style=False, sort_keys=False),
        )
    except Exception as e:
        console.print(f"[red]Error saving config.yaml: {e}[/red]")


# ---------------------------------------------------------------------------
# Context Resolution
# ---------------------------------------------------------------------------

@dataclass
class ResolvedContext:
    """Result of resolve_context() — everything needed to instantiate the DB."""
    mode: str             # global | per-repo | named
    context_name: str     # empty for global / per-repo
    database: str         # neo4j | falkordb | kuzudb
    db_path: str          # absolute path to the DB directory
    cgcignore_path: str   # path to the applicable .cgcignore
    is_local: bool = False  # True when a repository-local context was found
    database_is_explicit: bool = False  # backend stored by this context, not inherited


def find_local_cgc_dir(start: Optional[Path] = None) -> Optional[Path]:
    """
    Check *start* (default: cwd) for the selected repository-context directory
    (i.e. not the global CGC_HOME).
    Returns the directory path or None.
    """
    current = start or Path.cwd()
    global_dir = CONFIG_DIR.resolve()

    candidate = get_repo_context_path(current)
    if candidate.exists() and candidate.resolve() != global_dir:
        return candidate
    return None


class ContextNotFoundError(ValueError):
    """Raised when --context names an unregistered workspace."""


def resolve_context(
    cli_context: Optional[str] = None,
    cwd: Optional[Path] = None,
    create_local_context: bool = True,
    read_only: bool = False,
) -> ResolvedContext:
    """
    Determine which context / DB to use.

    Resolution order (highest priority first):
      1. --context <name>  CLI flag
      2. Local repository-context directory (default .codegraphcontext)
      3. Global config.yaml: mode + default_context
      4. Ultimate fallback: global mode, default DB

    ``create_local_context=False`` resolves the per-repo paths without creating
    the directory, database folder, config, or copied environment file.
    ``read_only=True`` also avoids creating or migrating global config files.
    """
    cfg = (
        load_context_config(create_if_missing=False)
        if read_only
        else load_context_config()
    )
    cwd = cwd or Path.cwd()

    # --- 1. Explicit CLI flag ---
    if cli_context:
        ctx = cfg.contexts.get(cli_context)
        if ctx is None:
            raise ContextNotFoundError(
                f"Context '{cli_context}' is not registered. "
                f"Create it with: cgc context create {cli_context}"
            )
        database, db_path = _named_context_database_and_path(
            cli_context,
            ctx.database,
            ctx.db_path,
            database_is_explicit=ctx.database_is_explicit,
        )
        return ResolvedContext(
            mode="named",
            context_name=cli_context,
            database=database,
            db_path=db_path,
            cgcignore_path=ctx.cgcignore_path,
            database_is_explicit=ctx.database_is_explicit,
        )

    # --- 2. Repository-local context directory (per-repo mode only) ---
    local_cgc = find_local_cgc_dir(cwd) if cfg.mode == "per-repo" else None

    # A saved workspace mapping can point at a context directory other than the
    # repository's default location. Prefer it when there is no local context.
    mapping = get_workspace_mapping(cwd) if local_cgc is None and cfg.mode == "per-repo" else None
    if mapping:
        mapped_ctx_path = Path(mapping["context_path"])
        if mapped_ctx_path.exists() and mapped_ctx_path.is_dir():
            mapped_db = mapping.get("database") or _context_database_for_path(
                mapped_ctx_path, _configured_default_database()
            )
            mapped_database_is_explicit = (
                bool(
                    mapping.get("database_is_explicit", mapping.get("database"))
                )
                or _context_database_setting(mapped_ctx_path) is not None
            )
            if not mapped_database_is_explicit and mapped_db == "ladybugdb":
                # An earlier run may have used an embedded backend after
                # LadybugDB was unavailable. Keep opening that graph when a
                # later run can use LadybugDB again.
                mapped_db = _infer_legacy_context_database(mapped_ctx_path, mapped_db)
            mapped_db, mapped_db_path = _existing_context_fallback_storage(
                mapped_ctx_path,
                mapped_db,
                database_is_explicit=mapped_database_is_explicit,
            )
            return ResolvedContext(
                mode="per-repo",
                context_name="",
                database=mapped_db,
                db_path=mapped_db_path,
                cgcignore_path=str(mapped_ctx_path / ".cgcignore"),
                is_local=True,
                database_is_explicit=mapped_database_is_explicit,
            )
    
    # In per-repo mode, resolve the path without creating it for read-only callers.
    if local_cgc is None and cfg.mode == "per-repo":
        local_cgc = get_repo_context_path(cwd)
        if create_local_context and not read_only:
            local_cgc.mkdir(parents=True, exist_ok=True)
            (local_cgc / "db").mkdir(exist_ok=True)

            inherited_db = load_config().get("DEFAULT_DATABASE", "ladybugdb")

            # Copy global .env into local context for easy per-repo tweaking.
            # Guard against the self-copy case: when cwd is the home directory,
            # local_cgc resolves to CONFIG_DIR itself, so local_cgc / ".env" is
            # CONFIG_FILE. Copying a file onto itself raises shutil.SameFileError,
            # which crashes resolve_context for any session started from home.
            import shutil
            _target_env = local_cgc / ".env"
            if CONFIG_FILE.exists() and _target_env.resolve() != CONFIG_FILE.resolve():
                shutil.copy2(CONFIG_FILE, _target_env)

            local_yaml = local_cgc / "config.yaml"
            if not local_yaml.exists():
                local_config = (
                    {"database": inherited_db}
                    if _has_explicit_database_default(
                        include_runtime=False, include_project_env=False
                    )
                    else {}
                )
                with open(local_yaml, "w", encoding="utf-8") as f:
                    yaml.safe_dump(local_config, f)

            console.print(
                f"[dim]Auto-initialized per-repo context at {local_cgc} "
                f"(Database: {inherited_db})[/dim]"
            )

    if local_cgc is not None:
        runtime_db = os.environ.get("CGC_RUNTIME_DB_TYPE") or os.environ.get(
            "DEFAULT_DATABASE"
        )
        default_db = (
            load_config(read_only=True) if read_only else load_config()
        ).get("DEFAULT_DATABASE", "ladybugdb")
        configured_local_db = _context_database_setting(local_cgc)
        local_db = configured_local_db
        if local_db is None:
            local_db = (
                runtime_db
                if runtime_db
                else _infer_legacy_context_database(local_cgc, default_db)
            )
        local_db, db_path = _existing_context_fallback_storage(
            local_cgc,
            local_db,
            database_is_explicit=(configured_local_db is not None or runtime_db is not None),
        )
        cgcignore = str(local_cgc / ".cgcignore")
        return ResolvedContext(
            mode="per-repo",
            context_name="",
            database=local_db,
            db_path=db_path,
            cgcignore_path=cgcignore,
            is_local=True,
            database_is_explicit=_context_database_setting(local_cgc) is not None,
        )

    # --- 3. Global config.yaml ---
    if cfg.mode == "named":
        ctx_name = cfg.default_context
        ctx = cfg.contexts.get(ctx_name) if ctx_name else None
        db = ctx.database if ctx else "ladybugdb"
        db_path = ctx.db_path if ctx else _default_db_path(ctx_name, db) if ctx_name else ""
        if not db_path:
            # No default context set — fall through to global
            pass
        else:
            cgcignore = (
                ctx.cgcignore_path
                if ctx
                else str(CONFIG_DIR / "contexts" / ctx_name / ".cgcignore")
            )
            db, db_path = _named_context_database_and_path(
                ctx_name,
                db,
                db_path,
                database_is_explicit=(ctx.database_is_explicit if ctx else False),
            )
            return ResolvedContext(
                mode="named",
                context_name=ctx_name,
                database=db,
                db_path=db_path,
                cgcignore_path=cgcignore,
                database_is_explicit=ctx.database_is_explicit if ctx else False,
            )

    # --- 4. Global fallback ---
    config = load_config(read_only=True) if read_only else load_config()
    db = _global_default_database(
        config.get("DEFAULT_DATABASE", "ladybugdb"), read_only=read_only
    )
    return ResolvedContext(
        mode="global",
        context_name="",
        database=db,
        db_path=_default_global_db_path(db, read_only=read_only),
        cgcignore_path=str(CONFIG_DIR / "global" / ".cgcignore"),
        database_is_explicit=_has_explicit_database_default(read_only=read_only),
    )


# ---------------------------------------------------------------------------
# Context CRUD helpers
# ---------------------------------------------------------------------------

def create_context(
    name: str,
    database: str = "ladybugdb",
    db_path: Optional[str] = None,
    *,
    database_is_explicit: bool = True,
) -> bool:
    """Create a new named context. Returns True on success."""
    cfg = load_context_config()
    if name in cfg.contexts:
        console.print(f"[yellow]Context '{name}' already exists.[/yellow]")
        return False

    resolved_db_path = db_path or _default_db_path(name, database)
    cgcignore = str(CONFIG_DIR / "contexts" / name / ".cgcignore")

    # Ensure the context directories exist (create parent of db_path so DBs can create their files/dirs)
    Path(resolved_db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cgcignore).parent.mkdir(parents=True, exist_ok=True)

    cfg.contexts[name] = ContextInfo(
        name=name,
        database=database,
        db_path=resolved_db_path,
        repos=[],
        cgcignore_path=cgcignore,
        database_is_explicit=database_is_explicit,
    )
    save_context_config(cfg)
    console.print(f"[green]✅ Created context '{name}' (DB: {database})[/green]")
    console.print(f"   [dim]DB path: {resolved_db_path}[/dim]")
    return True


def delete_context(name: str) -> bool:
    """Delete a named context from the registry. Returns True on success."""
    cfg = load_context_config()
    if name not in cfg.contexts:
        console.print(f"[red]Context '{name}' not found.[/red]")
        return False
    del cfg.contexts[name]
    if cfg.default_context == name:
        cfg.default_context = ""
    save_context_config(cfg)
    console.print(f"[green]✅ Deleted context '{name}'[/green]")
    console.print("[dim]Note: DB files were NOT deleted. Remove manually if needed.[/dim]")
    return True


def register_repo_in_context(context_name: str, repo_path: str, auto_create: bool = False) -> bool:
    """Add a repo path to a named context (idempotent).

    When *auto_create* is True the context is silently created with default
    settings if it does not yet exist, matching the UX of
    ``cgc index ./frontend --context ProjectAB`` "just working".
    """
    cfg = load_context_config()
    ctx = cfg.contexts.get(context_name)
    if ctx is None:
        if not auto_create:
            console.print(f"[red]Context '{context_name}' not found. Create it first with 'cgc context create {context_name}'.[/red]")
            return False
        create_context(context_name, database_is_explicit=False)
        cfg = load_context_config()
        ctx = cfg.contexts.get(context_name)
        if ctx is None:
            return False
    resolved = str(Path(repo_path).resolve())
    if resolved not in ctx.repos:
        ctx.repos.append(resolved)
        save_context_config(cfg)
    return True


def set_context_mode(mode: str) -> bool:
    """Set the global CGC mode. Returns True on success."""
    if mode not in VALID_MODES:
        console.print(f"[red]Invalid mode '{mode}'. Must be one of: {', '.join(VALID_MODES)}[/red]")
        return False
    cfg = load_context_config()
    cfg.mode = mode
    save_context_config(cfg)
    console.print(f"[green]✅ Mode set to '{mode}'[/green]")
    return True


def set_default_context(name: str) -> bool:
    """Set the default named context used when no --context flag is given."""
    cfg = load_context_config()
    if name and name not in cfg.contexts:
        console.print(f"[red]Context '{name}' not found. Create it first.[/red]")
        return False
    cfg.default_context = name
    save_context_config(cfg)
    console.print(f"[green]✅ Default context set to '{name}'[/green]")
    return True


def list_contexts() -> List[ContextInfo]:
    """Return all named contexts."""
    cfg = load_context_config()
    return list(cfg.contexts.values())


# =============================================================================
# CHILD CONTEXT DISCOVERY
# =============================================================================

@dataclass
class DiscoveredContext:
    """A repository-context folder found in a child directory."""
    path: str            # absolute path to the parent repo directory
    cgc_path: str        # absolute path to the repository-context directory
    repo_name: str       # name of the parent directory
    database: str        # backend from local config.yaml, or default
    db_path: str         # resolved db path
    cgcignore_path: str  # path to .cgcignore if present


def discover_child_contexts(
    start: Optional[Path] = None,
    max_depth: int = 1,
) -> List[DiscoveredContext]:
    """Walk child directories of *start* up to *max_depth* levels looking for
    repository-context folders that represent per-repo databases.

    Returns a list of :class:`DiscoveredContext` for each match found.
    The selected global CGC_HOME is always excluded.
    """
    start = (start or Path.cwd()).resolve()
    global_dir = CONFIG_DIR.resolve()
    default_db = _configured_default_database()
    results: List[DiscoveredContext] = []

    def _scan(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(directory.iterdir())
        except PermissionError:
            return
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            candidate = get_repo_context_path(entry)
            if candidate.exists() and candidate.is_dir() and candidate.resolve() != global_dir:
                local_db, local_db_path = _context_database_and_path_for_path(
                    candidate, default_db
                )
                results.append(DiscoveredContext(
                    path=str(entry),
                    cgc_path=str(candidate),
                    repo_name=entry.name,
                    database=local_db,
                    db_path=local_db_path,
                    cgcignore_path=str(candidate / ".cgcignore"),
                ))
            if depth < max_depth:
                _scan(entry, depth + 1)

    _scan(start, 1)
    return results


# =============================================================================
# WORKSPACE MAPPINGS  (global persistence of CWD -> context path)
# =============================================================================

def _load_workspace_mappings() -> Dict[str, Dict[str, Any]]:
    """Load the ``workspace_mappings`` section from config.yaml."""
    if not CONTEXT_CONFIG_FILE.exists():
        return {}
    try:
        with open(CONTEXT_CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return raw.get("workspace_mappings", {}) or {}
    except Exception:
        return {}


def _save_workspace_mappings(mappings: Dict[str, Dict[str, Any]]) -> None:
    """Write *mappings* back into the ``workspace_mappings`` key of config.yaml,
    preserving all other keys."""
    ensure_config_dir()
    raw: Dict[str, Any] = {}
    if CONTEXT_CONFIG_FILE.exists():
        try:
            with open(CONTEXT_CONFIG_FILE, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception:
            raw = {}
    raw["workspace_mappings"] = mappings
    try:
        _atomic_write_text(
            CONTEXT_CONFIG_FILE,
            yaml.dump(raw, default_flow_style=False, sort_keys=False),
        )
    except Exception as e:
        console.print(f"[red]Error saving workspace mappings: {e}[/red]")


def get_workspace_mapping(cwd: Path) -> Optional[Dict[str, Any]]:
    """Look up a saved workspace mapping for *cwd*.

    Returns a dict with context path, database, and optional selection provenance.
    """
    mappings = _load_workspace_mappings()
    return mappings.get(str(cwd.resolve()))


def save_workspace_mapping(
    cwd: Path,
    context_path: Path,
    *,
    database: Optional[str] = None,
    database_is_explicit: bool = True,
) -> None:
    """Persist an association from *cwd* to a repository-context directory.

    *database* may specify the backend actually selected after fallback. The
    explicitness flag records whether that backend was selected by this context
    or inherited from an automatic default.
    """
    context_path = context_path.resolve()
    local_db = database or _context_database_for_path(
        context_path, _configured_default_database()
    )

    mappings = _load_workspace_mappings()
    mappings[str(cwd.resolve())] = {
        "context_path": str(context_path),
        "database": local_db,
        "database_is_explicit": database_is_explicit,
    }
    _save_workspace_mappings(mappings)


def remove_workspace_mapping(cwd: Path) -> bool:
    """Delete a saved workspace mapping. Returns True if one was removed."""
    mappings = _load_workspace_mappings()
    key = str(cwd.resolve())
    if key in mappings:
        del mappings[key]
        _save_workspace_mappings(mappings)
        return True
    return False


def list_workspace_mappings() -> Dict[str, Dict[str, Any]]:
    """Return all saved workspace mappings."""
    return _load_workspace_mappings()
