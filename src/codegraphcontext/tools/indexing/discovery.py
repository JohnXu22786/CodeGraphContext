# src/codegraphcontext/tools/indexing/discovery.py
"""Enumerate files to index with ignore rules."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, FrozenSet, List, Optional, Set, Tuple

from ...core.cgcignore import build_ignore_spec, parse_cgcignore_lines
from ...utils.debug_log import debug_log, warning_logger
from .constants import DEFAULT_IGNORE_PATTERNS

# Generic file types that are added as minimal File nodes (no source parsing).
# Must stay in sync with GraphBuilder.generic_extensions / generic_filenames.
_GENERIC_EXTENSIONS: FrozenSet[str] = frozenset({
    ".toml", ".sh", ".yaml", ".yml", ".json", ".ini", ".cfg",
    ".md", ".txt", ".bat", ".ps1",
})
# Literal dotfiles/config filenames have no suffix (e.g. Path(".gitignore").suffix == ""),
# so they must be matched by name rather than extension.
_GENERIC_FILENAMES: FrozenSet[str] = frozenset({
    "Dockerfile", "Makefile", ".gitignore", ".dockerignore", ".env",
})


@dataclass(frozen=True)
class FileDiscoveryPreview:
    """Canonical graph paths plus paths omitted from an indexing scan."""

    files: List[Path]
    excluded: List[Tuple[Path, str]]
    ignore_root: Path
    _indexing_files: List[Path] = field(default_factory=list, repr=False, compare=False)


def _ignore_reason(spec: Any, path: str, explain: bool = True) -> Optional[str]:
    if explain and hasattr(spec, "match_reason"):
        return spec.match_reason(path)
    if spec.match_file(path):
        return "ignored by configured ignore rules"
    return None


def safe_walk(
    path: Path,
    spec: Optional[Any] = None,
    ignore_dirs: Optional[Set[str]] = None,
    ignore_root: Optional[Path] = None,
    excluded_paths: Optional[List[Tuple[Path, str]]] = None,
    emit_debug_logs: bool = True,
) -> List[Path]:
    """Recursively find files under path while:
    1. Pruning directories early if they match ignore_dirs or spec (avoiding walking into ignored directories).
    2. Logging and recovering from PermissionError / OSError.
    """
    if not path.exists():
        return []
    if not path.is_dir():
        return [path]

    if ignore_root is None:
        ignore_root = path

    if ignore_dirs is None:
        ignore_dirs = set()

    discovered_files: List[Path] = []

    def onerror(err: OSError):
        if excluded_paths is not None:
            error_path = Path(os.fsdecode(err.filename)) if err.filename else path
            excluded_paths.append(
                (error_path, f"access error during walk: {err.strerror or err}")
            )
        warning_logger(f"Access error during walk, skipping: {err}")

    for root_str, dirs, files in os.walk(str(path), topdown=True, onerror=onerror):
        root_path = Path(root_str)

        # Prune ignored directories in-place so os.walk does not descend into them
        i = len(dirs) - 1
        while i >= 0:
            d = dirs[i]
            d_path = root_path / d
            if d_path.is_symlink():
                if excluded_paths is not None:
                    excluded_paths.append((d_path, "directory symlink was not followed"))
                dirs.pop(i)
                i -= 1
                continue

            try:
                rel_d = d_path.relative_to(ignore_root)
                is_ignored = False
                reason = None
                if ignore_dirs:
                    parts = {p.lower() for p in rel_d.parts}
                    ignored_names = sorted(parts.intersection(ignore_dirs))
                    if ignored_names:
                        is_ignored = True
                        reason = "excluded by IGNORE_DIRS: " + ", ".join(ignored_names)

                if not is_ignored and spec:
                    # gitwildmatch matches directory patterns with a trailing slash
                    rel_path_str = rel_d.as_posix() + "/"
                    reason = _ignore_reason(
                        spec, rel_path_str, explain=excluded_paths is not None
                    )
                    if reason:
                        is_ignored = True

                if is_ignored:
                    if emit_debug_logs:
                        debug_log(f"Ignoring directory during walk: {rel_d}")
                    if excluded_paths is not None:
                        excluded_paths.append((d_path, reason or "ignored by configured rules"))
                    dirs.pop(i)
            except Exception:
                pass
            i -= 1

        for f in files:
            discovered_files.append(root_path / f)

    return discovered_files


def inspect_files_to_index(
    path: Path,
    cgcignore_path: Optional[str] = None,
    supported_extensions: Optional[Set[str]] = None,
    create_default_cgcignore: bool = False,
    collect_exclusions: bool = True,
) -> FileDiscoveryPreview:
    """Report the exact files indexing discovers and explain omitted paths.

    ``supported_extensions`` should be the set of extensions the active parsers
    handle (e.g. ``set(parsers.keys())``).  When provided, only files whose
    suffix is in that set OR in the built-in generic extension / filename sets
    are returned.  This avoids walking tens-of-thousands of ``.properties``,
    ``.xml``, ``.conf`` etc. files that would produce "No parser found" warnings
    and contribute nothing to the graph.

    The default is read-only: if the repository has no ``.cgcignore``, built-in
    patterns are used without creating that file. Indexing passes
    ``create_default_cgcignore=True`` to preserve its existing initialization.
    """
    path = Path(path).expanduser().resolve()
    ignore_root = path.resolve() if path.is_dir() else path.resolve().parent

    spec = None
    try:
        from ...cli.config_manager import DEFAULT_CGCIGNORE_PATTERNS, get_config_value

        spec, resolved_cgcignore = build_ignore_spec(
            ignore_root=ignore_root,
            default_patterns=DEFAULT_IGNORE_PATTERNS,
            explicit_path=cgcignore_path,
            create_default_cgcignore=create_default_cgcignore,
            missing_explicit_defaults=parse_cgcignore_lines(
                DEFAULT_CGCIGNORE_PATTERNS.splitlines()
            ),
        )
        if resolved_cgcignore and not collect_exclusions:
            debug_log(f"Using .cgcignore at {resolved_cgcignore} (filtering relative to {ignore_root})")
    except OSError as e:
        warning_logger(f"Could not load/create .cgcignore: {e}")

    ignore_dirs_str = get_config_value("IGNORE_DIRS", read_only=True) or ""
    ignore_dirs = set()
    if ignore_dirs_str and path.is_dir():
        ignore_dirs = {d.strip().lower() for d in ignore_dirs_str.split(",") if d.strip()}

    excluded: List[Tuple[Path, str]] = []
    all_files = safe_walk(
        path,
        spec=spec,
        ignore_dirs=ignore_dirs,
        ignore_root=ignore_root,
        excluded_paths=excluded if collect_exclusions else None,
        emit_debug_logs=not collect_exclusions,
    )

    indexing_files_by_graph_path: dict[Path, Path] = {}
    for file_path in all_files:
        graph_path = file_path
        if file_path.is_symlink():
            try:
                target = file_path.resolve(strict=True)
            except (OSError, RuntimeError):
                if collect_exclusions:
                    excluded.append((file_path, "broken or inaccessible symbolic link"))
                continue
            try:
                target.relative_to(ignore_root)
            except ValueError:
                if collect_exclusions:
                    excluded.append((file_path, "symbolic-link target is outside the repository root"))
                continue
            graph_path = target

        if not file_path.is_file():
            continue

        try:
            rel_path = file_path.relative_to(ignore_root).as_posix()
        except ValueError:
            if collect_exclusions:
                excluded.append((file_path, "path is outside the repository root"))
            continue

        if spec:
            reason = _ignore_reason(spec, rel_path, explain=collect_exclusions)
            if reason:
                if collect_exclusions:
                    excluded.append((file_path, reason))
                continue

        if graph_path != file_path:
            graph_rel_path = graph_path.relative_to(ignore_root).as_posix()
            ignored_target_dirs = sorted(
                {part.lower() for part in Path(graph_rel_path).parent.parts}.intersection(ignore_dirs)
            )
            if ignored_target_dirs:
                if collect_exclusions:
                    excluded.append((
                        file_path,
                        "symbolic-link target "
                        f"'{graph_rel_path}' is inside a directory excluded by IGNORE_DIRS: "
                        + ", ".join(ignored_target_dirs),
                    ))
                continue

            if spec:
                target_reason = _ignore_reason(
                    spec, graph_rel_path, explain=collect_exclusions
                )
                if target_reason:
                    if collect_exclusions:
                        excluded.append((
                            file_path,
                            f"symbolic-link target '{graph_rel_path}' is excluded: {target_reason}",
                        ))
                    continue

        if supported_extensions is not None:
            allowed_exts = supported_extensions | _GENERIC_EXTENSIONS
            if file_path.suffix not in allowed_exts and file_path.name not in _GENERIC_FILENAMES:
                if collect_exclusions:
                    extension = file_path.suffix or "(none)"
                    excluded.append((file_path, f"no parser or generic-file handler for extension {extension!r}"))
                continue

        current_source = indexing_files_by_graph_path.get(graph_path)
        if current_source is None or (
            current_source != graph_path
            and (file_path == graph_path or file_path.as_posix() < current_source.as_posix())
        ):
            indexing_files_by_graph_path[graph_path] = file_path

    files = sorted(indexing_files_by_graph_path, key=lambda file: file.as_posix())
    indexing_files = [indexing_files_by_graph_path[file] for file in files]

    return FileDiscoveryPreview(
        files=files,
        excluded=sorted(excluded, key=lambda item: item[0].as_posix()),
        ignore_root=ignore_root,
        _indexing_files=indexing_files,
    )


def discover_files_to_index(
    path: Path,
    cgcignore_path: Optional[str] = None,
    supported_extensions: Optional[Set[str]] = None,
) -> Tuple[List[Path], Path]:
    """Return (files, ignore_root), the discovery contract used by indexing."""
    preview = inspect_files_to_index(
        path,
        cgcignore_path=cgcignore_path,
        supported_extensions=supported_extensions,
        create_default_cgcignore=True,
        collect_exclusions=False,
    )
    return preview._indexing_files, preview.ignore_root
