# src/codegraphcontext/tools/code_finder.py
from __future__ import annotations
from contextlib import contextmanager
import logging
import json
from collections import Counter

from typing import Any, Dict, List, Literal, Optional, TYPE_CHECKING
from pathlib import Path, PureWindowsPath

if TYPE_CHECKING:
    from ..core.database import DatabaseManager
from ..utils.path_ignore import cypher_path_not_under_ignore_dirs
from ..utils.tool_limits import get_tool_result_limit

logger = logging.getLogger(__name__)


class RepositoryIndexInProgressError(RuntimeError):
    """Raised when a query would read a graph while an index is incomplete."""


class _ReadinessAwareDriver:
    """Keep CodeFinder reads behind the persisted repository readiness marker."""

    def __init__(self, code_finder, driver):
        self._code_finder = code_finder
        self._driver = driver

    def __getattr__(self, name):
        return getattr(self._driver, name)

    def session(self, *args, **kwargs):
        return self._code_finder._read_session(self._driver, *args, **kwargs)

_MAX_TRAVERSAL_DEPTH = 20
_MAX_RELATIONSHIP_PAGE_SIZE = 500
_MAX_RELATIONSHIP_OFFSET = 100_000
_PORTABLE_FUZZY_SCAN_LIMIT = 20_000
_EVIDENCE_PREVIEW_CHAR_LIMIT = 1_024
_EVIDENCE_PAGE_CHAR_LIMIT = 1_536
_RELATIONSHIP_ARGUMENT_MAX_ITEMS = 16
_RELATIONSHIP_ARGUMENT_MAX_CHARS = 128
_KOTLIN_CALL_AUDIT_DEFAULT_PAGE_SIZE = 100
_KOTLIN_CALL_AUDIT_CONTEXT_PREVIEW_LIMIT = 256
_KOTLIN_CALL_AUDIT_TARGET_PREVIEW_LIMIT = 5
_KOTLIN_CALL_AUDIT_MAX_TARGETS_PER_PAGE = 100
_KOTLIN_CALL_AUDIT_MAX_GROUPS = 100


def _cypher_substring_index_base(backend: str) -> int:
    """Return the first valid substring index for a graph backend."""
    return 1 if backend in {"kuzu", "kuzudb", "ladybug", "ladybugdb"} else 0


class _CandidateRows(list):
    def __init__(self, rows=(), *, scan_incomplete: bool = False):
        super().__init__(rows)
        self.scan_incomplete = scan_incomplete


def _line_number_sort_key(value: Any) -> tuple:
    """Sort line numbers numerically, placing missing or malformed values last."""
    if value is None:
        return (2, "")
    try:
        return (0, int(value))
    except (TypeError, ValueError, OverflowError):
        return (1, str(value))


def _direct_relationship_page_size(
    query_type: str, offset: int, page_size: int
) -> int:
    if type(offset) is not int or not 0 <= offset < _MAX_RELATIONSHIP_OFFSET:
        raise ValueError("offset must be an integer from 0 through 99999")
    if type(page_size) is not int or page_size < 1:
        raise ValueError("page_size must be a positive integer")

    configured_limit = get_tool_result_limit(query_type)
    limit = _MAX_RELATIONSHIP_PAGE_SIZE
    if configured_limit is not None:
        limit = min(limit, max(1, configured_limit))
    return min(
        page_size,
        limit,
        max(1, _MAX_RELATIONSHIP_OFFSET - offset),
    )


def _relationship_page_metadata(
    offset: int, page_size: int, row_count: int, has_more: bool
) -> Dict[str, Any]:
    """Describe one result page without emitting an unrequestable cursor."""
    page_end = offset + row_count
    continuation_limit_reached = bool(
        has_more and page_end >= _MAX_RELATIONSHIP_OFFSET
    )
    return {
        "offset": offset,
        "page_size": page_size,
        "result_limit": page_size,
        "truncated": bool(has_more),
        "next_offset": (
            page_end if has_more and not continuation_limit_reached else None
        ),
        "continuation_limit_reached": continuation_limit_reached,
    }


def _add_preview_length_metadata(
    row: Dict[str, Any], field: str, total_chars_field: str
) -> None:
    """Keep a bounded evidence preview while showing whether more is available."""
    total_chars = row.pop(total_chars_field, None)
    preview = row.get(field)
    if isinstance(total_chars, int) and not isinstance(total_chars, bool):
        row[f"{field}_total_chars"] = total_chars
        row[f"{field}_truncated"] = (
            isinstance(preview, str) and total_chars > len(preview)
        )


def _bounded_argument_array(property_expression: str, backend: str) -> str:
    """Return a fixed-size list preview without transferring a full signature."""
    preview_start = _cypher_substring_index_base(backend)
    if backend.lower() in {"kuzu", "kuzudb", "ladybug", "ladybugdb"}:
        return (
            f"list_transform(list_slice({property_expression}, 1, "
            f"{_RELATIONSHIP_ARGUMENT_MAX_ITEMS}), _cgc_arg_preview -> "
            f"substring(_cgc_arg_preview, {preview_start}, "
            f"{_RELATIONSHIP_ARGUMENT_MAX_CHARS}))"
        )
    return (
        f"[_cgc_arg_preview IN {property_expression}[0..{_RELATIONSHIP_ARGUMENT_MAX_ITEMS}] | "
        f"substring(_cgc_arg_preview, {preview_start}, {_RELATIONSHIP_ARGUMENT_MAX_CHARS})]"
    )


def _argument_array_truncated(
    property_expression: str,
    backend: str,
    *,
    offset: int = 0,
    limit: int = _RELATIONSHIP_ARGUMENT_MAX_ITEMS,
    char_offset: int = 0,
    char_limit: int = _RELATIONSHIP_ARGUMENT_MAX_CHARS,
) -> str:
    """Flag either omitted parameters or oversized parameter text."""
    slice_start = offset + 1 if backend.lower() in {"kuzu", "kuzudb", "ladybug", "ladybugdb"} else offset
    slice_end = offset + limit
    char_end = char_offset + char_limit
    if backend.lower() in {"kuzu", "kuzudb", "ladybug", "ladybugdb"}:
        page_slice = f"list_slice({property_expression}, {slice_start}, {slice_end})"
        oversized_items = (
            f"size(list_filter({page_slice}, _cgc_arg_check -> "
            f"size(_cgc_arg_check) > {char_end}))"
        )
        prefix_items = (
            f"size(list_filter({page_slice}, _cgc_arg_prefix -> "
            f"size(_cgc_arg_prefix) > {char_offset}))"
            if char_offset > 0 else "0"
        )
    else:
        page_slice = f"{property_expression}[{slice_start}..{slice_end}]"
        oversized_items = (
            f"size([_cgc_arg_check IN {page_slice} WHERE "
            f"size(_cgc_arg_check) > {char_end}])"
        )
        prefix_items = (
            f"size([_cgc_arg_prefix IN {page_slice} WHERE "
            f"size(_cgc_arg_prefix) > {char_offset}])"
            if char_offset > 0 else "0"
        )
    return (
        f"CASE WHEN {property_expression} IS NULL THEN false ELSE "
        f"size({property_expression}) > {slice_end} OR "
        f"{prefix_items} > 0 OR "
        f"{oversized_items} > 0 END"
    )


def _evidence_page_inputs(
    offset: int, char_limit: int, offset_name: str, limit_name: str
) -> tuple[int, int]:
    if type(offset) is not int or offset < 0:
        raise ValueError(f"{offset_name} must be a non-negative integer")
    if type(char_limit) is not int or char_limit < 1:
        raise ValueError(f"{limit_name} must be a positive integer")
    return offset, min(char_limit, _EVIDENCE_PAGE_CHAR_LIMIT)


def _add_evidence_page_metadata(
    row: Dict[str, Any],
    field: str,
    total_chars_field: str,
    requested_offset: int,
) -> None:
    total_chars = row.pop(total_chars_field, None)
    page = row.get(field)
    if isinstance(total_chars, int) and not isinstance(total_chars, bool):
        total_chars = max(0, total_chars)
        offset = min(requested_offset, total_chars)
        page_length = len(page) if isinstance(page, str) else 0
        next_offset = offset + page_length
        if next_offset >= total_chars:
            next_offset = None
    else:
        offset = requested_offset
        next_offset = None

    row[f"{field}_offset"] = offset
    row[f"{field}_total_chars"] = total_chars
    row[f"{field}_next_offset"] = next_offset
    row[f"{field}_truncated"] = next_offset is not None


# Names that are entry points rather than dead code. Matched on the *whole*
# name, lowercased. This used to be a substring test (`name CONTAINS 'main'`,
# `toLower(name) CONTAINS 'entry'`), which silently excluded any function whose
# name merely contained one of them — `domain_check`, `remainder`,
# `maintain_index`, `entry_count` — so genuinely unused code was never
# reported and there was no way to tell.
_ENTRY_POINT_NAMES = (
    "main",
    "setup",
    "run",
    "application",
    "entry",
    "entrypoint",
)
_ENTRY_POINT_NAMES_CYPHER = "[" + ", ".join(f"'{n}'" for n in _ENTRY_POINT_NAMES) + "]"

# Framework-invoked entry points for JVM/Android. These are never called by
# another function in the project -- the framework, the manifest, or an
# annotation processor invokes them -- so an uncalled one is not dead code.
# Scoped to kotlin/java so other languages keep the original global list.
_ANDROID_ENTRY_POINT_NAMES = (
    "oncreate", "onstart", "onresume", "onpause", "onstop", "ondestroy",
    "oncreateview", "onviewcreated", "ondestroyview", "onattach", "ondetach",
    "onbind", "onunbind", "onrebind", "onstartcommand", "onreceive",
    "onhandlework", "dowork", "onsaveinstancestate", "onrestoreinstancestate",
    "onactivityresult", "onrequestpermissionsresult", "onnewintent",
    "onconfigurationchanged", "onlowmemory", "ontrimmemory",
    "onbindviewholder", "oncreateviewholder", "getitemcount",
)
_TEST_HOOK_NAMES = {
    "setup", "teardown", "setupclass", "teardownclass",
    "setupmodule", "teardownmodule", "setup_method", "teardown_method",
}
_TEST_HOOK_NAMES_CYPHER = "[" + ", ".join(
    f"'{name}'" for name in sorted(_TEST_HOOK_NAMES)
) + "]"


def _classify_dead_code_confidence(row: dict) -> tuple:
    """(confidence, reason) for a zero-caller symbol (#1332).

    low    — matches a category that is almost always a false positive
             (dunder, test hook/prefix, entry-point name, override, test file)
    medium — decorated: decorators frequently register implicit callers
             (@property, @app.route, @pytest.fixture, …)
    high   — none of the above; very likely genuinely dead
    """
    name = (row.get("function_name") or "")
    lname = name.lower()
    path = (row.get("path") or "")
    if name.startswith("__") and name.endswith("__"):
        return "low", "dunder — called implicitly by the runtime"
    if lname in _TEST_HOOK_NAMES or name.startswith(("test_", "_test")):
        return "low", "test hook/prefix — invoked by the test runner"
    if lname in {n.lower() for n in _ENTRY_POINT_NAMES}:
        return "low", "entry-point name — typically called externally"
    if lname in {n.lower() for n in _ANDROID_ENTRY_POINT_NAMES}:
        return "low", "framework entry point"
    if row.get("_is_override") or "override" in (row.get("modifiers") or []):
        return "low", "override — dispatched via its base declaration"
    if "/tests/" in path or "/test/" in path or path.endswith("conftest.py"):
        return "low", "test-tree file — commonly runner-invoked"
    if row.get("_has_decorators") or row.get("decorators"):
        return "medium", "decorated — decorators often add implicit callers"
    return "high", "no callers, no excluding signal"


_ANDROID_ENTRY_POINT_NAMES_CYPHER = "[" + ", ".join(
    f"'{n}'" for n in _ANDROID_ENTRY_POINT_NAMES
) + "]"
_JVM_LANGS_CYPHER = "['kotlin', 'java']"

# Annotations whose presence means "something other than project code calls
# this": the Compose runtime, a test runner, the Hilt container, an annotation
# processor, or tooling. Pass as exclude_decorated_with when analysing an
# Android codebase. Matching is by substring (see find_dead_code), so bare
# names match annotations carrying arguments.
ANDROID_DECORATOR_PRESET = (
    "Composable", "Preview",
    "Test", "Before", "After", "BeforeEach", "AfterEach", "ParameterizedTest",
    "Inject", "Provides", "Binds", "HiltViewModel", "AndroidEntryPoint",
    "Dao", "Query", "Insert", "Update", "Delete", "Transaction", "TypeConverter",
    "JvmStatic", "Keep", "Serializable",
)


def _sanitize_depth(depth, default: int = 3) -> int:
    """Coerce and clamp a traversal depth before interpolating it into Cypher.

    The depth value ends up inside the query string (``[:CALLS*1..N]``), so it
    must be a plain bounded integer to prevent Cypher injection.
    """
    try:
        depth = int(depth)
    except (TypeError, ValueError):
        return default
    return max(1, min(depth, _MAX_TRAVERSAL_DEPTH))


def _levenshtein_distance(a: str, b: str) -> int:
    """Levenshtein distance for short identifiers (typo-tolerant name search)."""
    if len(a) < len(b):
        return _levenshtein_distance(b, a)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, c1 in enumerate(a):
        curr = [i + 1]
        for j, c2 in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]


def _normalize_identifier(s: str) -> str:
    """Lowercase and strip separator chars so camelCase / snake_case / spaces
    all compare on equal footing.

    Examples::

        _normalize_identifier('myFunction')    -> 'myfunction'
        _normalize_identifier('my_function')   -> 'myfunction'
        _normalize_identifier('my function')   -> 'myfunction'
        _normalize_identifier('MyFunc tion')   -> 'myfunction'
    """
    return s.lower().replace('_', '').replace(' ', '')


def _kotlin_call_ambiguity_group_key(row: Dict[str, Any]) -> tuple:
    return (
        row.get("_caller_uid"),
        row.get("caller_path"),
        row.get("caller_line"),
        row.get("caller_end_line"),
        row.get("call_line"),
        row.get("full_call_name"),
        row.get("target_name"),
    )


def _kotlin_call_ambiguity_target_key(row: Dict[str, Any]) -> tuple:
    context = row.get("target_context")
    if isinstance(context, str):
        context = context[:_KOTLIN_CALL_AUDIT_CONTEXT_PREVIEW_LIMIT]
    else:
        context = None
    return (
        row.get("target_path"),
        row.get("target_line"),
        context,
        row.get("_target_uid"),
    )


def summarize_kotlin_call_ambiguity(
    rows: List[Dict[str, Any]],
    limit: int = 20,
    target_limit: int = _KOTLIN_CALL_AUDIT_TARGET_PREVIEW_LIMIT,
    total_target_limit: int = _KOTLIN_CALL_AUDIT_MAX_TARGETS_PER_PAGE,
    offset: int = 0,
    page_size: Optional[int] = None,
    has_more: bool = False,
    previous_row: Optional[Dict[str, Any]] = None,
    next_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Summarize multi-target Kotlin function CALLS edges by callsite/name group."""
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    if type(target_limit) is not int or target_limit < 1:
        raise ValueError("target_limit must be a positive integer")
    if type(total_target_limit) is not int or total_target_limit < 1:
        raise ValueError("total_target_limit must be a positive integer")

    groups: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        key = _kotlin_call_ambiguity_group_key(row)
        raw_context = row.get("target_context")
        target = _kotlin_call_ambiguity_target_key(row)
        group = groups.setdefault(
            key,
            {
                "caller_name": row.get("caller_name"),
                "caller_path": row.get("caller_path"),
                "caller_line": row.get("caller_line"),
                "caller_end_line": row.get("caller_end_line"),
                "call_line": row.get("call_line"),
                "full_call_name": row.get("full_call_name"),
                "target_name": row.get("target_name"),
                "targets": {},
            },
        )
        total_chars = row.get("target_context_total_chars")
        if type(total_chars) is not int or total_chars < 0:
            total_chars = len(raw_context) if isinstance(raw_context, str) else 0
        elif isinstance(raw_context, str):
            total_chars = max(total_chars, len(raw_context))
        group["targets"][target] = max(
            total_chars, group["targets"].get(target, 0)
        )

    partial_group_keys = set()
    ambiguous_group_keys = {
        key for key, group in groups.items() if len(group["targets"]) > 1
    }
    if rows and previous_row is not None and (
        _kotlin_call_ambiguity_group_key(previous_row)
        == _kotlin_call_ambiguity_group_key(rows[0])
    ):
        key = _kotlin_call_ambiguity_group_key(rows[0])
        partial_group_keys.add(key)
        if _kotlin_call_ambiguity_target_key(previous_row) not in groups[key]["targets"]:
            ambiguous_group_keys.add(key)
    if rows and next_row is not None and (
        _kotlin_call_ambiguity_group_key(next_row)
        == _kotlin_call_ambiguity_group_key(rows[-1])
    ):
        key = _kotlin_call_ambiguity_group_key(rows[-1])
        partial_group_keys.add(key)
        if _kotlin_call_ambiguity_target_key(next_row) not in groups[key]["targets"]:
            ambiguous_group_keys.add(key)

    ambiguous_groups = [
        {
            **{k: v for k, v in group.items() if k != "targets"},
            "targets": [],
            "target_count": len(group["targets"]),
            "targets_truncated": False,
            "partial": key in partial_group_keys,
            "target_count_complete": key not in partial_group_keys,
            "__group_key": key,
        }
        for key, group in groups.items()
        if key in ambiguous_group_keys
    ]
    ambiguous_groups.sort(
        key=lambda group: (
            -group["target_count"],
            str(group.get("caller_path") or ""),
            group.get("call_line") or 0,
            str(group.get("full_call_name") or ""),
        )
    )
    remaining_target_budget = total_target_limit
    for output_group in ambiguous_groups[:limit]:
        group_key = output_group.pop("__group_key")
        target_rows = groups[group_key]["targets"]
        preview_count = min(target_limit, remaining_target_budget)
        output_group["targets_truncated"] = len(target_rows) > preview_count
        for (target_path, target_line, target_context, _target_uid), total_chars in sorted(
            target_rows.items(),
            key=lambda item: (
                str(item[0][0] or ""),
                item[0][1] or 0,
                str(item[0][2] or ""),
                str(item[0][3] or ""),
            ),
        )[:preview_count]:
            context = target_context if isinstance(target_context, str) else None
            output_group["targets"].append({
                "path": target_path,
                "line_number": target_line,
                "context": context,
                "context_total_chars": total_chars,
                "context_truncated": bool(context is not None and total_chars > len(context)),
            })
        remaining_target_budget -= len(output_group["targets"])
    top_names = Counter(
        group.get("target_name")
        for group in ambiguous_groups
        if group.get("target_name")
    )
    result = {
        "kotlin_fn_to_fn_edges": len(rows),
        "page_edge_count": len(rows),
        "ambiguous_groups": len(ambiguous_groups),
        "ambiguous_edges": sum(group["target_count"] for group in ambiguous_groups),
        "top_names": [
            {"name": name, "groups": count}
            for name, count in top_names.most_common(limit)
        ],
        "examples": ambiguous_groups[:limit],
    }
    if page_size is not None:
        metadata = _relationship_page_metadata(offset, page_size, len(rows), has_more)
        result.update(metadata)
        result["counts_scope"] = "complete" if offset == 0 and not has_more else "page"
        result["scan_complete"] = offset == 0 and not has_more
    return result


class CodeFinder:
    """Module for finding relevant code snippets and analyzing relationships."""

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager
        self._lacks_native_fulltext = getattr(db_manager, 'get_backend_type', lambda: 'neo4j')() != 'neo4j'
        self._active_graph = None

    @property
    def driver(self):
        """Returns driver for the active graph (set via graph_name params), or default."""
        return _ReadinessAwareDriver(
            self,
            self.db_manager.get_driver(self._active_graph),
        )

    def get_read_driver(self, graph_name: Optional[str] = None):
        """Return a readiness-aware driver for the requested graph."""
        self._active_graph = graph_name
        return self.driver

    @contextmanager
    def _read_session(self, driver, *args, **kwargs):
        """Run a CodeFinder read only when the graph has no building repository.

        Embedded drivers expose a shared query lock, which is held from the
        readiness check through the result query so a build cannot start
        between the two. Drivers with transaction support run both reads in
        one transaction.
        """
        lock = getattr(driver, "_write_lock", None) or getattr(driver, "_query_lock", None)
        transaction = None
        with driver.session(*args, **kwargs) as session:
            if lock is not None:
                lock.acquire()
            try:
                begin_transaction = getattr(session, "begin_transaction", None)
                transaction = begin_transaction() if callable(begin_transaction) else None
                query_session = transaction or session
                building = query_session.run(
                    "MATCH (r:Repository {index_state: 'building'}) "
                    "RETURN r.path AS path LIMIT 1"
                ).single()
                if building is not None:
                    repo_path = building.get("path") or "<unknown>"
                    raise RepositoryIndexInProgressError(
                        f"Repository indexing is in progress for {repo_path}; retry after it completes."
                    )
                yield query_session
                if transaction is not None:
                    transaction.commit()
            except BaseException:
                if transaction is not None:
                    try:
                        transaction.rollback()
                    except Exception:
                        logger.debug("Could not roll back CodeFinder read transaction", exc_info=True)
                raise
            finally:
                if lock is not None:
                    lock.release()

    def _normalize_repo_path_filter(self, repo_path: Optional[str]) -> Optional[str]:
        """Resolve a caller-supplied repo_path to the absolute path stored on indexed
        nodes, so that 'STARTS WITH $repo_path' filters actually match.

        Callers naturally pass whatever list_indexed_repositories()/list_graphs() just
        showed them — a bare repo name, or a relative path — but node.path is stored as
        the absolute filesystem path used at indexing time. Without this, any non-exact
        repo_path silently filters every result out with no error (see issue #1633).
        """
        if not isinstance(repo_path, str):
            return repo_path

        stripped_repo_path = repo_path.strip()
        if not stripped_repo_path:
            raise ValueError("repo_path must identify a non-empty repository path")

        candidate = PureWindowsPath(stripped_repo_path).as_posix().rstrip("/")
        if not candidate:
            raise ValueError("repo_path must identify a non-empty repository path")

        if PureWindowsPath(candidate).is_absolute() or Path(candidate).is_absolute():
            return candidate

        # Reuse the currently active graph so this lookup doesn't reset scoping.
        repos = self.list_indexed_repositories(graph_name=self._active_graph)
        norm = candidate.lower()
        matches: List[str] = []
        for repo in repos:
            raw_path = PureWindowsPath(str(repo.get("path", "")).strip()).as_posix().rstrip("/")
            if not raw_path:
                continue
            repo_name = str(repo.get("name", "")).strip().lower()
            base_name = Path(raw_path).name.lower()
            path_lower = raw_path.lower()
            if (
                norm == repo_name
                or norm == base_name
                or norm == path_lower
                or path_lower.endswith("/" + norm)
            ):
                matches.append(raw_path)

        unique_matches = sorted(set(matches))
        if len(unique_matches) == 1:
            return unique_matches[0]

        # Fall back to resolving relative to cwd, in case the caller passed a path
        # that's valid on the machine running this process (e.g. CLI usage).
        try:
            cwd_candidate = str((Path.cwd() / candidate).resolve()).rstrip("/")
        except Exception:
            cwd_candidate = None

        if cwd_candidate and any(
            str(repo.get("path", "")).strip().rstrip("/") == cwd_candidate
            or str(repo.get("path", "")).strip().rstrip("/").startswith(cwd_candidate + "/")
            for repo in repos
        ):
            return cwd_candidate

        # No confident match (zero or ambiguous multiple) — return unchanged rather
        # than guess; the STARTS WITH filter will simply match nothing, same as today.
        return candidate

    def _normalize_repo_path_prefix(self, repo_path: Optional[str]) -> Optional[str]:
        """Return a path prefix with a trailing separator to avoid sibling matches."""
        normalized = self._normalize_repo_path_filter(repo_path)
        if not normalized:
            return None
        return normalized.rstrip("/\\") + "/"

    def audit_kotlin_call_ambiguity(
        self,
        repo_path: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        page_size: int = _KOTLIN_CALL_AUDIT_DEFAULT_PAGE_SIZE,
    ) -> Dict[str, Any]:
        """Audit Kotlin function-to-function CALLS edges for multi-target callsites."""
        if type(limit) is not int or not 1 <= limit <= _KOTLIN_CALL_AUDIT_MAX_GROUPS:
            raise ValueError(
                f"limit must be an integer from 1 through {_KOTLIN_CALL_AUDIT_MAX_GROUPS}"
            )
        page_size = _direct_relationship_page_size(
            "kotlin_call_audit", offset, page_size
        )
        query_offset = max(0, offset - 1)
        query_limit = page_size + 1 + int(offset > 0)
        repo_path = self._normalize_repo_path_prefix(repo_path)
        repo_filter = (
            "AND a.path STARTS WITH $repo_path AND b.path STARTS WITH $repo_path"
            if repo_path else ""
        )
        backend = getattr(
            self.db_manager, "get_backend_type", lambda: "neo4j"
        )()
        query = f"""
            MATCH (a:Function)-[r:CALLS|HEURISTIC_CALLS]->(b:Function)
            WHERE a.path ENDS WITH '.kt'
              AND b.path ENDS WITH '.kt'
              {repo_filter}
            WITH DISTINCT a.uid as _caller_uid,
                   b.uid as _target_uid,
                   a.name as caller_name,
                   a.path as caller_path,
                   a.line_number as caller_line,
                   a.end_line as caller_end_line,
                   r.line_number as call_line,
                   r.full_call_name as full_call_name,
                   b.name as target_name,
                   b.path as target_path,
                   b.line_number as target_line,
                   substring(b.context, $context_start, $context_char_limit) as target_context,
                   size(b.context) as target_context_total_chars,
                   type(r) as _edge_type,
                   r.args_key as _edge_args_key
            ORDER BY caller_path, caller_line, caller_end_line, call_line,
                     full_call_name, target_name, _caller_uid, target_path,
                     target_line, _target_uid, _edge_type, _edge_args_key
            SKIP $offset LIMIT $limit
            RETURN _caller_uid, _target_uid, caller_name, caller_path,
                   caller_line, caller_end_line, call_line, full_call_name,
                   target_name, target_path, target_line, target_context,
                   target_context_total_chars, _edge_type
        """
        with self.driver.session() as session:
            rows = session.run(
                query,
                repo_path=repo_path,
                context_start=_cypher_substring_index_base(str(backend).lower()),
                context_char_limit=_KOTLIN_CALL_AUDIT_CONTEXT_PREVIEW_LIMIT,
                offset=query_offset,
                limit=query_limit,
            ).data()

        previous_row = rows[0] if offset and rows else None
        page_start = int(offset > 0 and bool(rows))
        page_rows = rows[page_start:page_start + page_size]
        lookahead_index = page_start + page_size
        next_row = rows[lookahead_index] if len(rows) > lookahead_index else None
        has_more = next_row is not None
        return summarize_kotlin_call_ambiguity(
            page_rows,
            limit=limit,
            target_limit=_KOTLIN_CALL_AUDIT_TARGET_PREVIEW_LIMIT,
            total_target_limit=_KOTLIN_CALL_AUDIT_MAX_TARGETS_PER_PAGE,
            offset=offset,
            page_size=page_size,
            has_more=has_more,
            previous_row=previous_row,
            next_row=next_row,
        )

    def format_query(
        self,
        find_by: Literal["Class", "Function"],
        fuzzy_search: bool,
        repo_path: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """Format the search query based on the search type and fuzzy search settings."""
        repo_filter = "AND node.path STARTS WITH $repo_path" if repo_path else ""
        if self._lacks_native_fulltext:
            # FalkorDB does not support CALL db.idx.fulltext.queryNodes.
            # Fall back to a pure Cypher CONTAINS/toLower match on node name.
            name_filter = "toLower(node.name) CONTAINS toLower($search_term)"
            return f"""
                MATCH (node:{find_by})
                WHERE {name_filter} {repo_filter}
                RETURN node.name as name, node.path as path, node.line_number as line_number,
                    node.is_dependency as is_dependency,
                    node.uid as _sort_uid
                ORDER BY node.is_dependency ASC, node.path, node.line_number, node.name, node.uid
                LIMIT $limit
            """
        return f"""
            CALL db.index.fulltext.queryNodes("code_search_index", $search_term) YIELD node, score
                WITH node, score
                WHERE node:{find_by} {'AND node.name CONTAINS $search_term' if not fuzzy_search else ''} {repo_filter}
                RETURN node.name as name, node.path as path, node.line_number as line_number,
                    node.is_dependency as is_dependency,
                    node.uid as _sort_uid, score as _match_score
                ORDER BY node.is_dependency ASC, score DESC, node.path, node.line_number,
                    CASE WHEN node:Function THEN 'function'
                         WHEN node:Class THEN 'class'
                         ELSE 'variable' END,
                    node.name, node.uid
                LIMIT $limit
            """

    def _find_by_name_fuzzy_portable(
        self,
        label: Literal["Function", "Class"],
        search_term: str,
        edit_distance: int,
        repo_path: Optional[str],
        limit: int = 20,
    ) -> List[Dict]:
        """Fuzzy name match for backends without Lucene fuzzy syntax (Kùzu, FalkorDB, …).

        Compares both the raw query and its identifier-normalised form against each
        candidate name, taking the minimum distance.  This lets camelCase queries
        match snake_case stored names and vice-versa without inflating the distance.
        """
        if not search_term.strip():
            return _CandidateRows()
        where_clause = "WHERE node.path STARTS WITH $repo_path" if repo_path else ""
        # Bound both scoped and unscoped scans, fetching one extra row to detect
        # when the fixed scan window omitted possible fuzzy matches.
        scan_limit = _PORTABLE_FUZZY_SCAN_LIMIT + 1
        params: Dict[str, Any] = {"scan_limit": scan_limit}
        if repo_path:
            params["repo_path"] = repo_path
        query = f"""
            MATCH (node:{label})
            {where_clause}
            RETURN node.name as name, node.path as path, node.line_number as line_number,
                node.is_dependency as is_dependency,
                node.uid as _sort_uid
            ORDER BY node.is_dependency ASC, node.path, node.line_number, node.name, node.uid
            LIMIT $scan_limit
        """
        with self.driver.session() as session:
            rows = session.run(query, **params).data()
        scan_incomplete = len(rows) > _PORTABLE_FUZZY_SCAN_LIMIT
        rows = rows[:_PORTABLE_FUZZY_SCAN_LIMIT]

        # Two query forms:
        #    q_raw  – lowercased original (e.g. "myFuncton" → "myfuncton")
        #    q_norm – separator-stripped  (e.g. "my_functon" → "myfuncton")
        # Using the minimum of both distances avoids the space-inflation bug where
        # the handler's replace('_', ' ') turns "my_functon" into "my functon",
        # which compares poorly against camelCase stored names.
        q_raw = search_term.lower()
        q_norm = _normalize_identifier(search_term)

        scored: List[tuple[int, Dict]] = []
        for row in rows:
            nm = row.get("name")
            if not isinstance(nm, str):
                continue
            nm_lower = nm.lower()
            nm_norm = _normalize_identifier(nm)
            d = min(
                _levenshtein_distance(q_raw, nm_lower),
                _levenshtein_distance(q_norm, nm_norm),
            )
            if d <= edit_distance:
                row["_match_score"] = -float(d)
                scored.append((d, row))
        scored.sort(
            key=lambda x: (
                bool(x[1].get("is_dependency")),
                x[0],
                str(x[1].get("path") or ""),
                _line_number_sort_key(x[1].get("line_number")),
                str(x[1].get("name") or ""),
                str(x[1].get("_sort_uid") or ""),
            )
        )
        return _CandidateRows(
            [r for _, r in scored[:limit]],
            scan_incomplete=scan_incomplete,
        )

    def hydrate_code_evidence(
        self,
        items: List[Dict],
        *,
        include_source: bool = False,
        include_docstring: bool = False,
        source_offset: int = 0,
        docstring_offset: int = 0,
        source_char_limit: int = 1_024,
        docstring_char_limit: int = 1_024,
        graph_name: Optional[str] = None,
    ) -> None:
        """Load bounded previews and requested text slices for the result page."""
        labels_by_search_type = {
            "function_name": "Function",
            "class_name": "Class",
            "variable_name": "Variable",
        }
        labels_by_content_type = {
            "function": "Function",
            "class": "Class",
            "variable": "Variable",
        }
        pending: Dict[str, List[Dict]] = {
            "Function": [],
            "Class": [],
            "Variable": [],
        }
        for item in items:
            label = labels_by_search_type.get(item.get("search_type"))
            if item.get("search_type") == "content":
                label = labels_by_content_type.get(item.get("type"))
            if not label:
                continue
            uid = item.get("_sort_uid")
            if uid is not None:
                pending[label].append(item)

        source_char_limit = (
            min(source_char_limit, _EVIDENCE_PAGE_CHAR_LIMIT)
            if type(source_char_limit) is int and source_char_limit > 0
            else 1_024
        )
        docstring_char_limit = (
            min(docstring_char_limit, _EVIDENCE_PAGE_CHAR_LIMIT)
            if type(docstring_char_limit) is int and docstring_char_limit > 0
            else 1_024
        )
        backend = getattr(self.db_manager, "get_backend_type", lambda: "neo4j")()
        substring_index_base = _cypher_substring_index_base(backend)
        params: Dict[str, Any] = {
            "preview_start": substring_index_base,
            "preview_limit": _EVIDENCE_PREVIEW_CHAR_LIMIT,
        }
        evidence_fields = [
            "substring(node.source, $preview_start, $preview_limit) as _source_preview",
            "size(node.source) as _source_total_chars",
            "substring(node.docstring, $preview_start, $preview_limit) as _docstring_preview",
            "size(node.docstring) as _docstring_total_chars",
        ]
        if include_source:
            source_offset = max(0, source_offset)
            params.update(
                source_offset=source_offset,
                source_start=source_offset + substring_index_base,
                source_char_limit=source_char_limit,
            )
            evidence_fields.append(
                "substring(node.source, $source_start, $source_char_limit) as _source_page"
            )
        if include_docstring:
            docstring_offset = max(0, docstring_offset)
            params.update(
                docstring_offset=docstring_offset,
                docstring_start=docstring_offset + substring_index_base,
                docstring_char_limit=docstring_char_limit,
            )
            evidence_fields.append(
                "substring(node.docstring, $docstring_start, $docstring_char_limit) as _docstring_page"
            )
        driver = self.db_manager.get_driver(graph_name)
        for label, candidates in pending.items():
            uids = list(dict.fromkeys(item["_sort_uid"] for item in candidates))
            if not uids:
                continue
            query_params = dict(params, uids=uids)
            label_fields = list(evidence_fields)
            if label == "Variable":
                label_fields.extend([
                    "substring(node.value, $preview_start, $preview_limit) as _value_preview",
                    "size(node.value) as _value_total_chars",
                    "substring(node.context, $preview_start, $preview_limit) as _context_preview",
                    "size(node.context) as _context_total_chars",
                ])
            projection = ", ".join(label_fields)
            query = f"""
                MATCH (node:{label})
                WHERE node.uid IN $uids
                RETURN node.uid as _sort_uid, {projection}
            """
            with driver.session() as session:
                rows = session.run(query, **query_params).data()
            evidence_by_uid = {row.get("_sort_uid"): row for row in rows}
            for item in candidates:
                evidence = evidence_by_uid.get(item["_sort_uid"])
                if evidence is not None:
                    for field in (
                        "_source_preview",
                        "_source_total_chars",
                        "_docstring_preview",
                        "_docstring_total_chars",
                    ):
                        item[field] = evidence.get(field)
                    if include_source:
                        item["_source_page"] = evidence.get("_source_page")
                    if include_docstring:
                        item["_docstring_page"] = evidence.get("_docstring_page")
                    if label == "Variable":
                        for field in (
                            "_value_preview",
                            "_value_total_chars",
                            "_context_preview",
                            "_context_total_chars",
                        ):
                            item[field] = evidence.get(field)
                    for field in ("source", "docstring", "value", "context"):
                        item.pop(field, None)

    def find_by_function_name(
        self,
        search_term: str,
        fuzzy_search: bool,
        repo_path: Optional[str] = None,
        edit_distance: int = 2,
        limit: int = 20,
    ) -> List[Dict]:
        """Find functions by name matching."""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        if not fuzzy_search:
            with self.driver.session() as session:
                result = session.run(f"""
                    MATCH (node:Function {{name: $name}})
                    {"WHERE node.path STARTS WITH $repo_path" if repo_path else ""}
                    RETURN node.name as name, node.path as path, node.line_number as line_number,
                           node.is_dependency as is_dependency,
                           node.uid as _sort_uid
                    ORDER BY node.is_dependency ASC, node.path, node.line_number, node.name, node.uid
                    LIMIT $limit
                """, name=search_term, repo_path=repo_path, limit=limit)
                return result.data()

        if self._lacks_native_fulltext:
            return self._find_by_name_fuzzy_portable(
                "Function", search_term, edit_distance, repo_path, limit
            )

        formatted_search_term = f"name:{search_term}"
        with self.driver.session() as session:
            result = session.run(
                self.format_query("Function", fuzzy_search, repo_path, limit),
                search_term=formatted_search_term,
                repo_path=repo_path,
                limit=limit,
            )
            return result.data()

    def find_by_class_name(
        self,
        search_term: str,
        fuzzy_search: bool,
        repo_path: Optional[str] = None,
        edit_distance: int = 2,
        limit: int = 20,
    ) -> List[Dict]:
        """Find classes by name matching."""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        if not fuzzy_search:
            with self.driver.session() as session:
                result = session.run(f"""
                    MATCH (node:Class {{name: $name}})
                    {"WHERE node.path STARTS WITH $repo_path" if repo_path else ""}
                    RETURN node.name as name, node.path as path, node.line_number as line_number,
                           node.is_dependency as is_dependency,
                           node.uid as _sort_uid
                    ORDER BY node.is_dependency ASC, node.path, node.line_number, node.name, node.uid
                    LIMIT $limit
                """, name=search_term, repo_path=repo_path, limit=limit)
                return result.data()

        if self._lacks_native_fulltext:
            return self._find_by_name_fuzzy_portable(
                "Class", search_term, edit_distance, repo_path, limit
            )

        formatted_search_term = f"name:{search_term}"
        with self.driver.session() as session:
            result = session.run(
                self.format_query("Class", fuzzy_search, repo_path, limit),
                search_term=formatted_search_term,
                repo_path=repo_path,
                limit=limit,
            )
            return result.data()

    def find_by_variable_name(
        self,
        search_term: str,
        repo_path: Optional[str] = None,
        limit: int = 20,
        *,
        offset: int = 0,
        include_context: bool = False,
        include_value: bool = False,
    ) -> List[Dict]:
        """Find variables by name, loading bounded value/context previews on request."""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        evidence_fields = []
        params: Dict[str, Any] = {
            "search_term": search_term,
            "repo_path": repo_path,
            "limit": limit,
            "offset": offset,
        }
        if include_context or include_value:
            backend = getattr(self.db_manager, "get_backend_type", lambda: "neo4j")()
            start_index = (
                1
                if backend in {"kuzu", "kuzudb", "ladybug", "ladybugdb"}
                else 0
            )
            params.update(
                evidence_start=start_index,
                evidence_limit=_EVIDENCE_PREVIEW_CHAR_LIMIT,
            )
            if include_context:
                evidence_fields.append(
                    "substring(v.context, $evidence_start, $evidence_limit) as context"
                )
                evidence_fields.append("size(v.context) as _context_total_chars")
            if include_value:
                evidence_fields.append(
                    "substring(v.value, $evidence_start, $evidence_limit) as value"
                )
                evidence_fields.append("size(v.value) as _value_total_chars")
        evidence_projection = (
            ", " + ", ".join(evidence_fields) if evidence_fields else ""
        )
        with self.driver.session() as session:
            result = session.run(f"""
                MATCH (v:Variable)
                WHERE v.name CONTAINS $search_term {"AND v.path STARTS WITH $repo_path" if repo_path else ""}
                RETURN v.name as name, v.path as path, v.line_number as line_number,
                       v.is_dependency as is_dependency,
                       v.uid as _sort_uid{evidence_projection}
                ORDER BY v.is_dependency ASC, v.path, v.line_number, v.name, v.uid
                SKIP $offset
                LIMIT $limit
            """, **params)
            
            rows = result.data()
        for row in rows:
            for field in ("context", "value"):
                total_key = f"_{field}_total_chars"
                if total_key not in row:
                    continue
                total = row.pop(total_key)
                preview = row.get(field) or ""
                row[f"{field}_total_chars"] = total
                row[f"{field}_truncated"] = (
                    total is not None and len(preview) < total
                )
        return rows

    def find_by_content(
        self,
        search_term: str,
        repo_path: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict]:
        """Find code by content matching in source or docstrings using the full-text index."""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        if self._lacks_native_fulltext:
            return self._find_by_content_falkordb(search_term, repo_path, limit, offset)
        with self.driver.session() as session:
            result = session.run(f"""
                CALL db.index.fulltext.queryNodes("code_search_index", $search_term) YIELD node, score
                WITH node, score
                WHERE (node:Function OR node:Class OR node:Variable) {"AND node.path STARTS WITH $repo_path" if repo_path else ""}
                MATCH (node)<-[:CONTAINS]-(f:File)
                RETURN
                    CASE
                        WHEN node:Function THEN 'function'
                        WHEN node:Class THEN 'class'
                        ELSE 'variable'
                    END as type,
                    node.name as name, f.path as path,
                    node.line_number as line_number, node.is_dependency as is_dependency,
                    node.uid as _sort_uid, score as _match_score
                ORDER BY node.is_dependency ASC, score DESC, f.path, node.line_number,
                    CASE WHEN node:Function THEN 'function'
                         WHEN node:Class THEN 'class'
                         ELSE 'variable' END,
                    node.name, node.uid
                SKIP $offset
                LIMIT $limit
            """, search_term=search_term, repo_path=repo_path, offset=offset, limit=limit)
            return result.data()

    def _find_by_content_falkordb(
        self,
        search_term: str,
        repo_path: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict]:
        """FalkorDB-compatible content search using pure Cypher CONTAINS matching.
        FalkorDB does not support CALL db.idx.fulltext.queryNodes, so we fall back
        to substring matching on name, source, and docstring fields."""
        all_results = []
        candidate_limit = offset + limit + 1
        with self.driver.session() as session:
            repo_filter = "AND node.path STARTS WITH $repo_path" if repo_path else ""
            for label, type_name in [('Function', 'function'), ('Class', 'class')]:
                result = session.run(f"""
                    MATCH (node:{label})
                    WHERE (toLower(node.name) CONTAINS toLower($search_term)
                        OR (node.source IS NOT NULL AND toLower(node.source) CONTAINS toLower($search_term))
                        OR (node.docstring IS NOT NULL AND toLower(node.docstring) CONTAINS toLower($search_term)))
                        {repo_filter}
                    RETURN
                        '{type_name}' as type,
                        node.name as name, node.path as path,
                        node.line_number as line_number, node.is_dependency as is_dependency,
                        node.uid as _sort_uid
                    ORDER BY node.is_dependency ASC, node.path, node.line_number, node.name, node.uid
                    LIMIT $limit
                """, search_term=search_term, repo_path=repo_path, limit=candidate_limit)
                all_results.extend(result.data())

        def sort_key(row):
            def value_key(value):
                if value is None:
                    return (2, "")
                if isinstance(value, (int, float)):
                    return (0, value)
                return (1, str(value))

            return (
                value_key(row.get("is_dependency")),
                value_key(row.get("path")),
                value_key(row.get("line_number")),
                str(row.get("type") or ""),
                value_key(row.get("name")),
                str(row.get("_sort_uid") or ""),
            )

        return sorted(all_results, key=sort_key)[offset:offset + limit]
    
    def find_by_module_name(
        self, search_term: str, repo_path: Optional[str] = None, limit: int = 20
    ) -> List[Dict]:
        """Find modules by name, optionally limited to modules used by this repo."""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        if repo_path:
            query = """
                MATCH (f:File)-[:IMPORTS]->(m:Module)
                WHERE f.path STARTS WITH $repo_path
                  AND m.name CONTAINS $search_term
                RETURN DISTINCT m.name as name, m.lang as lang
                ORDER BY name, lang
                LIMIT $limit
            """
        else:
            query = """
                MATCH (m:Module)
                WHERE m.name CONTAINS $search_term
                RETURN m.name as name, m.lang as lang
                ORDER BY m.name
                LIMIT $limit
            """
        with self.driver.session() as session:
            result = session.run(
                query, search_term=search_term, repo_path=repo_path, limit=limit
            )
            return result.data()

    def find_imports(
        self, search_term: str, repo_path: Optional[str] = None, limit: int = 20
    ) -> List[Dict]:
        """Find imported symbols (aliases or original names)."""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        repo_filter = "AND f.path STARTS WITH $repo_path" if repo_path else ""
        with self.driver.session() as session:
            result = session.run(f"""
                MATCH (f:File)-[r:IMPORTS]->(m:Module)
                WHERE (r.alias = $search_term OR r.imported_name = $search_term)
                  {repo_filter}
                RETURN 
                    r.alias as alias, 
                    r.imported_name as imported_name, 
                    m.name as module_name, 
                    f.path as path, 
                    r.line_number as line_number
                ORDER BY path, line_number, imported_name, alias
                LIMIT $limit
            """, search_term=search_term, repo_path=repo_path, limit=limit)
            return result.data()

    def find_related_code(
        self,
        user_query: str,
        fuzzy_search: bool,
        edit_distance: int,
        repo_path: Optional[str] = None,
        graph_name: str = None,
        candidate_limit: int = 20,
    ) -> Dict[str, Any]:
        self._active_graph = graph_name
        """Find code related to a query using multiple search strategies"""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        # For Lucene backends: split snake_case/underscore tokens so Lucene sees
        # individual words, then append the fuzzy modifier.
        # For portable backends: keep user_query verbatim — _find_by_name_fuzzy_portable
        # handles normalisation via _normalize_identifier.
        if fuzzy_search and not self._lacks_native_fulltext:
            lucene_base = user_query.replace("_", " ").strip()
            lucene_fuzzy_query = " ".join(f"{t}~{edit_distance}" for t in lucene_base.split())
        else:
            lucene_fuzzy_query = user_query

        # For portable backends, always pass the *original* query to the fuzzy name
        # matcher — _find_by_name_fuzzy_portable applies its own normalisation.
        # For Lucene-capable backends, use the Lucene fuzzy token form.
        if self._lacks_native_fulltext:
            name_lookup_q = user_query
        else:
            name_lookup_q = lucene_fuzzy_query if fuzzy_search else user_query

        content_lookup_q = lucene_fuzzy_query if (fuzzy_search and not self._lacks_native_fulltext) else user_query

        results: Dict[str, Any] = {
            "query": lucene_fuzzy_query if fuzzy_search else user_query,
            "functions_by_name": self.find_by_function_name(
                name_lookup_q, fuzzy_search, repo_path, edit_distance, candidate_limit
            ),
            "classes_by_name": self.find_by_class_name(
                name_lookup_q, fuzzy_search, repo_path, edit_distance, candidate_limit
            ),
            "variables_by_name": self.find_by_variable_name(
                user_query,
                repo_path,
                candidate_limit,
                include_context=False,
                include_value=False,
            ),  # variables are not fuzzy-matched
            "content_matches": self.find_by_content(
                content_lookup_q, repo_path, candidate_limit
            ),
        }
        
        all_results: List[Dict[str, Any]] = []
        
        for func in results["functions_by_name"]:
            func["search_type"] = "function_name"
            func["relevance_score"] = 0.9 if not func["is_dependency"] else 0.7
            all_results.append(func)
        
        for cls in results["classes_by_name"]:
            cls["search_type"] = "class_name"
            cls["relevance_score"] = 0.8 if not cls["is_dependency"] else 0.6
            all_results.append(cls)

        for var in results["variables_by_name"]:
            var["search_type"] = "variable_name"
            var["relevance_score"] = 0.7 if not var["is_dependency"] else 0.5
            all_results.append(var)
        
        for content in results["content_matches"]:
            content["search_type"] = "content"
            content["relevance_score"] = 0.6 if not content["is_dependency"] else 0.4
            all_results.append(content)
        
        all_results.sort(
            key=lambda x: (
                -float(x.get("relevance_score") or 0),
                str(x.get("search_type") or ""),
                -float(x.get("_match_score") or 0),
                str(x.get("path") or ""),
                _line_number_sort_key(x.get("line_number")),
                str(x.get("type") or ""),
                str(x.get("name") or ""),
                str(x.get("_sort_uid") or ""),
            )
        )
        
        results["ranked_results"] = all_results
        results["total_matches"] = len(all_results)
        
        return results
    
    def find_functions_by_argument(
        self, argument_name: str, path: Optional[str] = None,
        repo_path: Optional[str] = None, limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[Dict]:
        """Find functions that take a specific argument name or type."""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        with self.driver.session() as session:
            path_filter = "AND f.path = $path" if path else ""
            repo_filter = "AND f.path STARTS WITH $repo_path" if repo_path else ""
            page_clause = (
                "SKIP $offset LIMIT $limit" if limit is not None
                else "SKIP $offset" if offset else ""
            )
            params = {
                "argument_name": argument_name,
                "repo_path": repo_path,
                "preview_start": _cypher_substring_index_base(
                    getattr(self.db_manager, "get_backend_type", lambda: "neo4j")()
                ),
                "preview_char_limit": _EVIDENCE_PREVIEW_CHAR_LIMIT,
            }
            if offset or limit is not None:
                params["offset"] = offset
            if path:
                params["path"] = path
            if limit is not None:
                params["limit"] = limit

            # A WHERE attached to an OPTIONAL MATCH only constrains the
            # optional part — it can never filter f, so the original form
            # returned EVERY function for any search term. Filter f itself
            # after materializing the optional parameter match.
            query = f"""
                MATCH (f:Function)
                WHERE 1=1 {path_filter} {repo_filter}
                OPTIONAL MATCH (f)-[:HAS_PARAMETER]->(p:Parameter {{name: $argument_name}})
                WITH DISTINCT f, p
                WHERE p IS NOT NULL
                   OR (f.arg_types IS NOT NULL AND $argument_name IN f.arg_types)
                RETURN DISTINCT f.name AS function_name, f.path AS path,
                       f.line_number AS line_number,
                       substring(f.docstring, $preview_start, $preview_char_limit) AS docstring,
                       size(f.docstring) AS _docstring_total_chars,
                       f.is_dependency AS is_dependency, f.uid AS _sort_uid
                ORDER BY is_dependency ASC, path, line_number, _sort_uid
                {page_clause}
            """
            result = session.run(query, **params)
            rows = result.data()
            for row in rows:
                row.pop("_sort_uid", None)
                _add_preview_length_metadata(
                    row, "docstring", "_docstring_total_chars"
                )
            return rows

    def find_functions_by_decorator(
        self, decorator_name: str, path: Optional[str] = None,
        repo_path: Optional[str] = None, limit: Optional[int] = None,
        offset: int = 0, decorator_offset: int = 0,
        decorator_page_size: int = _RELATIONSHIP_ARGUMENT_MAX_ITEMS,
        decorator_value_offset: int = 0,
        decorator_value_page_size: int = _RELATIONSHIP_ARGUMENT_MAX_CHARS,
    ) -> List[Dict]:
        """Find functions that have a specific decorator applied to them."""
        if type(decorator_offset) is not int or not 0 <= decorator_offset < _MAX_RELATIONSHIP_OFFSET:
            raise ValueError("decorator_offset must be an integer from 0 through 99999")
        if type(decorator_page_size) is not int or decorator_page_size < 1:
            raise ValueError("decorator_page_size must be a positive integer")
        decorator_page_size = min(
            decorator_page_size,
            _RELATIONSHIP_ARGUMENT_MAX_ITEMS,
            max(1, _MAX_RELATIONSHIP_OFFSET - decorator_offset),
        )
        decorator_page_end = decorator_offset + decorator_page_size
        if type(decorator_value_offset) is not int or not 0 <= decorator_value_offset < _MAX_RELATIONSHIP_OFFSET:
            raise ValueError("decorator_value_offset must be an integer from 0 through 99999")
        if type(decorator_value_page_size) is not int or decorator_value_page_size < 1:
            raise ValueError("decorator_value_page_size must be a positive integer")
        decorator_value_page_size = min(
            decorator_value_page_size,
            _RELATIONSHIP_ARGUMENT_MAX_CHARS,
            max(1, _MAX_RELATIONSHIP_OFFSET - decorator_value_offset),
        )
        decorator_value_page_end = decorator_value_offset + decorator_value_page_size
        repo_path = self._normalize_repo_path_prefix(repo_path)
        with self.driver.session() as session:
            repo_filter = "AND f.path STARTS WITH $repo_path" if repo_path else ""
            page_clause = (
                "SKIP $offset LIMIT $limit" if limit is not None
                else "SKIP $offset" if offset else ""
            )
            params = {
                "decorator_name": decorator_name,
                "repo_path": repo_path,
                "preview_start": _cypher_substring_index_base(
                    getattr(self.db_manager, "get_backend_type", lambda: "neo4j")()
                ),
                "preview_char_limit": _EVIDENCE_PREVIEW_CHAR_LIMIT,
            }
            backend = getattr(self.db_manager, "get_backend_type", lambda: "neo4j")()
            is_kuzu = backend.lower() in {"kuzu", "kuzudb", "ladybug", "ladybugdb"}
            decorator_list_start = decorator_offset + 1 if is_kuzu else decorator_offset
            decorator_char_start = (
                decorator_value_offset + _cypher_substring_index_base(backend)
            )
            decorator_slice = (
                f"list_slice(f.decorators, {decorator_list_start}, {decorator_page_end})"
                if is_kuzu else
                f"f.decorators[{decorator_offset}..{decorator_page_end}]"
            )
            decorators_projection = (
                f"list_transform({decorator_slice}, _cgc_decorator_preview -> "
                f"substring(_cgc_decorator_preview, {decorator_char_start}, "
                f"{decorator_value_page_size}))"
                if is_kuzu else
                f"[_cgc_decorator_preview IN {decorator_slice} | "
                f"substring(_cgc_decorator_preview, {decorator_char_start}, "
                f"{decorator_value_page_size})]"
            )
            decorators_value_total_chars = (
                f"list_transform({decorator_slice}, _cgc_decorator_length -> "
                f"size(_cgc_decorator_length))"
                if is_kuzu else
                f"[_cgc_decorator_length IN {decorator_slice} | size(_cgc_decorator_length)]"
            )
            decorators_truncated = _argument_array_truncated(
                "f.decorators", backend, offset=decorator_offset,
                limit=decorator_page_size, char_offset=decorator_value_offset,
                char_limit=decorator_value_page_size,
            )
            if is_kuzu:
                decorator_values_have_more = (
                    f"size(list_filter({decorator_slice}, _cgc_decorator_check -> "
                    f"size(_cgc_decorator_check) > {decorator_value_page_end})) > 0"
                )
            else:
                decorator_values_have_more = (
                    f"size([_cgc_decorator_check IN {decorator_slice} WHERE "
                    f"size(_cgc_decorator_check) > {decorator_value_page_end}]) > 0"
                )
            decorator_fields = f"""
                {decorators_projection} AS decorators,
                {decorators_value_total_chars} AS decorators_value_total_chars,
                CASE WHEN f.decorators IS NULL THEN 0 ELSE size(f.decorators) END
                    AS decorators_total_count,
                {decorators_truncated} AS decorators_truncated,
                {decorator_offset} AS decorators_offset,
                {decorator_page_size} AS decorators_page_size,
                {decorator_value_offset} AS decorators_value_offset,
                {decorator_value_page_size} AS decorators_value_page_size,
                CASE WHEN f.decorators IS NOT NULL AND size(f.decorators) > {decorator_page_end}
                     AND {decorator_page_end} < {_MAX_RELATIONSHIP_OFFSET}
                     THEN {decorator_page_end} ELSE null END AS decorators_next_offset,
                CASE WHEN f.decorators IS NOT NULL AND size(f.decorators) > {decorator_page_end}
                     AND {decorator_page_end} >= {_MAX_RELATIONSHIP_OFFSET}
                     THEN true ELSE false END AS decorators_continuation_limit_reached,
                CASE WHEN f.decorators IS NOT NULL AND {decorator_values_have_more}
                     AND {decorator_value_page_end} < {_MAX_RELATIONSHIP_OFFSET}
                     THEN {decorator_value_page_end} ELSE null END AS decorators_value_next_offset,
                CASE WHEN f.decorators IS NOT NULL AND {decorator_values_have_more}
                     AND {decorator_value_page_end} >= {_MAX_RELATIONSHIP_OFFSET}
                     THEN true ELSE false END AS decorators_value_continuation_limit_reached
            """
            if offset or limit is not None:
                params["offset"] = offset
            if limit is not None:
                params["limit"] = limit
            if path:
                params["path"] = path
                query = f"""
                    MATCH (f:Function)
                    WHERE f.path = $path AND $decorator_name IN f.decorators {repo_filter}
                    RETURN f.name AS function_name, f.path AS path, f.line_number AS line_number,
                           substring(f.docstring, $preview_start, $preview_char_limit) AS docstring,
                           size(f.docstring) AS _docstring_total_chars,
                           f.is_dependency AS is_dependency,
                           {decorator_fields}, f.uid AS _sort_uid
                    ORDER BY f.is_dependency ASC, f.path, f.line_number, _sort_uid
                    {page_clause}
                """
                result = session.run(query, **params)
            else:
                query = f"""
                    MATCH (f:Function)
                    WHERE $decorator_name IN f.decorators {repo_filter}
                    RETURN f.name AS function_name, f.path AS path, f.line_number AS line_number,
                           substring(f.docstring, $preview_start, $preview_char_limit) AS docstring,
                           size(f.docstring) AS _docstring_total_chars,
                           f.is_dependency AS is_dependency,
                           {decorator_fields}, f.uid AS _sort_uid
                    ORDER BY f.is_dependency ASC, f.path, f.line_number, _sort_uid
                    {page_clause}
                """
                result = session.run(query, **params)
            rows = result.data()
            for row in rows:
                row.pop("_sort_uid", None)
                _add_preview_length_metadata(
                    row, "docstring", "_docstring_total_chars"
                )
            return rows
    
    def who_calls_function(
        self,
        function_name: str,
        path: Optional[str] = None,
        repo_path: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[Dict]:
        """Find callers of a function or class constructor using CALLS relationships with improved matching"""
        if path:
            path = Path(path).as_posix()
        repo_path = self._normalize_repo_path_prefix(repo_path)
        # Indexed symbols live below the repository directory. A separator
        # prevents a /repo filter from including its sibling /repo-other.
        if repo_path:
            repo_path = Path(repo_path).as_posix().rstrip("/") + "/"
        with self.driver.session() as session:
            repo_filter = (
                "AND caller.path STARTS WITH $repo_path AND target.path STARTS WITH $repo_path"
                if repo_path else ""
            )
            if type(offset) is not int or not 0 <= offset < _MAX_RELATIONSHIP_OFFSET:
                raise ValueError("offset must be an integer from 0 through 99999")
            if limit is not None and (type(limit) is not int or limit < 1):
                raise ValueError("limit must be a positive integer")
            query_limit = min(
                limit if limit is not None else _MAX_RELATIONSHIP_PAGE_SIZE,
                _MAX_RELATIONSHIP_PAGE_SIZE + 1,
            )
            page_clause = "SKIP $offset LIMIT $limit"
            params = {
                "function_name": function_name,
                "repo_path": repo_path,
                "offset": offset,
                "limit": query_limit,
            }
            use_exact_path = False
            if path:
                exact_target = session.run(f"""
                    MATCH (caller)-[:CALLS|HEURISTIC_CALLS]->
                          (target {{name: $function_name, path: $path}})
                    WHERE (target:Function OR target:Class)
                      AND (caller:Function OR caller:Class OR caller:File) {repo_filter}
                    RETURN 1 AS exact_match
                    LIMIT 1
                """,
                    function_name=function_name,
                    path=path,
                    repo_path=repo_path,
                )
                use_exact_path = bool(exact_target.data())
            if use_exact_path:
                params["path"] = path
            caller_pattern = "(caller)" if path else "(caller:Function)"
            target_pattern = (
                "{name: $function_name, path: $path}"
                if use_exact_path else "{name: $function_name}"
            )
            caller_type_filter = (
                "AND (caller:Function OR caller:Class OR caller:File)"
                if path else ""
            )
            caller_path_expr = (
                "COALESCE(caller.path, caller_file.path)" if path else "caller.path"
            )
            result = session.run(f"""
                MATCH {caller_pattern}-[call:CALLS|HEURISTIC_CALLS]->(target {target_pattern})
                WHERE (target:Function OR target:Class) {caller_type_filter} {repo_filter}
                OPTIONAL MATCH (caller_file:File)-[:CONTAINS]->(caller)
                WITH DISTINCT
                    caller.uid as _caller_uid,
                    target.uid as _target_uid,
                    type(call) as _edge_type,
                    call.line_number as _call_line,
                    call.full_call_name as _call_full_name,
                    call.args_key as _call_args_key,
                    caller.name as caller_function,
                    {caller_path_expr} as caller_file_path,
                    caller.line_number as caller_line_number,
                    call.line_number as call_line_number,
                    caller.is_dependency as caller_is_dependency,
                    target.path as target_file_path
                ORDER BY caller_is_dependency ASC, caller_file_path, caller_line_number,
                    _caller_uid, _target_uid, _call_line, _edge_type,
                    _call_full_name, _call_args_key
                    {page_clause}
                RETURN caller_function, caller_file_path, caller_line_number,
                    call_line_number, caller_is_dependency, target_file_path
            """, **params)
            results = result.data()

            return [
                {key: value for key, value in row.items() if not key.startswith("_")}
                for row in results
            ]
    
    def what_does_function_call(
        self,
        function_name: str,
        path: Optional[str] = None,
        repo_path: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        docstring_offset: int = 0,
        docstring_char_limit: int = _EVIDENCE_PREVIEW_CHAR_LIMIT,
    ) -> List[Dict]:
        """Find functions and class constructors a specific function calls using CALLS relationships"""
        docstring_offset, docstring_char_limit = _evidence_page_inputs(
            docstring_offset,
            docstring_char_limit,
            "docstring_offset",
            "docstring_char_limit",
        )
        repo_path = self._normalize_repo_path_prefix(repo_path)
        # Indexed symbols live below the repository directory. A separator
        # prevents a /repo filter from including its sibling /repo-other.
        if repo_path:
            repo_path = Path(repo_path).as_posix().rstrip("/") + "/"
        with self.driver.session() as session:
            repo_filter = (
                "AND caller.path STARTS WITH $repo_path AND called.path STARTS WITH $repo_path"
                if repo_path else ""
            )
            if type(offset) is not int or not 0 <= offset < _MAX_RELATIONSHIP_OFFSET:
                raise ValueError("offset must be an integer from 0 through 99999")
            if limit is not None and (type(limit) is not int or limit < 1):
                raise ValueError("limit must be a positive integer")
            query_limit = min(
                limit if limit is not None else _MAX_RELATIONSHIP_PAGE_SIZE,
                _MAX_RELATIONSHIP_PAGE_SIZE + 1,
            )
            page_clause = "SKIP $offset LIMIT $limit"
            params = {
                "function_name": function_name,
                "repo_path": repo_path,
                "offset": offset,
                "limit": query_limit,
            }
            substring_index_base = _cypher_substring_index_base(
                getattr(self.db_manager, "get_backend_type", lambda: "neo4j")()
            )
            params.update(
                docstring_offset=docstring_offset,
                docstring_start=docstring_offset + substring_index_base,
                docstring_char_limit=docstring_char_limit,
            )
            if path:
                # Convert path to absolute path
                absolute_file_path = Path(path).resolve().as_posix()
                params["absolute_file_path"] = absolute_file_path
                result = session.run(f"""
                    MATCH (caller:Function {{name: $function_name, path: $absolute_file_path}})
                    MATCH (caller)-[call:CALLS|HEURISTIC_CALLS]->(called)
                    WHERE (called:Function OR called:Class) {repo_filter}
                    OPTIONAL MATCH (called_file:File)-[:CONTAINS]->(called)
                    WITH DISTINCT
                        caller.uid as _caller_uid,
                        called.uid as _target_uid,
                        type(call) as _edge_type,
                        call.line_number as _call_line,
                        call.full_call_name as _call_full_name,
                        call.args_key as _call_args_key,
                        called.name as called_function,
                        called.path as called_file_path,
                        called.line_number as called_line_number,
                        substring(called.docstring, $docstring_start, $docstring_char_limit) as called_docstring,
                        size(called.docstring) as _called_docstring_total_chars,
                        called.is_dependency as called_is_dependency,
                        call.line_number as call_line_number,
                        call.full_call_name as full_call_name
                    ORDER BY called_is_dependency ASC, called_function,
                        called_file_path, called_line_number, _target_uid,
                        _caller_uid, _call_line, _edge_type, _call_full_name,
                        _call_args_key
                    {page_clause}
                    RETURN called_function, called_file_path, called_line_number,
                        called_docstring, _called_docstring_total_chars,
                        called_is_dependency, call_line_number, full_call_name
                """, **params)
            else:
                result = session.run(f"""
                    MATCH (caller:Function {{name: $function_name}})-[call:CALLS|HEURISTIC_CALLS]->(called)
                    WHERE (called:Function OR called:Class) {repo_filter}
                    OPTIONAL MATCH (called_file:File)-[:CONTAINS]->(called)
                    WITH DISTINCT
                        caller.uid as _caller_uid,
                        called.uid as _target_uid,
                        type(call) as _edge_type,
                        call.line_number as _call_line,
                        call.full_call_name as _call_full_name,
                        call.args_key as _call_args_key,
                        called.name as called_function,
                        called.path as called_file_path,
                        called.line_number as called_line_number,
                        substring(called.docstring, $docstring_start, $docstring_char_limit) as called_docstring,
                        size(called.docstring) as _called_docstring_total_chars,
                        called.is_dependency as called_is_dependency,
                        call.line_number as call_line_number,
                        call.full_call_name as full_call_name
                    ORDER BY called_is_dependency ASC, called_function,
                        called_file_path, called_line_number, _target_uid,
                        _caller_uid, _call_line, _edge_type, _call_full_name,
                        _call_args_key
                    {page_clause}
                    RETURN called_function, called_file_path, called_line_number,
                        called_docstring, _called_docstring_total_chars,
                        called_is_dependency, call_line_number, full_call_name
                """, **params)

            results = result.data()
            for row in results:
                _add_evidence_page_metadata(
                    row,
                    "called_docstring",
                    "_called_docstring_total_chars",
                    docstring_offset,
                )
            return [
                {key: value for key, value in row.items() if not key.startswith("_")}
                for row in results
            ]
    
    def who_imports_module(
        self, module_name: str, repo_path: Optional[str] = None,
        limit: Optional[int] = None, offset: int = 0,
    ) -> List[Dict]:
        """Find what files import a specific module using IMPORTS relationships"""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        with self.driver.session() as session:
            repo_filter = "AND file.path STARTS WITH $repo_path" if repo_path else ""
            page_clause = (
                "SKIP $offset LIMIT $limit" if limit is not None
                else "SKIP $offset" if offset else ""
            )
            params = {"module_name": module_name, "repo_path": repo_path}
            if offset or limit is not None:
                params["offset"] = offset
            if limit is not None:
                params["limit"] = limit
            result = session.run(f"""
                MATCH (file:File)-[imp:IMPORTS]->(module:Module)
                WHERE (module.name = $module_name OR module.full_import_name CONTAINS $module_name) {repo_filter}
                OPTIONAL MATCH (repo:Repository)-[:CONTAINS]->(file)
                WITH file, repo, COLLECT({{
                    imported_module: module.name,
                    import_alias: imp.alias,
                    full_import_name: module.full_import_name
                }}) AS imports
                RETURN
                    file.name AS file_name,
                    file.path AS path,
                    file.relative_path AS file_relative_path,
                    file.is_dependency AS file_is_dependency,
                    repo.name AS repository_name,
                    imports
                ORDER BY file_is_dependency ASC, path
                {page_clause}
            """, **params)
            
            return result.data()
    
    def who_modifies_variable(
        self, variable_name: str, repo_path: Optional[str] = None,
        limit: Optional[int] = None, offset: int = 0,
    ) -> List[Dict]:
        """Find what functions contain or modify a specific variable"""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        with self.driver.session() as session:
            repo_filter = "AND container.path STARTS WITH $repo_path" if repo_path else ""
            page_clause = (
                "SKIP $offset LIMIT $limit" if limit is not None
                else "SKIP $offset" if offset else ""
            )
            params = {
                "variable_name": variable_name,
                "repo_path": repo_path,
                "preview_start": _cypher_substring_index_base(
                    getattr(self.db_manager, "get_backend_type", lambda: "neo4j")()
                ),
                "preview_char_limit": _EVIDENCE_PREVIEW_CHAR_LIMIT,
            }
            if offset or limit is not None:
                params["offset"] = offset
            if limit is not None:
                params["limit"] = limit
            result = session.run(f"""
                MATCH (var:Variable {{name: $variable_name}})
                MATCH (container)-[:CONTAINS]->(var)
                WHERE (container:Function OR container:Class OR container:File) {repo_filter}
                OPTIONAL MATCH (file:File)-[:CONTAINS]->(container)
                RETURN DISTINCT
                    CASE 
                        WHEN container:Function THEN container.name
                        WHEN container:Class THEN container.name
                        ELSE 'file_level'
                    END as container_name,
                    CASE 
                        WHEN container:Function THEN 'function'
                        WHEN container:Class THEN 'class'
                        ELSE 'file'
                    END as container_type,
                    COALESCE(container.path, file.path) as path,
                    container.line_number as container_line_number,
                    var.line_number as variable_line_number,
                    substring(var.value, $preview_start, $preview_char_limit) as variable_value,
                    size(var.value) as _variable_value_total_chars,
                    substring(var.context, $preview_start, $preview_char_limit) as variable_context,
                    size(var.context) as _variable_context_total_chars,
                    COALESCE(container.is_dependency, file.is_dependency, false) as is_dependency,
                    var.uid as _var_uid
                ORDER BY is_dependency ASC, path, variable_line_number,
                    container_type, container_name, _var_uid
                {page_clause}
            """, **params)
            
            rows = result.data()
            for row in rows:
                row.pop("_var_uid", None)
                _add_preview_length_metadata(
                    row, "variable_value", "_variable_value_total_chars"
                )
                _add_preview_length_metadata(
                    row, "variable_context", "_variable_context_total_chars"
                )
            return rows

    def find_class_hierarchy(
        self,
        class_name: str,
        path: Optional[str] = None,
        repo_path: Optional[str] = None,
        offset: int = 0,
        page_size: int = _MAX_RELATIONSHIP_PAGE_SIZE,
    ) -> Dict[str, Any]:
        """Return bounded pages of inheritance relationships and class methods."""
        page_size = _direct_relationship_page_size(
            "class_hierarchy", offset, page_size
        )
        query_limit = page_size + 1
        repo_path = self._normalize_repo_path_prefix(repo_path)
        with self.driver.session() as session:
            repo_filter_child = "AND child.path STARTS WITH $repo_path" if repo_path else ""
            repo_filter_parent = "AND parent.path STARTS WITH $repo_path" if repo_path else ""
            if path:
                match_clause = "MATCH (child:Class {name: $class_name, path: $path})"
            else:
                match_clause = "MATCH (child:Class {name: $class_name})"
            backend = getattr(self.db_manager, "get_backend_type", lambda: "neo4j")()
            params = {
                "class_name": class_name,
                "path": path,
                "repo_path": repo_path,
                "offset": offset,
                "limit": query_limit,
                "preview_start": _cypher_substring_index_base(backend),
                "preview_char_limit": _EVIDENCE_PREVIEW_CHAR_LIMIT,
            }

            parents_query = f"""
                {match_clause}
                MATCH (child)-[:INHERITS]->(parent:Class)
                WHERE 1=1 {repo_filter_child} {repo_filter_parent}
                OPTIONAL MATCH (parent_file:File)-[:CONTAINS]->(parent)
                RETURN DISTINCT
                    parent.name as parent_class,
                    parent.path as parent_file_path,
                    parent.line_number as parent_line_number,
                    substring(parent.docstring, $preview_start, $preview_char_limit) as parent_docstring,
                    size(parent.docstring) as _parent_docstring_total_chars,
                    parent.is_dependency as parent_is_dependency,
                    parent.uid as _sort_uid
                ORDER BY parent_is_dependency ASC, parent_class,
                         parent_file_path, parent_line_number, _sort_uid
                SKIP $offset LIMIT $limit
            """
            parents_result = session.run(parents_query, **params)
            
            repo_filter_grandchild = "AND grandchild.path STARTS WITH $repo_path" if repo_path else ""
            children_query = f"""
                {match_clause}
                MATCH (grandchild:Class)-[:INHERITS]->(child)
                WHERE 1=1 {repo_filter_child} {repo_filter_grandchild}
                OPTIONAL MATCH (child_file:File)-[:CONTAINS]->(grandchild)
                RETURN DISTINCT
                    grandchild.name as child_class,
                    grandchild.path as child_file_path,
                    grandchild.line_number as child_line_number,
                    substring(grandchild.docstring, $preview_start, $preview_char_limit) as child_docstring,
                    size(grandchild.docstring) as _child_docstring_total_chars,
                    grandchild.is_dependency as child_is_dependency,
                    grandchild.uid as _sort_uid
                ORDER BY child_is_dependency ASC, child_class,
                         child_file_path, child_line_number, _sort_uid
                SKIP $offset LIMIT $limit
            """
            children_result = session.run(children_query, **params)
            
            repo_filter_method = (
                "WHERE child.path STARTS WITH $repo_path AND method.path STARTS WITH $repo_path"
                if repo_path else ""
            )
            methods_query = f"""
                {match_clause}
                MATCH (child)-[:CONTAINS]->(method:Function)
                {repo_filter_method}
                RETURN DISTINCT
                    method.name as method_name,
                    method.path as method_file_path,
                    method.line_number as method_line_number,
                    {_bounded_argument_array("method.args", backend)} as method_args,
                    size(method.args) as method_args_total_count,
                    {_argument_array_truncated("method.args", backend)} as method_args_truncated,
                    substring(method.docstring, $preview_start, $preview_char_limit) as method_docstring,
                    size(method.docstring) as _method_docstring_total_chars,
                    method.is_dependency as method_is_dependency,
                    method.uid as _sort_uid
                ORDER BY method_is_dependency ASC, method_line_number,
                         method_file_path, method_name, _sort_uid
                SKIP $offset LIMIT $limit
            """
            methods_result = session.run(methods_query, **params)

            def shape_page(rows, field, total_chars_field):
                has_more = len(rows) > page_size
                page = rows[:page_size]
                for row in page:
                    row.pop("_sort_uid", None)
                    _add_preview_length_metadata(row, field, total_chars_field)
                metadata = _relationship_page_metadata(
                    offset, page_size, len(page), has_more
                )
                return page, metadata

            parent_classes, parent_metadata = shape_page(
                parents_result.data(),
                "parent_docstring",
                "_parent_docstring_total_chars",
            )
            child_classes, child_metadata = shape_page(
                children_result.data(),
                "child_docstring",
                "_child_docstring_total_chars",
            )
            methods, method_metadata = shape_page(
                methods_result.data(),
                "method_docstring",
                "_method_docstring_total_chars",
            )
            has_more = any(
                metadata["truncated"]
                for metadata in (parent_metadata, child_metadata, method_metadata)
            )
            overall_metadata = _relationship_page_metadata(
                offset,
                page_size,
                page_size if has_more else max(
                    len(parent_classes), len(child_classes), len(methods)
                ),
                has_more,
            )

            result = {
                "class_name": class_name,
                "parent_classes": parent_classes,
                "child_classes": child_classes,
                "methods": methods,
                **overall_metadata,
            }
            for name, metadata in (
                ("parent_classes", parent_metadata),
                ("child_classes", child_metadata),
                ("methods", method_metadata),
            ):
                result[f"{name}_truncated"] = metadata["truncated"]
                result[f"{name}_next_offset"] = metadata["next_offset"]
                result[f"{name}_continuation_limit_reached"] = metadata[
                    "continuation_limit_reached"
                ]
            return result
    
    def find_function_overrides(
        self, function_name: str, repo_path: Optional[str] = None,
        limit: Optional[int] = None, offset: int = 0,
    ) -> List[Dict]:
        """Find all implementations of a function across different classes"""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        with self.driver.session() as session:
            repo_filter = "AND class.path STARTS WITH $repo_path" if repo_path else ""
            backend = getattr(self.db_manager, "get_backend_type", lambda: "neo4j")()
            page_clause = (
                "SKIP $offset LIMIT $limit" if limit is not None
                else "SKIP $offset" if offset else ""
            )
            params = {
                "function_name": function_name,
                "repo_path": repo_path,
                "preview_start": _cypher_substring_index_base(backend),
                "preview_char_limit": _EVIDENCE_PREVIEW_CHAR_LIMIT,
            }
            if offset or limit is not None:
                params["offset"] = offset
            if limit is not None:
                params["limit"] = limit
            result = session.run(f"""
                MATCH (class:Class)-[:CONTAINS]->(func:Function {{name: $function_name}})
                WHERE 1=1 {repo_filter}
                OPTIONAL MATCH (file:File)-[:CONTAINS]->(class)
                RETURN DISTINCT
                    class.name as class_name,
                    class.path as class_file_path,
                    func.name as function_name,
                    func.line_number as function_line_number,
                    {_bounded_argument_array("func.args", backend)} as function_args,
                    size(func.args) as function_args_total_count,
                    {_argument_array_truncated("func.args", backend)} as function_args_truncated,
                    substring(func.docstring, $preview_start, $preview_char_limit) as function_docstring,
                    size(func.docstring) as _function_docstring_total_chars,
                    func.is_dependency as is_dependency,
                    file.name as file_name,
                    class.uid as _class_uid, func.uid as _func_uid
                ORDER BY is_dependency ASC, class_name, _class_uid, _func_uid
                {page_clause}
            """, **params)
            
            rows = result.data()
            for row in rows:
                row.pop("_class_uid", None)
                row.pop("_func_uid", None)
                _add_preview_length_metadata(
                    row,
                    "function_docstring",
                    "_function_docstring_total_chars",
                )
            return rows
    
    def find_dead_code(
        self,
        exclude_decorated_with: Optional[List[str]] = None,
        repo_path: Optional[str] = None,
        graph_name: str = None,
        limit: Optional[int] = None,
        include_low_confidence: bool = False,
        offset: int = 0,
    ) -> Dict[str, Any]:
        self._active_graph = graph_name
        """Find potentially unused functions (not called by other functions in the project), optionally excluding those with specific decorators.

        Returns `potentially_unused_functions` (at most `limit` rows, all of
        them when `limit` is None) starting at `offset`, alongside
        `total_count`, the full number of matches before paging. Ordering is
        by path then line number, so pages are stable and continuation does
        not skip results. Full docstrings and function context are omitted;
        use `find_code` to retrieve bounded text evidence on demand.
        """
        if type(offset) is not int or not 0 <= offset < _MAX_RELATIONSHIP_OFFSET:
            raise ValueError("offset must be an integer from 0 through 99999")
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit must be a positive integer")
        if limit is not None:
            limit = min(limit, _MAX_RELATIONSHIP_OFFSET - offset)
        repo_path = self._normalize_repo_path_prefix(repo_path)
        if exclude_decorated_with is None:
            exclude_decorated_with = []

        backend = getattr(self.db_manager, "get_backend_type", lambda: "neo4j")()
        with self.driver.session() as session:
            repo_filter = "AND func.path STARTS WITH $repo_path" if repo_path else ""
            caller_repo_filter = (
                "AND caller.path STARTS WITH $repo_path" if repo_path else ""
            )
            decorator_filter = ""
            if exclude_decorated_with:
                any_conditions = " AND ".join(
                    f"NOT ANY(d IN func.decorators WHERE d CONTAINS '{p.replace(chr(39), chr(39)+chr(39))}')"
                    for p in exclude_decorated_with
                )
                decorator_filter = f"AND {any_conditions}"
            func_ignore = cypher_path_not_under_ignore_dirs("func.path")
            caller_ignore = cypher_path_not_under_ignore_dirs("caller.path")

            hard_excluded_conditions = f"""
                toLower(func.name) IN {_ENTRY_POINT_NAMES_CYPHER}
                OR (func.lang IN {_JVM_LANGS_CYPHER}
                    AND toLower(func.name) IN {_ANDROID_ENTRY_POINT_NAMES_CYPHER})
                OR (func.modifiers IS NOT NULL AND 'override' IN func.modifiers)
                OR (func.name STARTS WITH '__' AND func.name ENDS WITH '__')
                OR func.name STARTS WITH '_test'
                OR func.name STARTS WITH 'test_'
            """
            hard_exclusion_filter = (
                f"AND NOT ({hard_excluded_conditions})"
                if not include_low_confidence
                else ""
            )
            classified_low_conditions = f"""
                ({hard_excluded_conditions})
                OR toLower(func.name) IN {_TEST_HOOK_NAMES_CYPHER}
                OR toLower(func.name) IN {_ANDROID_ENTRY_POINT_NAMES_CYPHER}
                OR func.path CONTAINS '/tests/'
                OR func.path CONTAINS '/test/'
                OR func.path ENDS WITH 'conftest.py'
            """
            confidence_expression = f"""
                CASE
                    WHEN ({classified_low_conditions}) THEN 'low'
                    WHEN func.decorators IS NOT NULL AND size(func.decorators) > 0 THEN 'medium'
                    ELSE 'high'
                END
            """
            candidate_query = f"""
                MATCH (func:Function)
                WHERE func.is_dependency = false {repo_filter} {func_ignore}
                  {hard_exclusion_filter}
                  AND func.name <> '<module>'
                  {decorator_filter}
                WITH func
                OPTIONAL MATCH (caller)-[:CALLS|HEURISTIC_CALLS]->(func)
                WHERE (caller.is_dependency IS NULL OR caller.is_dependency = false)
                  {caller_repo_filter}
                  {caller_ignore}
                WITH func, count(caller) as caller_count
                WHERE caller_count = 0
                OPTIONAL MATCH (file:File)-[:CONTAINS]->(func)
            """
            count_query = f"""
                {candidate_query}
                WITH func, file, {confidence_expression} AS confidence
                RETURN
                    count(*) AS total_count,
                    sum(CASE WHEN confidence = 'high' THEN 1 ELSE 0 END) AS high_count,
                    sum(CASE WHEN confidence = 'medium' THEN 1 ELSE 0 END) AS medium_count,
                    sum(CASE WHEN confidence = 'low' THEN 1 ELSE 0 END) AS low_count
            """

            page_clause = ""
            page_params = {"repo_path": repo_path} if repo_path else {}
            if limit is not None:
                page_clause = "SKIP $offset LIMIT $limit"
                page_params.update(offset=offset, limit=limit)
            elif offset:
                page_clause = "SKIP $offset"
                page_params["offset"] = offset

            query = f"""
                {candidate_query}
                RETURN
                    func.name as function_name,
                    func.path as path,
                    func.line_number as line_number,
                    {_bounded_argument_array("func.decorators", backend)} AS decorators,
                    CASE WHEN func.decorators IS NULL THEN 0 ELSE size(func.decorators) END
                        AS decorators_total_count,
                    {_argument_array_truncated("func.decorators", backend)} AS decorators_truncated,
                    {_bounded_argument_array("func.modifiers", backend)} AS modifiers,
                    CASE WHEN func.modifiers IS NULL THEN 0 ELSE size(func.modifiers) END
                        AS modifiers_total_count,
                    {_argument_array_truncated("func.modifiers", backend)} AS modifiers_truncated,
                    CASE WHEN func.decorators IS NOT NULL AND size(func.decorators) > 0
                        THEN true ELSE false END AS _has_decorators,
                    CASE WHEN func.modifiers IS NOT NULL AND 'override' IN func.modifiers
                        THEN true ELSE false END AS _is_override,
                    file.name as file_name,
                    func.uid as _sort_uid
                ORDER BY func.path, func.line_number, func.uid
                {page_clause}
            """

            count_params = {"repo_path": repo_path} if repo_path else {}
            count_rows = session.run(count_query, **count_params).data()
            counts = count_rows[0] if count_rows else {}
            total_count = int(counts.get("total_count") or 0)
            confidence_counts = {
                key: int(counts.get(f"{key}_count") or 0)
                for key in ("high", "medium", "low")
            }

            result = session.run(query, **page_params)
            rows = result.data()
            for row in rows:
                confidence, reason = _classify_dead_code_confidence(row)
                row["confidence"] = confidence
                row["confidence_reason"] = reason
                row.pop("docstring", None)
                row.pop("context", None)
                row.pop("_has_decorators", None)
                row.pop("_is_override", None)
            for row in rows:
                row.pop("_sort_uid", None)

            page_size = len(rows) if limit is None else limit
            has_more = limit is not None and total_count > offset + len(rows)
            return {
                "potentially_unused_functions": rows,
                "total_count": total_count,
                "confidence_counts": confidence_counts,
                **_relationship_page_metadata(
                    offset, page_size, len(rows), has_more
                ),
                "note": (
                    "These functions might be unused, but could be entry points, callbacks, "
                    "or called dynamically. Use find_code for bounded source or docstring "
                    "evidence on demand."
                ),
            }
    
    def export_diagram_edges(
        self, level: str = "file", repo_path: Optional[str] = None,
        limit: Optional[int] = 300, offset: int = 0,
    ) -> Dict[str, Any]:
        """Edges for a Mermaid/DOT export (#1287).

        level="file": File -[IMPORTS]-> Module dependencies.
        level="call": Function -[CALLS]-> Function within the repo.

        Returns {"edges": [(src, dst), ...], "total_count": n, "truncated": bool}
        — the cap is reported, never silent.
        """
        if type(offset) is not int or not 0 <= offset < _MAX_RELATIONSHIP_OFFSET:
            raise ValueError("offset must be an integer from 0 through 99999")
        if limit is None:
            limit = 300
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        page_size = min(
            limit,
            _MAX_RELATIONSHIP_PAGE_SIZE,
            _MAX_RELATIONSHIP_OFFSET - offset,
        )
        candidate_limit = page_size + 1
        repo_path = self._normalize_repo_path_prefix(repo_path)
        with self.driver.session() as session:
            if level == "call":
                repo_filter = (
                    "AND caller.path STARTS WITH $repo_path "
                    "AND callee.path STARTS WITH $repo_path"
                    if repo_path else ""
                )
                match_query = f"""
                    MATCH (caller:Function)-[:CALLS]->(callee:Function)
                    WHERE caller.is_dependency = false AND callee.is_dependency = false
                      AND caller.name <> '<module>' AND callee.name <> '<module>'
                      {repo_filter}
                """
                count_query = f"""
                    {match_query}
                    WITH DISTINCT caller.name AS src, callee.name AS dst
                    RETURN count(*) AS total_count
                """
                query = f"""
                    {match_query}
                    RETURN DISTINCT caller.name AS src, callee.name AS dst
                    ORDER BY src, dst
                    SKIP $offset LIMIT $candidate_limit
                """
            else:
                repo_filter = "AND file.path STARTS WITH $repo_path" if repo_path else ""
                match_query = f"""
                    MATCH (file:File)-[:IMPORTS]->(module:Module)
                    WHERE file.is_dependency = false {repo_filter}
                """
                count_query = f"""
                    {match_query}
                    WITH DISTINCT file.relative_path AS src_rel,
                        file.name AS src_name, module.name AS dst
                    RETURN count(*) AS total_count
                """
                query = f"""
                    {match_query}
                    RETURN DISTINCT file.relative_path AS src_rel, file.name AS src_name,
                                    module.name AS dst
                    ORDER BY src_rel, dst, src_name
                    SKIP $offset LIMIT $candidate_limit
                """
            params = {"repo_path": repo_path} if repo_path else {}
            total_rows = session.run(count_query, **params).data()
            total = int((total_rows[0].get("total_count") if total_rows else 0) or 0)
            page_rows = session.run(
                query,
                **params,
                offset=offset,
                candidate_limit=candidate_limit,
            ).data()

        if level == "call":
            edges = [(r["src"], r["dst"]) for r in page_rows if r.get("src") and r.get("dst")]
        else:
            edges = [
                ((r.get("src_rel") or r.get("src_name") or ""), r["dst"])
                for r in page_rows
                if (r.get("src_rel") or r.get("src_name")) and r.get("dst")
            ]
        edges = edges[:page_size]
        has_more = len(page_rows) > page_size or total > offset + len(edges)
        return {
            "edges": edges,
            "total_count": total,
            **_relationship_page_metadata(offset, page_size, len(edges), has_more),
        }

    @staticmethod
    def _call_path_edges(rows: List[Dict], repo_path: Optional[str] = None) -> List[Dict]:
        """Flatten bounded call paths without backend-specific list indexing.

        Embedded backends do not reliably support a parameterized list predicate
        over ``nodes(path)``. Check every returned node here so a path that crosses
        the repository boundary cannot contribute any public edge.
        """
        edges = []
        seen = set()
        for row in rows:
            nodes = row["path_nodes"]
            node_props = [getattr(node, "properties", node) or {} for node in nodes]
            paths = [node.get("path") for node in node_props]
            if repo_path and any(
                not isinstance(path, str) or not path.startswith(repo_path)
                for path in paths
            ):
                continue
            names = [node.get("name") for node in node_props]
            lines = [getattr(rel, "properties", rel).get("line_number") for rel in row["rels"]]
            for i, line in enumerate(lines):
                key = (names[i], paths[i], names[i + 1], paths[i + 1], line)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(dict(zip(
                    ("caller_name", "caller_path", "callee_name", "callee_path", "line"), key,
                )))
                if len(edges) == 500:
                    return edges
        return edges

    def find_all_callers(
        self, function_name: str, path: Optional[str] = None,
        repo_path: Optional[str] = None, depth: int = 3, offset: int = 0,
        page_size: int = _MAX_RELATIONSHIP_PAGE_SIZE,
        return_metadata: bool = False,
    ) -> List[Dict] | Dict[str, Any]:
        """Page distinct edges that belong to paths ending at *function_name*."""
        if path:
            path = Path(path).as_posix()
        repo_path = self._normalize_repo_path_prefix(repo_path)
        if type(offset) is not int or not 0 <= offset < _MAX_RELATIONSHIP_OFFSET:
            raise ValueError("offset must be an integer from 0 through 99999")
        if type(page_size) is not int or page_size < 1:
            raise ValueError("page_size must be a positive integer")
        page_size = min(
            page_size,
            _MAX_RELATIONSHIP_PAGE_SIZE,
            max(1, _MAX_RELATIONSHIP_OFFSET - offset),
        )
        candidate_limit = page_size + 1
        depth = _sanitize_depth(depth)
        path_node_filter = (
            "AND all(path_node IN nodes(p) WHERE path_node.path STARTS WITH "
            f"{json.dumps(repo_path, ensure_ascii=False)})"
            if repo_path else ""
        )
        repo_filter = (
            "AND src.path STARTS WITH $repo_path "
            "AND dst.path STARTS WITH $repo_path "
            f"{path_node_filter}"
            if repo_path else ""
        )
        path_filter = "AND target.path = $path" if path else ""
        with self.driver.session() as session:
            result = session.run(f"""
                MATCH (src)-[edge:CALLS|HEURISTIC_CALLS]->(dst)
                MATCH p = (dst)-[:CALLS|HEURISTIC_CALLS*0..{depth - 1}]->(target)
                WHERE (target:Function OR target:Class)
                  AND target.name = $function_name {path_filter} {repo_filter}
                WITH DISTINCT
                    src.name AS caller_name, src.path AS caller_path,
                    dst.name AS callee_name, dst.path AS callee_path,
                    edge.line_number AS line,
                    type(edge) AS _edge_type,
                    edge.full_call_name AS _edge_full_call_name,
                    edge.args_key AS _edge_args_key,
                    src.path AS _caller_path, src.line_number AS _caller_line,
                    src.uid AS _caller_uid, dst.path AS _callee_path,
                    dst.line_number AS _callee_line, dst.uid AS _callee_uid,
                    edge.line_number AS _edge_line
                ORDER BY _caller_path, _caller_line, _caller_uid,
                    _callee_path, _callee_line, _callee_uid, _edge_line,
                    _edge_type, _edge_full_call_name, _edge_args_key
                SKIP $candidate_offset LIMIT $candidate_limit
                RETURN caller_name, caller_path, callee_name, callee_path, line
            """, function_name=function_name, path=path, repo_path=repo_path,
                candidate_offset=offset, candidate_limit=candidate_limit)
            rows = result.data()
        has_more = len(rows) > page_size
        edges = [
            {key: row.get(key) for key in (
                "caller_name", "caller_path", "callee_name", "callee_path", "line",
            )}
            for row in rows[:page_size]
        ]
        page_end = offset + len(edges)
        continuation_limit_reached = has_more and page_end >= _MAX_RELATIONSHIP_OFFSET
        next_offset = (
            page_end
            if has_more and not continuation_limit_reached
            else None
        )
        if return_metadata:
            return {
                "results": edges,
                "truncated": has_more,
                "offset": offset,
                "page_size": page_size,
                "result_limit": page_size,
                "next_offset": next_offset,
                "continuation_limit_reached": continuation_limit_reached,
            }
        return edges

    def find_all_callees(
        self, function_name: str, path: Optional[str] = None,
        repo_path: Optional[str] = None, depth: int = 3, offset: int = 0,
        page_size: int = _MAX_RELATIONSHIP_PAGE_SIZE,
        return_metadata: bool = False,
    ) -> List[Dict] | Dict[str, Any]:
        """Page distinct edges that start at *function_name* within *depth*."""
        if path:
            path = Path(path).as_posix()
        repo_path = self._normalize_repo_path_prefix(repo_path)
        if type(offset) is not int or not 0 <= offset < _MAX_RELATIONSHIP_OFFSET:
            raise ValueError("offset must be an integer from 0 through 99999")
        if type(page_size) is not int or page_size < 1:
            raise ValueError("page_size must be a positive integer")
        page_size = min(
            page_size,
            _MAX_RELATIONSHIP_PAGE_SIZE,
            max(1, _MAX_RELATIONSHIP_OFFSET - offset),
        )
        candidate_limit = page_size + 1
        depth = _sanitize_depth(depth)
        path_node_filter = (
            "AND all(path_node IN nodes(p) WHERE path_node.path STARTS WITH "
            f"{json.dumps(repo_path, ensure_ascii=False)})"
            if repo_path else ""
        )
        repo_filter = (
            "AND src.path STARTS WITH $repo_path "
            "AND dst.path STARTS WITH $repo_path "
            f"{path_node_filter}"
            if repo_path else ""
        )
        path_filter = "AND caller.path = $path" if path else ""
        with self.driver.session() as session:
            result = session.run(f"""
                MATCH (src)-[edge:CALLS|HEURISTIC_CALLS]->(dst)
                MATCH p = (caller:Function {{name: $function_name}})
                    -[:CALLS|HEURISTIC_CALLS*0..{depth - 1}]->(src)
                WHERE (dst:Function OR dst:Class) {path_filter} {repo_filter}
                WITH DISTINCT
                    src.name AS caller_name, src.path AS caller_path,
                    dst.name AS callee_name, dst.path AS callee_path,
                    edge.line_number AS line,
                    type(edge) AS _edge_type,
                    edge.full_call_name AS _edge_full_call_name,
                    edge.args_key AS _edge_args_key,
                    src.path AS _caller_path, src.line_number AS _caller_line,
                    src.uid AS _caller_uid, dst.path AS _callee_path,
                    dst.line_number AS _callee_line, dst.uid AS _callee_uid,
                    edge.line_number AS _edge_line
                ORDER BY _caller_path, _caller_line, _caller_uid,
                    _callee_path, _callee_line, _callee_uid, _edge_line,
                    _edge_type, _edge_full_call_name, _edge_args_key
                SKIP $candidate_offset LIMIT $candidate_limit
                RETURN caller_name, caller_path, callee_name, callee_path, line
            """, function_name=function_name, path=path, repo_path=repo_path,
                candidate_offset=offset, candidate_limit=candidate_limit)
            rows = result.data()
        has_more = len(rows) > page_size
        edges = [
            {key: row.get(key) for key in (
                "caller_name", "caller_path", "callee_name", "callee_path", "line",
            )}
            for row in rows[:page_size]
        ]
        page_end = offset + len(edges)
        continuation_limit_reached = has_more and page_end >= _MAX_RELATIONSHIP_OFFSET
        next_offset = (
            page_end
            if has_more and not continuation_limit_reached
            else None
        )
        if return_metadata:
            return {
                "results": edges,
                "truncated": has_more,
                "offset": offset,
                "page_size": page_size,
                "result_limit": page_size,
                "next_offset": next_offset,
                "continuation_limit_reached": continuation_limit_reached,
            }
        return edges

    def find_function_call_chain(
        self, start_function: str, end_function: str, max_depth: int = 5,
        start_file: Optional[str] = None, end_file: Optional[str] = None,
        repo_path: Optional[str] = None, limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[Dict]:
        """Find call chains between two functions"""
        max_depth = _sanitize_depth(max_depth)
        repo_path = self._normalize_repo_path_prefix(repo_path)
        with self.driver.session() as session:
            backend = getattr(self.db_manager, "get_backend_type", lambda: "neo4j")()
            if backend in ("kuzudb", "ladybugdb"):
                path_node_key = (
                    "CAST(list_transform(nodes(path), node -> node.uid) AS STRING)"
                )
                path_edge_key = (
                    "CAST(list_transform(relationships(path), call_rel -> id(call_rel)) AS STRING)"
                )
                path_end = "func_nodes[size(func_nodes)]"
            else:
                path_node_key = (
                    "reduce(node_key = '', path_node IN nodes(path) "
                    "| node_key + '|' + path_node.uid)"
                )
                path_edge_key = (
                    "reduce(edge_key = '', call_rel IN relationships(path) "
                    "| edge_key + '|' + toString(id(call_rel)))"
                )
                path_end = "last(func_nodes)"
            # Build match clauses based on whether files are specified
            start_props = "{name: $start_function" + (", path: $start_file}" if start_file else "}")
            end_props = "{name: $end_function" + (", path: $end_file}" if end_file else "}")

            # KùzuDB-compatible: Use anonymous end node and filter
            repo_filter = (
                "WHERE start.path STARTS WITH $repo_path AND end_target.path STARTS WITH $repo_path"
                if repo_path else ""
            )
            path_node_filter = (
                "AND all(path_node IN nodes(path) WHERE path_node.path STARTS WITH "
                f"{json.dumps(repo_path, ensure_ascii=False)})"
                if repo_path else ""
            )
            page_clause = (
                "SKIP $offset LIMIT $limit" if limit is not None
                else "SKIP $offset" if offset else ""
            )
            backend = str(
                getattr(self.db_manager, "get_backend_type", lambda: "neo4j")()
            ).lower()
            chain_preview_start = _cypher_substring_index_base(backend)
            if backend in {"kuzu", "kuzudb", "ladybug", "ladybugdb"}:
                function_nodes_projection = """
                    list_transform(func_nodes, chain_node -> {
                        uid: chain_node.uid,
                        name: substring(chain_node.name, $chain_preview_start, $chain_preview_char_limit),
                        name_total_chars: size(chain_node.name),
                        name_truncated: size(chain_node.name) > $chain_preview_char_limit,
                        path: substring(chain_node.path, $chain_preview_start, $chain_preview_char_limit),
                        path_total_chars: size(chain_node.path),
                        path_truncated: size(chain_node.path) > $chain_preview_char_limit,
                        line_number: chain_node.line_number,
                        is_dependency: chain_node.is_dependency
                    })
                """
                call_nodes_projection = """
                    list_transform(call_rels, chain_rel -> {
                        line_number: chain_rel.line_number,
                        full_call_name: substring(chain_rel.full_call_name, $chain_preview_start, $chain_preview_char_limit),
                        full_call_name_total_chars: size(chain_rel.full_call_name),
                        full_call_name_truncated: size(chain_rel.full_call_name) > $chain_preview_char_limit
                    })
                """
            else:
                function_nodes_projection = """
                    [chain_node IN func_nodes | {
                        uid: chain_node.uid,
                        name: substring(chain_node.name, $chain_preview_start, $chain_preview_char_limit),
                        name_total_chars: size(chain_node.name),
                        name_truncated: size(chain_node.name) > $chain_preview_char_limit,
                        path: substring(chain_node.path, $chain_preview_start, $chain_preview_char_limit),
                        path_total_chars: size(chain_node.path),
                        path_truncated: size(chain_node.path) > $chain_preview_char_limit,
                        line_number: chain_node.line_number,
                        is_dependency: chain_node.is_dependency
                    }]
                """
                call_nodes_projection = """
                    [chain_rel IN call_rels | {
                        line_number: chain_rel.line_number,
                        full_call_name: substring(chain_rel.full_call_name, $chain_preview_start, $chain_preview_char_limit),
                        full_call_name_total_chars: size(chain_rel.full_call_name),
                        full_call_name_truncated: size(chain_rel.full_call_name) > $chain_preview_char_limit
                    }]
                """
            function_nodes_projection = function_nodes_projection.replace(
                "$chain_preview_start", str(chain_preview_start)
            ).replace(
                "$chain_preview_char_limit", str(_EVIDENCE_PREVIEW_CHAR_LIMIT)
            )
            call_nodes_projection = call_nodes_projection.replace(
                "$chain_preview_start", str(chain_preview_start)
            ).replace(
                "$chain_preview_char_limit", str(_EVIDENCE_PREVIEW_CHAR_LIMIT)
            )
            query = f"""
                MATCH (start:Function {start_props}), (end_target:Function {end_props})
                {repo_filter}
                WITH start as start, end_target as end_target
                MATCH path = (start)-[:CALLS|HEURISTIC_CALLS*1..{max_depth}]->()
                WITH path as path, end_target as end_target,
                    nodes(path) as func_nodes, relationships(path) as call_rels,
                    {path_node_key} as path_node_key,
                    {path_edge_key} as path_edge_key
                WITH path as path, func_nodes as func_nodes, call_rels as call_rels,
                    end_target as end_target,
                    {path_end} as path_end,
                    path_node_key as path_node_key, path_edge_key as path_edge_key
                WHERE path_end.name = end_target.name
                  AND (end_target.path IS NULL OR path_end.path = end_target.path)
                  {path_node_filter}
                RETURN {function_nodes_projection} as function_nodes,
                    {call_nodes_projection} as call_nodes,
                    size(call_rels) as chain_length
                ORDER BY chain_length ASC, path_node_key, path_edge_key
                {page_clause}
            """
            
            # Prepare parameters
            params = {
                "start_function": start_function,
                "end_function": end_function,
                "start_file": start_file,
                "end_file": end_file,
                "repo_path": repo_path,
            }
            if limit is not None:
                params["limit"] = limit
            if limit is not None or offset:
                params["offset"] = offset
            
            result = session.run(query, **params)

            # Post-process Node/Rel objects into plain dicts so CLI output stays stable
            rows = result.data()
            transformed: List[Dict[str, Any]] = []
            for row in rows:
                func_nodes = row.get("function_nodes") or []
                rel_nodes = row.get("call_nodes") or []
                chain_len = row.get("chain_length", 0)

                function_chain = []
                # Repository scope was checked against every full path-node
                # path in the query before these bounded path previews were
                # projected. Rechecking a truncated preview here would reject
                # valid chains when the repository root exceeds the preview.
                for n in func_nodes:
                    # Depending on KùzuDB + driver wrapping, list elements can arrive
                    # either as Node/Rel objects or already-materialized dicts.
                    if isinstance(n, dict):
                        props = n
                    else:
                        props = None
                        try:
                            props = n.get_properties()
                        except Exception:
                            props = getattr(n, "properties", None)
                        if props is None:
                            props = {}
                    function_chain.append(
                        {
                            "uid": props.get("uid"),
                            "name": props.get("name"),
                            "name_total_chars": props.get("name_total_chars"),
                            "name_truncated": props.get("name_truncated", False),
                            "path": props.get("path"),
                            "path_total_chars": props.get("path_total_chars"),
                            "path_truncated": props.get("path_truncated", False),
                            "line_number": props.get("line_number"),
                            "is_dependency": props.get("is_dependency"),
                        }
                    )

                call_details = []
                for r in rel_nodes:
                    if isinstance(r, dict):
                        props = r
                    else:
                        props = None
                        try:
                            props = r.get_properties()
                        except Exception:
                            props = getattr(r, "properties", None)
                        if props is None:
                            props = {}
                    call_details.append(
                        {
                            "call_line": props.get("line_number"),
                            "full_call_name": props.get("full_call_name"),
                            "full_call_name_total_chars": props.get("full_call_name_total_chars"),
                            "full_call_name_truncated": props.get(
                                "full_call_name_truncated", False
                            ),
                        }
                    )

                transformed.append(
                    {
                        "function_chain": function_chain,
                        "call_details": call_details,
                        "chain_length": chain_len,
                    }
                )

            return transformed

    def find_by_type(
        self,
        element_type: str,
        limit: int = 50,
        repo_path: Optional[str] = None,
        offset: int = 0,
    ) -> List[Dict]:
        """Find all elements of a specific type (Function, Class, File, Module)."""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        # Map input type to node label
        type_map = {
            "function": "Function",
            "class": "Class",
            "file": "File",
            "module": "Module",
            "interface": "Interface",
            "trait": "Trait",
            "struct": "Struct",
            "enum": "Enum",
        }
        label = type_map.get(element_type.lower())
        
        if not label:
            return []
            
        with self.driver.session() as session:
            if label == "File":
                repo_filter = "WHERE n.path STARTS WITH $repo_path" if repo_path else ""
                query = f"""
                    MATCH (n:File)
                    {repo_filter}
                    RETURN n.name as name, n.path as path, n.is_dependency as is_dependency
                    ORDER BY n.path
                    SKIP $offset
                    LIMIT $limit
                """
            elif label == "Module":
                if repo_path:
                    query = """
                        MATCH (file:File)-[:IMPORTS]->(n:Module)
                        WHERE file.path STARTS WITH $repo_path
                        RETURN DISTINCT n.name as name, n.name as path, false as is_dependency
                        ORDER BY name
                        SKIP $offset
                        LIMIT $limit
                    """
                else:
                    query = """
                        MATCH (n:Module)
                        RETURN n.name as name, n.name as path, false as is_dependency
                        ORDER BY n.name
                        SKIP $offset
                        LIMIT $limit
                    """
            else:
                repo_filter = "WHERE n.path STARTS WITH $repo_path" if repo_path else ""
                query = f"""
                    MATCH (n:{label})
                    {repo_filter}
                    RETURN n.name as name, n.path as path, n.line_number as line_number, n.is_dependency as is_dependency
                    ORDER BY is_dependency ASC, name, n.path, n.line_number, n.uid
                    SKIP $offset
                    LIMIT $limit
                """
            
            result = session.run(query, limit=limit, repo_path=repo_path, offset=offset)
            return result.data()
    
    def find_module_dependencies(
        self, module_name: str, repo_path: Optional[str] = None,
        offset: int = 0, page_size: int = 50,
    ) -> Dict[str, Any]:
        """Find all dependencies and dependents of a module"""
        if type(offset) is not int or not 0 <= offset < _MAX_RELATIONSHIP_OFFSET:
            raise ValueError("offset must be an integer from 0 through 99999")
        if type(page_size) is not int or page_size < 1:
            raise ValueError("page_size must be a positive integer")
        page_size = min(
            page_size,
            _MAX_RELATIONSHIP_PAGE_SIZE,
            _MAX_RELATIONSHIP_OFFSET - offset,
        )
        candidate_limit = page_size + 1
        repo_path = self._normalize_repo_path_prefix(repo_path)
        with self.driver.session() as session:
            repo_filter = "AND file.path STARTS WITH $repo_path" if repo_path else ""
            backend = getattr(self.db_manager, "get_backend_type", lambda: "")()

            # KuzuDB is stricter about OPTIONAL MATCH variable scoping, and nested
            # repository ownership is already represented in the file path.
            if backend == "kuzudb":
                importers_result = session.run(f"""
                    MATCH (file:File)-[imp:IMPORTS]->(module:Module)
                    WHERE (module.name = $module_name OR module.full_import_name CONTAINS $module_name) {repo_filter}
                    RETURN DISTINCT
                        file.path as importer_file_path,
                        imp.line_number as import_line_number,
                        file.is_dependency as file_is_dependency,
                        '' as repository_name
                    ORDER BY file_is_dependency ASC, importer_file_path,
                        import_line_number
                    SKIP $offset LIMIT $limit
                """, module_name=module_name, repo_path=repo_path,
                    offset=offset, limit=candidate_limit)

                imports_result = session.run(f"""
                    MATCH (file:File)-[:IMPORTS]->(target_module:Module)
                    WHERE (target_module.name = $module_name OR target_module.full_import_name CONTAINS $module_name) {repo_filter}
                    WITH file, target_module
                    MATCH (file)-[imp:IMPORTS]->(other_module:Module)
                    WHERE other_module.name <> target_module.name
                    RETURN DISTINCT
                        other_module.name as imported_module,
                        imp.alias as import_alias
                    ORDER BY imported_module, import_alias
                    SKIP $offset LIMIT $limit
                """, module_name=module_name, repo_path=repo_path,
                    offset=offset, limit=candidate_limit)

                importer_rows = importers_result.data()
                import_rows = imports_result.data()
                importer_has_more = len(importer_rows) > page_size
                imports_has_more = len(import_rows) > page_size
                importer_page = importer_rows[:page_size]
                imports_page = import_rows[:page_size]
                has_more = importer_has_more or imports_has_more
                metadata = _relationship_page_metadata(
                    offset,
                    page_size,
                    page_size if has_more else max(len(importer_page), len(imports_page)),
                    has_more,
                )
                return {
                    "module_name": module_name,
                    "importers": importer_page,
                    "imports": imports_page,
                    "importers_truncated": importer_has_more,
                    "imports_truncated": imports_has_more,
                    **metadata,
                }

            # Find files that import this module (who imports this module)
            importers_result = session.run(f"""
                MATCH (file:File)-[imp:IMPORTS]->(module:Module {{name: $module_name}})
                WHERE 1=1 {repo_filter}
                OPTIONAL MATCH (repo:Repository)-[:CONTAINS]->(file)
                RETURN DISTINCT
                    file.path as importer_file_path,
                    imp.line_number as import_line_number,
                    file.is_dependency as file_is_dependency,
                    repo.name as repository_name
                ORDER BY file_is_dependency ASC, importer_file_path,
                    import_line_number
                SKIP $offset LIMIT $limit
            """, module_name=module_name, repo_path=repo_path,
                offset=offset, limit=candidate_limit)
            
            # Find modules that are imported by files that also import the target module
            # This helps understand what this module is typically used with
            imports_result = session.run(f"""
                MATCH (file:File)-[:IMPORTS]->(target_module:Module {{name: $module_name}})
                MATCH (file)-[imp:IMPORTS]->(other_module:Module)
                WHERE other_module <> target_module {repo_filter}
                RETURN DISTINCT
                    other_module.name as imported_module,
                    imp.alias as import_alias
                ORDER BY imported_module, import_alias
                SKIP $offset LIMIT $limit
            """, module_name=module_name, repo_path=repo_path,
                offset=offset, limit=candidate_limit)

            importer_rows = importers_result.data()
            import_rows = imports_result.data()
            importer_has_more = len(importer_rows) > page_size
            imports_has_more = len(import_rows) > page_size
            importer_page = importer_rows[:page_size]
            imports_page = import_rows[:page_size]
            has_more = importer_has_more or imports_has_more
            metadata = _relationship_page_metadata(
                offset,
                page_size,
                page_size if has_more else max(len(importer_page), len(imports_page)),
                has_more,
            )
            return {
                "module_name": module_name,
                "importers": importer_page,
                "imports": imports_page,
                "importers_truncated": importer_has_more,
                "imports_truncated": imports_has_more,
                **metadata,
            }
    
    def find_variable_usage_scope(
        self, variable_name: str, path: Optional[str] = None,
        repo_path: Optional[str] = None, offset: int = 0,
        page_size: int = 50, value_offset: int = 0,
        context_offset: int = 0,
        evidence_char_limit: int = _EVIDENCE_PREVIEW_CHAR_LIMIT,
    ) -> Dict[str, Any]:
        """Find the scope and usage patterns of a variable, optional file path filtering"""
        if type(offset) is not int or not 0 <= offset < _MAX_RELATIONSHIP_OFFSET:
            raise ValueError("offset must be an integer from 0 through 99999")
        if type(page_size) is not int or page_size < 1:
            raise ValueError("page_size must be a positive integer")
        page_size = min(
            page_size,
            _MAX_RELATIONSHIP_PAGE_SIZE,
            _MAX_RELATIONSHIP_OFFSET - offset,
        )
        value_offset, evidence_char_limit = _evidence_page_inputs(
            value_offset,
            evidence_char_limit,
            "value_offset",
            "evidence_char_limit",
        )
        context_offset, evidence_char_limit = _evidence_page_inputs(
            context_offset,
            evidence_char_limit,
            "context_offset",
            "evidence_char_limit",
        )
        candidate_limit = offset + page_size + 1
        repo_path = self._normalize_repo_path_prefix(repo_path)
        substring_index_base = _cypher_substring_index_base(
            getattr(self.db_manager, "get_backend_type", lambda: "neo4j")()
        )
        with self.driver.session() as session:
            repo_filter = "AND var.path STARTS WITH $repo_path" if repo_path else ""
            path_filter = "(var.path ENDS WITH $path OR var.path = $path)" if path else "1=1"
            evidence_params = {
                "value_offset": value_offset,
                "value_start": value_offset + substring_index_base,
                "context_offset": context_offset,
                "context_start": context_offset + substring_index_base,
                "evidence_char_limit": evidence_char_limit,
            }

            # Two-pass approach for KuzuDB compatibility (doesn't support
            # OPTIONAL MATCH referencing variables bound in a prior MATCH).
            # Pass 1: variables WITH a container
            contained = session.run(f"""
                MATCH (container)-[:CONTAINS]->(var:Variable {{name: $variable_name}})
                WHERE {path_filter} {repo_filter}
                RETURN DISTINCT
                    var.name as variable_name,
                    substring(var.value, $value_start, $evidence_char_limit) as variable_value,
                    size(var.value) as _variable_value_total_chars,
                    var.line_number as line_number,
                    substring(var.context, $context_start, $evidence_char_limit) as context,
                    size(var.context) as _context_total_chars,
                    var.path as path,
                    CASE
                        WHEN container:Function THEN 'function'
                        WHEN container:Class THEN 'class'
                        ELSE 'module'
                    END as scope_type,
                    CASE
                        WHEN container:Function THEN container.name
                        WHEN container:Class THEN container.name
                        ELSE 'module_level'
                    END as scope_name,
                    var.is_dependency as is_dependency,
                    var.uid as _sort_uid
                ORDER BY is_dependency ASC, path, line_number,
                    scope_type, scope_name, _sort_uid
                LIMIT $candidate_limit
            """, variable_name=variable_name, path=path, repo_path=repo_path,
                candidate_limit=candidate_limit, **evidence_params)
            instances = contained.data()

            # Pass 2: variables WITHOUT any container (module-level)
            try:
                orphaned = session.run(f"""
                    MATCH (var:Variable {{name: $variable_name}})
                    WHERE {path_filter} {repo_filter}
                      AND NOT ()-[:CONTAINS]->(var)
                    RETURN DISTINCT
                        var.name as variable_name,
                        substring(var.value, $value_start, $evidence_char_limit) as variable_value,
                        size(var.value) as _variable_value_total_chars,
                        var.line_number as line_number,
                        substring(var.context, $context_start, $evidence_char_limit) as context,
                        size(var.context) as _context_total_chars,
                        var.path as path,
                        'module' as scope_type,
                        'module_level' as scope_name,
                        var.is_dependency as is_dependency,
                        var.uid as _sort_uid
                    ORDER BY is_dependency ASC, path, line_number, _sort_uid
                    LIMIT $candidate_limit
                """, variable_name=variable_name, path=path, repo_path=repo_path,
                    candidate_limit=candidate_limit, **evidence_params)
                instances.extend(orphaned.data())
            except Exception:
                pass

            instances.sort(key=lambda r: (
                r.get("is_dependency") or False,
                r.get("path") or "",
                r.get("line_number") or 0,
                r.get("scope_type") or "",
                r.get("scope_name") or "",
                r.get("_sort_uid") or "",
            ))
            has_more = len(instances) > offset + page_size
            page = instances[offset:offset + page_size]
            for row in page:
                _add_evidence_page_metadata(
                    row,
                    "variable_value",
                    "_variable_value_total_chars",
                    value_offset,
                )
                _add_evidence_page_metadata(
                    row, "context", "_context_total_chars", context_offset
                )
                row.pop("_sort_uid", None)
            metadata = _relationship_page_metadata(
                offset, page_size, len(page), has_more
            )
            return {
                "variable_name": variable_name,
                "instances": page,
                **metadata,
            }

    def analyze_code_relationships(
        self, query_type: str, target: str, context: Optional[str] = None,
        repo_path: Optional[str] = None, depth: Optional[int] = None,
        graph_name: str = None, offset: int = 0,
        page_size: int = _MAX_RELATIONSHIP_PAGE_SIZE,
        docstring_offset: int = 0,
        docstring_char_limit: int = _EVIDENCE_PREVIEW_CHAR_LIMIT,
        value_offset: int = 0,
        context_offset: int = 0,
        evidence_char_limit: int = _EVIDENCE_PREVIEW_CHAR_LIMIT,
        decorator_offset: int = 0,
        decorator_page_size: int = _RELATIONSHIP_ARGUMENT_MAX_ITEMS,
        decorator_value_offset: int = 0,
        decorator_value_page_size: int = _RELATIONSHIP_ARGUMENT_MAX_CHARS,
    ) -> Dict[str, Any]:
        self._active_graph = graph_name
        """Main method to analyze different types of code relationships with fixed return types"""
        query_type = query_type.lower().strip()
        
        # Use depth if provided, otherwise default to 3 for 'all' queries
        effective_depth = depth if depth is not None else 3
        
        try:
            if query_type == "find_callers":
                req_limit = _direct_relationship_page_size(
                    "find_callers", offset, page_size
                )
                raw_results = self.who_calls_function(
                    target,
                    context,
                    repo_path=repo_path,
                    limit=req_limit + 1,
                    offset=offset,
                )
                truncated = len(raw_results) > req_limit
                results = raw_results[:req_limit]
                page_end = offset + len(results)
                continuation_limit_reached = (
                    truncated and page_end >= _MAX_RELATIONSHIP_OFFSET
                )
                next_offset = (
                    page_end
                    if truncated and not continuation_limit_reached
                    else None
                )

                target_paths = {r.get("target_file_path") for r in results}
                target_file_path = target_paths.pop() if len(target_paths) == 1 else None
                if target_file_path is not None:
                    for r in results:
                        r.pop("target_file_path", None)

                summary = f"Found {len(results)} functions that call '{target}'"
                if truncated:
                    summary += " (truncated — more exist)"

                envelope = {
                    "query_type": "find_callers", "target": target, "context": context,
                    "target_file_path": target_file_path, "results": results,
                    "summary": summary, "truncated": truncated,
                    "offset": offset, "page_size": req_limit,
                    "result_limit": req_limit, "next_offset": next_offset,
                    "continuation_limit_reached": continuation_limit_reached,
                }
                if target_file_path is None and len(target_paths) > 1:
                    envelope["note"] = (
                        f"'{target}' is defined in {len(target_paths)} files; each result "
                        "carries its own target_file_path. Pass `context` to disambiguate."
                    )
                return envelope
            
            elif query_type == "find_callees":
                req_limit = _direct_relationship_page_size(
                    "find_callees", offset, page_size
                )
                raw_results = self.what_does_function_call(
                    target,
                    context,
                    repo_path=repo_path,
                    limit=req_limit + 1,
                    offset=offset,
                    docstring_offset=docstring_offset,
                    docstring_char_limit=docstring_char_limit,
                )
                truncated = len(raw_results) > req_limit
                results = raw_results[:req_limit]
                page_end = offset + len(results)
                continuation_limit_reached = (
                    truncated and page_end >= _MAX_RELATIONSHIP_OFFSET
                )
                next_offset = (
                    page_end
                    if truncated and not continuation_limit_reached
                    else None
                )

                summary = f"Function '{target}' calls {len(results)} other functions"
                if truncated:
                    summary += " (truncated — more exist)"

                return {
                    "query_type": "find_callees", "target": target, "context": context, "results": results,
                    "summary": summary, "truncated": truncated,
                    "offset": offset, "page_size": req_limit,
                    "result_limit": req_limit, "next_offset": next_offset,
                    "continuation_limit_reached": continuation_limit_reached,
                }
                
            elif query_type == "find_importers":
                req_limit = _direct_relationship_page_size(
                    "find_importers", offset, page_size
                )
                raw_results = self.who_imports_module(
                    target, repo_path=repo_path, limit=req_limit + 1, offset=offset
                )
                truncated = len(raw_results) > req_limit
                results = raw_results[:req_limit]

                summary = f"Found {len(results)} files that import '{target}'"
                if truncated:
                    summary += " (truncated — more exist)"

                return {
                    "query_type": "find_importers", "target": target, "results": results,
                    "summary": summary,
                    **_relationship_page_metadata(
                        offset, req_limit, len(results), truncated
                    ),
                }
                
            elif query_type == "find_functions_by_argument":
                req_limit = _direct_relationship_page_size(
                    "find_functions_by_argument", offset, page_size
                )
                raw_results = self.find_functions_by_argument(
                    target, context, repo_path=repo_path,
                    limit=req_limit + 1, offset=offset,
                )
                truncated = len(raw_results) > req_limit
                results = raw_results[:req_limit]

                summary = f"Found {len(results)} functions that take '{target}' as an argument"
                if truncated:
                    summary += " (truncated — more exist)"

                return {
                    "query_type": "find_functions_by_argument", "target": target, "context": context, "results": results,
                    "summary": summary,
                    **_relationship_page_metadata(
                        offset, req_limit, len(results), truncated
                    ),
                }
            
            elif query_type == "find_functions_by_decorator":
                req_limit = _direct_relationship_page_size(
                    "find_functions_by_decorator", offset, page_size
                )
                raw_results = self.find_functions_by_decorator(
                    target, context, repo_path=repo_path,
                    limit=req_limit + 1, offset=offset,
                    decorator_offset=decorator_offset,
                    decorator_page_size=decorator_page_size,
                    decorator_value_offset=decorator_value_offset,
                    decorator_value_page_size=decorator_value_page_size,
                )
                truncated = len(raw_results) > req_limit
                results = raw_results[:req_limit]

                summary = f"Found {len(results)} functions decorated with '{target}'"
                if truncated:
                    summary += " (truncated — more exist)"

                return {
                    "query_type": "find_functions_by_decorator", "target": target, "context": context, "results": results,
                    "summary": summary,
                    **_relationship_page_metadata(
                        offset, req_limit, len(results), truncated
                    ),
                }
                
            elif query_type in ["who_modifies", "modifies", "mutations", "changes", "variable_usage"]:
                req_limit = _direct_relationship_page_size(
                    "who_modifies", offset, page_size
                )
                raw_results = self.who_modifies_variable(
                    target, repo_path=repo_path, limit=req_limit + 1,
                    offset=offset,
                )
                truncated = len(raw_results) > req_limit
                results = raw_results[:req_limit]

                summary = f"Found {len(results)} containers that hold variable '{target}'"
                if truncated:
                    summary += " (truncated — more exist)"

                return {
                    "query_type": "who_modifies", "target": target, "results": results,
                    "summary": summary,
                    **_relationship_page_metadata(
                        offset, req_limit, len(results), truncated
                    ),
                }
            
            elif query_type in ["class_hierarchy", "inheritance", "extends"]:
                results = self.find_class_hierarchy(
                    target,
                    context,
                    repo_path=repo_path,
                    offset=offset,
                    page_size=page_size,
                )
                summary = (
                    f"Class '{target}' page contains "
                    f"{len(results['parent_classes'])} parents, "
                    f"{len(results['child_classes'])} children, and "
                    f"{len(results['methods'])} methods"
                )
                if results["truncated"]:
                    summary += " (truncated — more results remain)"
                return {
                    "query_type": "class_hierarchy", "target": target, "results": results,
                    "summary": summary,
                    "truncated": results["truncated"],
                    "offset": results["offset"],
                    "page_size": results["page_size"],
                    "result_limit": results["result_limit"],
                    "next_offset": results["next_offset"],
                    "continuation_limit_reached": results[
                        "continuation_limit_reached"
                    ],
                }
            
            elif query_type in ["overrides", "implementations", "polymorphism"]:
                req_limit = _direct_relationship_page_size(
                    "overrides", offset, page_size
                )
                raw_results = self.find_function_overrides(
                    target, repo_path=repo_path, limit=req_limit + 1,
                    offset=offset,
                )
                truncated = len(raw_results) > req_limit
                results = raw_results[:req_limit]

                summary = f"Found {len(results)} implementations of function '{target}'"
                if truncated:
                    summary += " (truncated — more exist)"

                return {
                    "query_type": "overrides", "target": target, "results": results,
                    "summary": summary,
                    **_relationship_page_metadata(
                        offset, req_limit, len(results), truncated
                    ),
                }
            
            elif query_type in ["dead_code", "unused", "unreachable"]:
                req_limit = _direct_relationship_page_size(
                    "find_dead_code", offset, page_size
                )
                # graph_name must be forwarded: find_dead_code defaults it to None and
                # re-assigns the shared self._active_graph, so omitting it here
                # silently redirected the query to the default graph.
                raw_results = self.find_dead_code(
                    repo_path=repo_path, graph_name=graph_name,
                    limit=req_limit + 1, offset=offset,
                )
                unused = raw_results.get("potentially_unused_functions", [])
                page = unused[:req_limit]
                has_more = (
                    len(unused) > req_limit
                    or offset + len(page) < raw_results.get("total_count", 0)
                )
                results = {
                    **raw_results,
                    "potentially_unused_functions": page,
                    **_relationship_page_metadata(
                        offset, req_limit, len(page), has_more
                    ),
                }
                return {
                    "query_type": "dead_code", "results": results,
                    "summary": f"Found {len(page)} potentially unused functions",
                    **_relationship_page_metadata(
                        offset, req_limit, len(page), has_more
                    ),
                }
            
            elif query_type == "find_complexity":
                limit_val = context if context else target
                requested_limit = (
                    int(limit_val)
                    if limit_val and str(limit_val).isdigit()
                    else 10
                )
                requested_limit = min(
                    requested_limit, _MAX_RELATIONSHIP_OFFSET
                )
                requested_page_size = _direct_relationship_page_size(
                    "find_complexity", offset, page_size
                )
                remaining = max(0, requested_limit - offset)
                effective_page_size = min(
                    requested_page_size, remaining
                ) if remaining else requested_page_size
                if remaining:
                    raw_results = self.find_most_complex_functions(
                        effective_page_size + 1,
                        repo_path=repo_path,
                        graph_name=graph_name,
                        offset=offset,
                    )
                    has_more = len(raw_results) > effective_page_size
                    results = raw_results[:effective_page_size]
                else:
                    has_more = False
                    results = []
                page_end = offset + len(results)
                continuation_limit_reached = bool(
                    has_more and page_end >= requested_limit
                )
                next_offset = (
                    page_end
                    if has_more
                    and not continuation_limit_reached
                    and page_end < _MAX_RELATIONSHIP_OFFSET
                    else None
                )
                summary = f"Found {len(results)} most complex functions"
                if has_more:
                    summary += " (truncated — more results remain)"
                return {
                    "query_type": "find_complexity",
                    "limit": requested_limit,
                    "results": results,
                    "summary": summary,
                    "truncated": has_more,
                    "offset": offset,
                    "page_size": effective_page_size,
                    "result_limit": effective_page_size,
                    "next_offset": next_offset,
                    "continuation_limit_reached": continuation_limit_reached,
                }
            
            elif query_type == "find_all_callers":
                page = self.find_all_callers(
                    target, context, repo_path=repo_path, depth=effective_depth,
                    offset=offset, page_size=page_size, return_metadata=True,
                )
                results = page["results"]
                summary = f"Found {len(results)} direct and indirect callers of '{target}' (depth: {effective_depth})"
                if page["truncated"]:
                    summary += " (truncated — more candidates remain)"
                return {
                    "query_type": "find_all_callers", "target": target, "context": context, "results": results, "depth": effective_depth,
                    "summary": summary, "truncated": page["truncated"],
                    "offset": page["offset"], "page_size": page["page_size"],
                    "result_limit": page["result_limit"],
                    "next_offset": page["next_offset"],
                    "continuation_limit_reached": page[
                        "continuation_limit_reached"
                    ],
                }
 
            elif query_type == "find_all_callees":
                page = self.find_all_callees(
                    target, context, repo_path=repo_path, depth=effective_depth,
                    offset=offset, page_size=page_size, return_metadata=True,
                )
                results = page["results"]
                summary = f"Found {len(results)} direct and indirect callees of '{target}' (depth: {effective_depth})"
                if page["truncated"]:
                    summary += " (truncated — more candidates remain)"
                return {
                    "query_type": "find_all_callees", "target": target, "context": context, "results": results, "depth": effective_depth,
                    "summary": summary, "truncated": page["truncated"],
                    "offset": page["offset"], "page_size": page["page_size"],
                    "result_limit": page["result_limit"],
                    "next_offset": page["next_offset"],
                    "continuation_limit_reached": page[
                        "continuation_limit_reached"
                    ],
                }
                
            elif query_type in ["call_chain", "path", "chain"]:
                if '->' in target:
                    start_func, end_func = target.split('->', 1)
                    max_depth = int(context) if context and str(context).isdigit() else 5
                    req_limit = _direct_relationship_page_size(
                        "call_chain", offset, page_size
                    )
                    raw_results = self.find_function_call_chain(
                        start_func.strip(), end_func.strip(), max_depth,
                        repo_path=repo_path, limit=req_limit + 1,
                        offset=offset,
                    )
                    truncated = len(raw_results) > req_limit
                    results = raw_results[:req_limit]

                    summary = f"Found {len(results)} call chains from '{start_func.strip()}' to '{end_func.strip()}' (max depth: {max_depth})"
                    if truncated:
                        summary += " (truncated — more exist)"

                    return {
                        "query_type": "call_chain", "target": target, "results": results,
                        "summary": summary,
                        **_relationship_page_metadata(
                            offset, req_limit, len(results), truncated
                        ),
                    }
                else:
                    return {
                        "error": "For call_chain queries, use format 'start_function->end_function'",
                        "example": "main->process_data"
                    }
            
            elif query_type in ["module_deps", "module_dependencies", "module_usage"]:
                req_limit = _direct_relationship_page_size(
                    "module_deps", offset, page_size
                )
                results = self.find_module_dependencies(
                    target, repo_path=repo_path, offset=offset,
                    page_size=req_limit,
                )
                return {
                    "query_type": "module_dependencies", "target": target, "results": results,
                    "summary": f"Module '{target}' is imported by {len(results['importers'])} files",
                    **{
                        key: results[key]
                        for key in (
                            "offset", "page_size", "result_limit", "truncated",
                            "next_offset", "continuation_limit_reached",
                        )
                    },
                }
            
            elif query_type in ["variable_scope", "var_scope", "variable_usage_scope"]:
                req_limit = _direct_relationship_page_size(
                    "variable_scope", offset, page_size
                )
                results = self.find_variable_usage_scope(
                    target, repo_path=repo_path, offset=offset,
                    page_size=req_limit, value_offset=value_offset,
                    context_offset=context_offset,
                    evidence_char_limit=evidence_char_limit,
                )
                return {
                    "query_type": "variable_scope", "target": target, "results": results,
                    "summary": f"Variable '{target}' has {len(results['instances'])} instances across different scopes",
                    **{
                        key: results[key]
                        for key in (
                            "offset", "page_size", "result_limit", "truncated",
                            "next_offset", "continuation_limit_reached",
                        )
                    },
                }
            
            else:
                return {
                    "error": f"Unknown query type: {query_type}",
                    "supported_types": [
                        "find_callers", "find_callees", "find_importers", "who_modifies",
                        "class_hierarchy", "overrides", "dead_code", "call_chain",
                        "module_deps", "variable_scope", "find_complexity"
                    ]
                }
        
        except Exception as e:
            return {
                "error": f"Error executing relationship query: {str(e)}",
                "query_type": query_type,
                "target": target
            }

    def get_cyclomatic_complexity(self, function_name: str, path: Optional[str] = None, repo_path: Optional[str] = None, graph_name: str = None) -> Optional[Dict]:
        self._active_graph = graph_name
        """Get the cyclomatic complexity of a function."""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        with self.driver.session() as session:
            repo_filter = "AND f.path STARTS WITH $repo_path" if repo_path else ""
            if path:
                # Use ENDS WITH for flexible path matching, or exact match
                query = f"""
                    MATCH (f:Function {{name: $function_name}})
                    WHERE (f.path ENDS WITH $path OR f.path = $path) {repo_filter}
                    RETURN f.name as function_name, f.cyclomatic_complexity as complexity,
                           f.path as path, f.line_number as line_number
                """
                result = session.run(query, function_name=function_name, path=path, repo_path=repo_path)
            else:
                query = f"""
                    MATCH (f:Function {{name: $function_name}})
                    WHERE 1=1 {repo_filter}
                    RETURN f.name as function_name, f.cyclomatic_complexity as complexity,
                           f.path as path, f.line_number as line_number
                """
                result = session.run(query, function_name=function_name, repo_path=repo_path)
            
            result_data = result.data()
            if result_data:
                return result_data[0]
            return None

    def find_most_complex_functions(
        self,
        limit: Optional[int] = 10,
        repo_path: Optional[str] = None,
        graph_name: str = None,
        offset: int = 0,
    ) -> List[Dict]:
        self._active_graph = graph_name
        """Find the most complex functions based on cyclomatic complexity.

        ``limit=None`` returns the full set — CI enforcement must not have its
        violation count truncated by a display page (#1333).
        """
        repo_path = self._normalize_repo_path_prefix(repo_path)
        with self.driver.session() as session:
            repo_filter = "AND f.path STARTS WITH $repo_path" if repo_path else ""
            path_ignore = cypher_path_not_under_ignore_dirs("f.path")
            offset_clause = "SKIP $offset" if offset else ""
            limit_clause = "LIMIT $limit" if limit is not None else ""
            params = {"repo_path": repo_path}
            if offset_clause:
                params["offset"] = offset
            if limit is not None:
                params["limit"] = limit
            query = f"""
                MATCH (f:Function)
                WHERE f.cyclomatic_complexity IS NOT NULL AND f.is_dependency = false {repo_filter} {path_ignore}
                RETURN f.name as function_name, f.path as path,
                       f.cyclomatic_complexity as complexity,
                       f.line_number as line_number, f.uid as _sort_uid
                ORDER BY f.cyclomatic_complexity DESC, f.path, f.line_number, _sort_uid
                {offset_clause} {limit_clause}
            """
            result = session.run(query, **params)
            rows = result.data()
            for row in rows:
                row.pop("_sort_uid", None)
            return rows

    def find_most_complex_functions_in_file(self, file_path: str, limit: Optional[int] = 20, repo_path: Optional[str] = None) -> List[Dict]:
        """Find the most complex functions in a specific file (limit=None = all)."""
        repo_path = self._normalize_repo_path_prefix(repo_path)
        with self.driver.session() as session:
            repo_filter = "AND f.path STARTS WITH $repo_path" if repo_path else ""
            limit_clause = "LIMIT $limit" if limit is not None else ""
            params = {"file_path": file_path, "repo_path": repo_path}
            if limit is not None:
                params["limit"] = limit
            query = f"""
                MATCH (f:Function)
                WHERE f.cyclomatic_complexity IS NOT NULL
                  AND (f.path ENDS WITH $file_path OR f.path = $file_path)
                  {repo_filter}
                RETURN f.name as function_name, f.path as path,
                       f.cyclomatic_complexity as complexity, f.line_number as line_number
                ORDER BY f.cyclomatic_complexity DESC
                {limit_clause}
            """
            result = session.run(query, **params)
            return result.data()

    def list_indexed_repositories(self, graph_name: str = None) -> List[Dict]:
        self._active_graph = graph_name
        """List all indexed repositories."""
        # The list itself is the readiness query; don't reject it because
        # another repository in the graph is still building.
        driver = self.db_manager.get_driver(self._active_graph)
        with driver.session() as session:
            result = session.run("""
                MATCH (r:Repository)
                WHERE r.index_state IS NULL OR r.index_state = 'ready'
                RETURN r.name as name, r.path as path, r.is_dependency as is_dependency
                ORDER BY r.name
            """)
            rows = result.data()
            bad = [r for r in rows if r.get("path") in (None, "")]
            if bad:
                logger.warning(
                    "Found %s Repository record(s) with missing path in the graph; "
                    "they are ignored when matching filesystem paths. If this persists, "
                    "remove stale Repository nodes (e.g. Neo4j: "
                    "MATCH (r:Repository) WHERE r.path IS NULL DETACH DELETE r) and re-index.",
                    len(bad),
                )
            return rows
