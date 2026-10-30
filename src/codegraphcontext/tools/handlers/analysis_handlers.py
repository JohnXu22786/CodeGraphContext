# src/codegraphcontext/tools/handlers/analysis_handlers.py
from typing import Any, Dict
from ..code_finder import CodeFinder
from ...utils.debug_log import debug_log
from ...utils.tool_limits import get_tool_result_limit


_FIND_CODE_DEFAULT_PAGE_SIZE = 3
_FIND_CODE_MAX_PAGE_SIZE = 100
_FIND_CODE_MAX_OFFSET = 100_000
_FIND_CODE_MAX_SOURCE_CHARS = 1536
_FIND_CODE_MAX_SOURCE_LINES = 100


def _require_integer_argument(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer.")
    return value


def _bounded_page_inputs(
    args: Dict[str, Any],
    *,
    offset_key: str = "offset",
    page_size_key: str = "page_size",
    default_page_size: int = _FIND_CODE_MAX_PAGE_SIZE,
):
    try:
        offset = _require_integer_argument(args.get(offset_key, 0), offset_key)
        requested_page_size = _require_integer_argument(
            args.get(page_size_key, default_page_size), page_size_key
        )
    except (TypeError, ValueError) as exc:
        return None, None, {"error": str(exc)}

    if offset < 0 or offset >= _FIND_CODE_MAX_OFFSET or requested_page_size < 1:
        return None, None, {
            "error": (
                f"{offset_key} must be between 0 and {_FIND_CODE_MAX_OFFSET - 1} "
                f"and {page_size_key} must be a positive integer."
            )
        }

    page_size = min(
        requested_page_size,
        _FIND_CODE_MAX_PAGE_SIZE,
        _FIND_CODE_MAX_OFFSET - offset,
    )
    return offset, page_size, None


def _page_rows(rows, offset: int, page_size: int):
    candidates = list(rows)
    page = candidates[:page_size]
    page_end = offset + len(page)
    has_more = len(candidates) > page_size
    continuation_limit_reached = has_more and page_end >= _FIND_CODE_MAX_OFFSET
    return page, {
        "offset": offset,
        "page_size": page_size,
        "result_limit": page_size,
        "truncated": has_more,
        "next_offset": (
            page_end if has_more and not continuation_limit_reached else None
        ),
        "continuation_limit_reached": continuation_limit_reached,
    }


def _find_code_excerpt(text: Any, query: str, limit: int = 240) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    terms = [term.casefold() for term in query.split() if term]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        lines = [text]
    line = next(
        (candidate for candidate in lines if any(term in candidate.casefold() for term in terms)),
        lines[0],
    )
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _find_code_text_page(
    text: Any,
    offset: int,
    char_limit: int,
    line_limit: int,
    *,
    total_chars: int | None = None,
) -> tuple[str, int | None, int]:
    if not isinstance(text, str):
        text = ""
    full_text_total = len(text) if total_chars is None else max(0, total_chars)
    start = min(offset, full_text_total)
    # Database hydration may already return only the requested slice. In that
    # case `offset` is the position of the fragment in the full value.
    page_start = start if total_chars is None else 0
    page = text[page_start : page_start + char_limit]
    lines = page.splitlines(keepends=True)
    if len(lines) > line_limit:
        page = "".join(lines[:line_limit])
    next_offset = start + len(page)
    return page, (next_offset if next_offset < full_text_total else None), full_text_total


def _find_code_result_item(
    item: Dict[str, Any],
    query: str,
    *,
    include_source: bool = False,
    include_docstring: bool = False,
    source_offset: int = 0,
    docstring_offset: int = 0,
    char_limit: int = 1024,
    docstring_char_limit: int = 1024,
    line_limit: int = 20,
) -> Dict[str, Any]:
    source = item.get("_source_preview", item.get("source"))
    docstring = item.get("_docstring_preview", item.get("docstring"))
    evidence_text = {"source": source, "docstring": docstring}
    for key in ("context", "value"):
        evidence_text[key] = item.get(f"_{key}_preview", item.get(key))
    concise = {
        key: value
        for key, value in item.items()
        if not key.startswith("_")
        if key not in (
            "source",
            "docstring",
            "context",
            "value",
            "_sort_uid",
            "_match_score",
        )
    }
    for key, text in evidence_text.items():
        total_chars = item.get(f"_{key}_total_chars")
        concise[f"{key}_available"] = (
            total_chars > 0 if isinstance(total_chars, int) and not isinstance(total_chars, bool)
            else bool(text)
        )
        if text:
            concise[f"{key}_excerpt"] = _find_code_excerpt(text, query)

    if include_source:
        source_total_chars = item.get("_source_total_chars")
        source_page = item.get("_source_page", item.get("source"))
        page, next_offset, total_chars = _find_code_text_page(
            source_page,
            source_offset,
            char_limit,
            line_limit,
            total_chars=(
                source_total_chars
                if isinstance(source_total_chars, int) and not isinstance(source_total_chars, bool)
                else None
            ),
        )
        concise.update(
            source=page,
            source_offset=min(source_offset, total_chars),
            source_total_chars=total_chars,
            source_next_offset=next_offset,
            source_truncated=next_offset is not None,
        )
    if include_docstring:
        docstring_total_chars = item.get("_docstring_total_chars")
        docstring_page = item.get("_docstring_page", item.get("docstring"))
        page, next_offset, total_chars = _find_code_text_page(
            docstring_page,
            docstring_offset,
            docstring_char_limit,
            line_limit,
            total_chars=(
                docstring_total_chars
                if isinstance(docstring_total_chars, int) and not isinstance(docstring_total_chars, bool)
                else None
            ),
        )
        concise.update(
            docstring=page,
            docstring_offset=min(docstring_offset, total_chars),
            docstring_total_chars=total_chars,
            docstring_next_offset=next_offset,
            docstring_truncated=next_offset is not None,
        )
    return concise


def _find_code_category_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Keep category summaries small; ranked results carry the evidence excerpt."""
    return {
        key: item[key]
        for key in (
            "type", "name", "path", "relative_path", "line_number", "end_line",
            "is_dependency", "confidence_label", "evidence_kind",
        )
        if key in item
    }


def find_dead_code(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Tool to find potentially dead code across the entire project."""
    exclude_decorated_with = args.get("exclude_decorated_with", [])
    repo_path = args.get("repo_path")
    graph_name = args.get("graph_name")
    try:
        offset = _require_integer_argument(args.get("offset", 0), "offset")
        configured_limit = get_tool_result_limit("find_dead_code", default=50) or 50
        requested_page_size = _require_integer_argument(
            args.get("page_size", configured_limit), "page_size"
        )
        if offset < 0 or offset >= _FIND_CODE_MAX_OFFSET or requested_page_size < 1:
            raise ValueError
    except (TypeError, ValueError):
        return {
            "error": (
                "offset must be between 0 and 99999 and page_size must be a positive integer."
            )
        }

    page_size = min(
        requested_page_size,
        configured_limit,
        _FIND_CODE_MAX_PAGE_SIZE,
        _FIND_CODE_MAX_OFFSET - offset,
    )
    try:
        debug_log(f"Finding dead code. repo_path={repo_path}")
        results = code_finder.find_dead_code(
            exclude_decorated_with=exclude_decorated_with,
            repo_path=repo_path,
            graph_name=graph_name,
            limit=page_size + 1,
            offset=offset,
        )
        unused = results.get("potentially_unused_functions", [])
        page = unused[:page_size]
        page_end = offset + len(page)
        has_more = (
            len(unused) > page_size
            or page_end < results.get("total_count", 0)
        )
        continuation_limit_reached = has_more and page_end >= _FIND_CODE_MAX_OFFSET
        next_offset = (
            page_end if has_more and not continuation_limit_reached else None
        )
        page_results = {
            **results,
            "potentially_unused_functions": page,
            "offset": offset,
            "page_size": page_size,
            "result_limit": page_size,
            "truncated": has_more,
            "next_offset": next_offset,
            "continuation_limit_reached": continuation_limit_reached,
        }

        return {
            "success": True,
            "query_type": "dead_code",
            "results": page_results,
            "offset": offset,
            "page_size": page_size,
            "result_limit": page_size,
            "truncated": has_more,
            "next_offset": next_offset,
            "continuation_limit_reached": continuation_limit_reached,
        }
    except Exception as e:
        debug_log(f"Error finding dead code: {str(e)}")
        return {"error": f"Failed to find dead code: {str(e)}"}


def calculate_cyclomatic_complexity(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Tool to calculate cyclomatic complexity for a given function."""
    function_name = args.get("function_name")
    path = args.get("path")
    repo_path = args.get("repo_path")
    graph_name = args.get("graph_name")

    try:
        debug_log(f"Calculating cyclomatic complexity for function: {function_name}, repo_path={repo_path}")
        results = code_finder.get_cyclomatic_complexity(function_name, path, repo_path=repo_path, graph_name=graph_name)

        response = {
            "success": True,
            "function_name": function_name,
            "results": results
        }
        if path:
            response["path"] = path

        return response
    except Exception as e:
        debug_log(f"Error calculating cyclomatic complexity: {str(e)}")
        return {"error": f"Failed to calculate cyclomatic complexity: {str(e)}"}


def find_most_complex_functions(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Tool to find the most complex functions."""
    limit = get_tool_result_limit("find_most_complex_functions", default=args.get("limit", 10))
    repo_path = args.get("repo_path")
    graph_name = args.get("graph_name")
    try:
        debug_log(f"Finding the top {limit} most complex functions. repo_path={repo_path}")
        results = code_finder.find_most_complex_functions(limit, repo_path=repo_path, graph_name=graph_name)
        return {
            "success": True,
            "limit": limit,
            "results": results
        }
    except Exception as e:
        debug_log(f"Error finding most complex functions: {str(e)}")
        return {"error": f"Failed to find most complex functions: {str(e)}"}


def analyze_code_relationships(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Tool to analyze code relationships"""
    query_type = args.get("query_type")
    target = args.get("target")
    context = args.get("context")
    repo_path = args.get("repo_path")
    graph_name = args.get("graph_name")
    offset = args.get("offset", 0)
    page_size = args.get("page_size", 500)
    docstring_offset = args.get("docstring_offset", 0)
    docstring_char_limit = args.get("docstring_char_limit", 1_024)
    value_offset = args.get("value_offset", 0)
    context_offset = args.get("context_offset", 0)
    evidence_char_limit = args.get("evidence_char_limit", 1_024)
    decorator_offset = args.get("decorator_offset", 0)
    decorator_page_size = args.get("decorator_page_size", 16)
    decorator_value_offset = args.get("decorator_value_offset", 0)
    decorator_value_page_size = args.get("decorator_value_page_size", 128)

    if not query_type or not target:
        return {
            "error": "Both 'query_type' and 'target' are required",
            "supported_query_types": [
                "find_callers", "find_callees", "find_all_callers", "find_all_callees", "find_importers", "who_modifies",
                "class_hierarchy", "overrides", "dead_code", "call_chain",
                "module_deps", "variable_scope", "find_complexity", "find_functions_by_argument", "find_functions_by_decorator"
            ]
        }

    if query_type in {"find_callers", "find_callees"}:
        direct_offset, direct_page_size, page_error = _bounded_page_inputs(
            args,
            default_page_size=get_tool_result_limit(query_type, default=20) or 20,
        )
        if page_error:
            return page_error
        offset, page_size = direct_offset, direct_page_size

    try:
        depth = args.get("depth")
        if depth is not None:
            try:
                depth = max(1, min(int(depth), 20))
            except (TypeError, ValueError):
                return {"error": f"Invalid 'depth' value: {depth!r}. Must be an integer between 1 and 20."}
        debug_log(f"Analyzing relationships: {query_type} for {target}, repo_path={repo_path}, depth={depth}")
        results = code_finder.analyze_code_relationships(
            query_type, target, context, repo_path=repo_path, depth=depth,
            graph_name=graph_name, offset=offset, page_size=page_size,
            docstring_offset=docstring_offset,
            docstring_char_limit=docstring_char_limit,
            value_offset=value_offset,
            context_offset=context_offset,
            evidence_char_limit=evidence_char_limit,
            decorator_offset=decorator_offset,
            decorator_page_size=decorator_page_size,
            decorator_value_offset=decorator_value_offset,
            decorator_value_page_size=decorator_value_page_size,
        )

        response = {
            "success": True, "query_type": query_type, "target": target,
            "context": context, "results": results,
        }
        if isinstance(results, dict) and "truncated" in results:
            response["truncated"] = results["truncated"]
            response["offset"] = results.get("offset", offset)
            response["page_size"] = results.get("page_size", page_size)
            response["result_limit"] = results.get("result_limit")
            if "next_offset" in results:
                response["next_offset"] = results.get("next_offset")
            if "continuation_limit_reached" in results:
                response["continuation_limit_reached"] = results[
                    "continuation_limit_reached"
                ]
        return response

    except Exception as e:
        debug_log(f"Error analyzing relationships: {str(e)}")
        return {"error": f"Failed to analyze relationships: {str(e)}"}


def find_code(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Find concise code evidence, with source and docstrings available by page."""
    query = args.get("query")
    DEFAULT_EDIT_DISTANCE = 2
    DEFAULT_FUZZY_SEARCH = False

    fuzzy_search = args.get("fuzzy_search", DEFAULT_FUZZY_SEARCH)
    edit_distance = args.get("edit_distance", DEFAULT_EDIT_DISTANCE)
    repo_path = args.get("repo_path")
    graph_name = args.get("graph_name")

    try:
        offset = _require_integer_argument(args.get("offset", 0), "offset")
        requested_page_size = _require_integer_argument(
            args.get("page_size", _FIND_CODE_DEFAULT_PAGE_SIZE), "page_size"
        )
        source_offset = _require_integer_argument(
            args.get("source_offset", 0), "source_offset"
        )
        docstring_offset = _require_integer_argument(
            args.get("docstring_offset", 0), "docstring_offset"
        )
        source_char_limit = min(
            max(
                1,
                _require_integer_argument(
                    args.get("source_char_limit", 1024), "source_char_limit"
                ),
            ),
            _FIND_CODE_MAX_SOURCE_CHARS,
        )
        docstring_char_limit = min(
            max(
                1,
                _require_integer_argument(
                    args.get("docstring_char_limit", 1024), "docstring_char_limit"
                ),
            ),
            _FIND_CODE_MAX_SOURCE_CHARS,
        )
        source_line_limit = min(
            max(
                1,
                _require_integer_argument(
                    args.get("source_line_limit", 20), "source_line_limit"
                ),
            ),
            _FIND_CODE_MAX_SOURCE_LINES,
        )
        if (
            min(offset, source_offset, docstring_offset) < 0
            or offset >= _FIND_CODE_MAX_OFFSET
            or requested_page_size < 1
        ):
            raise ValueError
    except (TypeError, ValueError):
        return {
            "error": (
                "offset, page_size, source/docstring offsets, and source/docstring "
                "limits must be integers; "
                f"offset must be between 0 and {_FIND_CODE_MAX_OFFSET - 1}, "
                "offsets must be non-negative, and page_size must be positive."
            )
        }

    result_limit = get_tool_result_limit("find_code", default=20) or 20
    page_size = min(
        requested_page_size,
        result_limit,
        _FIND_CODE_MAX_PAGE_SIZE,
        _FIND_CODE_MAX_OFFSET - offset,
    )
    candidate_limit = offset + page_size + 1

    if fuzzy_search:
        # For Lucene backends the replace('_', ' ') improves token splitting.
        # For portable (Kùzu/FalkorDB) backends _find_by_name_fuzzy_portable
        # handles normalisation internally, so we leave the query as-is here.
        pass  # transformation deferred to find_related_code / _find_by_name_fuzzy_portable

    try:
        debug_log(f"Finding code for query: {query} with fuzzy_search={fuzzy_search}, edit_distance={edit_distance}, repo_path={repo_path}")
        results = code_finder.find_related_code(
            query,
            fuzzy_search,
            edit_distance,
            repo_path=repo_path,
            graph_name=graph_name,
            candidate_limit=candidate_limit,
        )
        ranked = results.get("ranked_results", [])
        total_matches = len(ranked)
        page_end = min(offset + page_size, total_matches)
        category_keys = (
            "functions_by_name",
            "classes_by_name",
            "variables_by_name",
            "content_matches",
        )
        category_scan_incomplete = {
            key: bool(getattr(results.get(key), "scan_incomplete", False))
            for key in category_keys
        }
        category_counts_exact = {
            key: (
                len(results.get(key, [])) < candidate_limit
                and not category_scan_incomplete[key]
            )
            for key in category_keys
        }
        total_matches_exact = all(category_counts_exact.values())
        has_more = page_end < total_matches or not total_matches_exact
        scan_window_exhausted = (
            any(category_scan_incomplete.values()) and page_end >= total_matches
        )
        continuation_limit_reached = has_more and (
            page_end >= _FIND_CODE_MAX_OFFSET or scan_window_exhausted
        )
        include_source = bool(args.get("include_source", False))
        include_docstring = bool(args.get("include_docstring", False))
        ranked_page = ranked[offset:page_end]
        hydrate_evidence = getattr(code_finder, "hydrate_code_evidence", None)
        if ranked_page and callable(hydrate_evidence):
            hydrate_evidence(
                ranked_page,
                include_source=include_source,
                include_docstring=include_docstring,
                source_offset=source_offset,
                docstring_offset=docstring_offset,
                source_char_limit=source_char_limit,
                docstring_char_limit=docstring_char_limit,
                graph_name=graph_name,
            )

        concise_results = dict(results)
        category_counts = {}
        concise_results["category_counts_exact"] = category_counts_exact
        category_next_offsets = {}
        for key in category_keys:
            category = results.get(key, [])
            category_start = min(offset, len(category))
            category_end = min(category_start + page_size, len(category))
            category_counts[key] = len(category)
            category_next_offsets[key] = (
                category_end
                if category_end < len(category) and category_end < _FIND_CODE_MAX_OFFSET
                else None
            )
            concise_results[key] = [
                _find_code_category_item(item)
                for item in category[category_start:category_end]
            ]
        concise_results["category_offset"] = offset
        concise_results["category_counts"] = category_counts
        concise_results["category_next_offsets"] = category_next_offsets
        concise_results["ranked_results"] = [
            _find_code_result_item(
                item,
                query,
                include_source=include_source,
                include_docstring=include_docstring,
                source_offset=source_offset,
                docstring_offset=docstring_offset,
                char_limit=source_char_limit,
                docstring_char_limit=docstring_char_limit,
                line_limit=source_line_limit,
            )
            for item in ranked_page
        ]
        concise_results["total_matches"] = total_matches

        response = {
            "success": True,
            "query": query,
            "results": concise_results,
            "offset": offset,
            "page_size": page_size,
            "total_matches": total_matches,
            "total_matches_exact": total_matches_exact,
            "has_more": has_more,
            "continuation_limit_reached": continuation_limit_reached,
            "truncated": has_more,
        }
        if has_more and not continuation_limit_reached:
            response["next_offset"] = page_end
            response["result_limit"] = page_size
        return response

    except Exception as e:
        debug_log(f"Error finding code: {str(e)}")
        return {"error": f"Failed to find code: {str(e)}"}


# ── Spring-aware handlers (#887 / #889) ───────────────────────────────────────

#: Row cap shared by the Java/Spring tools; surfaced via `truncated`.
_SPRING_ROW_LIMIT = 100


def find_java_spring_endpoints(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Return all Spring HTTP endpoint functions, optionally filtered by method or path."""
    offset, page_size, page_error = _bounded_page_inputs(
        args, default_page_size=_SPRING_ROW_LIMIT
    )
    if page_error:
        return page_error

    http_method = args.get("http_method")
    path_pattern = args.get("path_pattern")
    repo_path = args.get("repo_path")
    repo_path = code_finder._normalize_repo_path_prefix(repo_path) if repo_path else None

    conditions = ["fn.http_method IS NOT NULL"]
    params: Dict[str, Any] = {}

    if http_method:
        conditions.append("fn.http_method = $http_method")
        params["http_method"] = http_method.upper()

    if path_pattern:
        conditions.append("fn.http_path CONTAINS $path_pattern")
        params["path_pattern"] = path_pattern

    if repo_path:
        conditions.append("fn.path STARTS WITH $repo_path")
        params["repo_path"] = repo_path

    where_clause = " AND ".join(conditions)
    params["spring_offset"] = offset
    params["spring_candidate_limit"] = page_size + 1
    query = f"""
        MATCH (fn:Function)
        WHERE {where_clause}
        RETURN fn.http_method AS method, fn.http_path AS path,
               fn.name AS handler, fn.path AS file, fn.line_number AS line_number
        ORDER BY fn.http_path, fn.http_method, fn.path, fn.line_number, fn.uid
        SKIP $spring_offset LIMIT $spring_candidate_limit
    """

    try:
        with code_finder.driver.session() as session:
            result = session.run(query, **params)
            rows = [dict(r) for r in result]
        page, metadata = _page_rows(rows, offset, page_size)
        return {
            "success": True,
            "endpoints": page,
            "count": len(page),
            **metadata,
        }
    except Exception as exc:
        debug_log(f"Error finding Spring endpoints: {exc}")
        return {"error": str(exc)}


def find_java_spring_beans(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Return Spring bean classes, optionally filtered by stereotype."""
    offset, page_size, page_error = _bounded_page_inputs(
        args, default_page_size=_SPRING_ROW_LIMIT
    )
    if page_error:
        return page_error

    stereotype = args.get("stereotype")
    repo_path = args.get("repo_path")
    repo_path = code_finder._normalize_repo_path_prefix(repo_path) if repo_path else None

    conditions = ["c.spring_stereotype IS NOT NULL"]
    params: Dict[str, Any] = {}

    if stereotype:
        conditions.append("c.spring_stereotype = $stereotype")
        params["stereotype"] = stereotype.upper()

    if repo_path:
        conditions.append("c.path STARTS WITH $repo_path")
        params["repo_path"] = repo_path

    where_clause = " AND ".join(conditions)
    injector_repo_filter = (
        "AND injector.path STARTS WITH $repo_path" if repo_path else ""
    )
    params["spring_offset"] = offset
    params["spring_candidate_limit"] = page_size + 1
    query = f"""
        MATCH (c:Class)
        WHERE {where_clause}
        OPTIONAL MATCH (injector)-[inj:INJECTS]->(c)
        WHERE 1 = 1 {injector_repo_filter}
        WITH c, count(inj) AS injection_count
        RETURN c.name AS name, c.spring_stereotype AS stereotype,
               c.path AS file, c.line_number AS line_number, injection_count
        ORDER BY stereotype, name, c.path, c.line_number, c.uid
        SKIP $spring_offset LIMIT $spring_candidate_limit
    """

    try:
        with code_finder.driver.session() as session:
            result = session.run(query, **params)
            rows = [dict(r) for r in result]
        page, metadata = _page_rows(rows, offset, page_size)
        return {"success": True, "beans": page, "count": len(page), **metadata}
    except Exception as exc:
        debug_log(f"Error finding Spring beans: {exc}")
        return {"error": str(exc)}


def find_datasource_nodes(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Return datasource data linked to this repository, or global metadata explicitly."""
    offset, page_size, page_error = _bounded_page_inputs(
        args, default_page_size=_SPRING_ROW_LIMIT
    )
    if page_error:
        return page_error
    table_offset, table_page_size, page_error = _bounded_page_inputs(
        args,
        offset_key="table_offset",
        page_size_key="table_page_size",
        default_page_size=_SPRING_ROW_LIMIT,
    )
    if page_error:
        return page_error

    kind = args.get("kind")
    name_filter = args.get("name")
    include_columns = args.get("include_columns", False)
    global_scope = args.get("global_scope") is True
    repo_path = args.get("repo_path")
    if not global_scope:
        repo_path = code_finder._normalize_repo_path_prefix(repo_path) if repo_path else None
        if not repo_path:
            return {"error": "repo_path is required unless global_scope=true."}

    ds_conditions = []
    params: Dict[str, Any] = {}

    if kind:
        ds_conditions.append("d.kind = $kind")
        params["kind"] = kind

    if name_filter:
        ds_conditions.append("d.name CONTAINS $name_filter")
        params["name_filter"] = name_filter

    if include_columns:
        column_offset, column_page_size, page_error = _bounded_page_inputs(
            args,
            offset_key="column_offset",
            page_size_key="column_page_size",
            default_page_size=_SPRING_ROW_LIMIT,
        )
        if page_error:
            return page_error

    if global_scope:
        key_pattern_offset, key_pattern_page_size, page_error = _bounded_page_inputs(
            args,
            offset_key="key_pattern_offset",
            page_size_key="key_pattern_page_size",
            default_page_size=_SPRING_ROW_LIMIT,
        )
        if page_error:
            return page_error

    base_where = ("WHERE " + " AND ".join(ds_conditions)) if ds_conditions else ""
    if global_scope:
        datasource_count_query = f"""
            MATCH (d:Datasource)
            {base_where}
            RETURN count(DISTINCT d.name) AS datasource_count
        """
        datasource_query = f"""
            MATCH (d:Datasource)
            {base_where}
            RETURN DISTINCT d.name AS datasource, d.kind AS kind,
                   d.host AS host, d.env AS env
            ORDER BY kind, datasource
            SKIP $datasource_offset LIMIT $datasource_candidate_limit
        """
    else:
        params["repo_path"] = repo_path
        scoped_conditions = ["source.path STARTS WITH $repo_path", *ds_conditions]
        datasource_count_query = f"""
            MATCH (source)-[:MAPS_TO|READS|WRITES]->(:DbTable)-[:STORED_IN]->(d:Datasource)
            WHERE {" AND ".join(scoped_conditions)}
            RETURN count(DISTINCT d.name) AS datasource_count
        """
        datasource_query = f"""
            MATCH (source)-[:MAPS_TO|READS|WRITES]->(:DbTable)-[:STORED_IN]->(d:Datasource)
            WHERE {" AND ".join(scoped_conditions)}
            RETURN DISTINCT d.name AS datasource, d.kind AS kind,
                   d.host AS host, d.env AS env
            ORDER BY kind, datasource
            SKIP $datasource_offset LIMIT $datasource_candidate_limit
        """

    datasource_params = {
        **params,
        "datasource_offset": offset,
        "datasource_candidate_limit": page_size + 1,
    }
    table_params = {
        "table_offset": table_offset,
        "table_candidate_limit": table_page_size + 1,
    }
    if not global_scope:
        table_params["repo_path"] = repo_path
    if global_scope:
        table_query = """
            MATCH (tbl:DbTable)-[:STORED_IN]->(d:Datasource)
            WHERE d.name IN $datasource_names
            RETURN DISTINCT d.name AS datasource, tbl.fqn AS table_fqn
            ORDER BY datasource, table_fqn
            SKIP $table_offset LIMIT $table_candidate_limit
        """
    else:
        table_query = """
            MATCH (source)-[:MAPS_TO|READS|WRITES]->(tbl:DbTable)-[:STORED_IN]->(d:Datasource)
            WHERE source.path STARTS WITH $repo_path AND d.name IN $datasource_names
            RETURN DISTINCT d.name AS datasource, tbl.fqn AS table_fqn
            ORDER BY datasource, table_fqn
            SKIP $table_offset LIMIT $table_candidate_limit
        """

    column_rows = []
    if include_columns:
        if global_scope:
            column_query = """
                MATCH (tbl:DbTable)-[:STORED_IN]->(d:Datasource)
                MATCH (tbl)-[:HAS_COLUMN]->(col:DbColumn)
                WHERE d.name IN $datasource_names
                RETURN DISTINCT d.name AS datasource, tbl.fqn AS table_fqn,
                       col.name AS col_name, col.type AS col_type,
                       col.nullable AS nullable, col.is_primary_key AS is_pk
                ORDER BY datasource, table_fqn, col_name
                SKIP $column_offset LIMIT $column_candidate_limit
            """
        else:
            column_query = """
                MATCH (source)-[:MAPS_TO|READS|WRITES]->(tbl:DbTable)-[:STORED_IN]->(d:Datasource)
                MATCH (tbl)-[:HAS_COLUMN]->(col:DbColumn)
                WHERE source.path STARTS WITH $repo_path AND d.name IN $datasource_names
                RETURN DISTINCT d.name AS datasource, tbl.fqn AS table_fqn,
                       col.name AS col_name, col.type AS col_type,
                       col.nullable AS nullable, col.is_primary_key AS is_pk
                ORDER BY datasource, table_fqn, col_name
                SKIP $column_offset LIMIT $column_candidate_limit
            """
        column_params = {
            "datasource_names": [],
            "column_offset": column_offset,
            "column_candidate_limit": column_page_size + 1,
        }
        if not global_scope:
            column_params["repo_path"] = repo_path

    key_pattern_rows = []
    if global_scope:
        key_pattern_query = """
            MATCH (kp:RedisKeyPattern)-[:STORED_IN]->(d:Datasource)
            WHERE d.kind = 'redis' AND d.name IN $datasource_names
            RETURN DISTINCT d.name AS datasource, kp.pattern AS pattern,
                   kp.key_type AS key_type, kp.count AS count
            ORDER BY datasource, pattern, key_type
            SKIP $key_pattern_offset LIMIT $key_pattern_candidate_limit
        """
        key_pattern_params = {
            "datasource_names": [],
            "key_pattern_offset": key_pattern_offset,
            "key_pattern_candidate_limit": key_pattern_page_size + 1,
        }

    try:
        with code_finder.driver.session() as session:
            count_rows = [
                dict(row)
                for row in session.run(datasource_count_query, **params)
            ]
            datasource_count = (
                count_rows[0].get("datasource_count", 0) if count_rows else 0
            )
            raw_datasources = [
                dict(row) for row in session.run(datasource_query, **datasource_params)
            ]
            datasource_rows, datasource_meta = _page_rows(raw_datasources, offset, page_size)
            datasources = [dict(row, tables=[]) for row in datasource_rows]
            datasource_names = [row["datasource"] for row in datasources]

            table_rows = []
            if datasource_names:
                table_rows = [
                    dict(row)
                    for row in session.run(
                        table_query,
                        **{**table_params, "datasource_names": datasource_names},
                    )
                ]
            table_page, table_meta = _page_rows(
                table_rows, table_offset, table_page_size
            )
            datasource_by_name = {row["datasource"]: row for row in datasources}
            for row in table_page:
                datasource = datasource_by_name.get(row.get("datasource"))
                table_fqn = row.get("table_fqn")
                if datasource is not None and table_fqn is not None:
                    datasource["tables"].append(table_fqn)

            if include_columns and datasource_names:
                column_params["datasource_names"] = datasource_names
                raw_columns = [
                    dict(row) for row in session.run(column_query, **column_params)
                ]
                column_rows, column_meta = _page_rows(
                    raw_columns, column_offset, column_page_size
                )
                for row in column_rows:
                    datasource = datasource_by_name.get(row.get("datasource"))
                    if datasource is None or row.get("table_fqn") is None:
                        continue
                    datasource.setdefault("columns", {}).setdefault(
                        row["table_fqn"], []
                    ).append({
                        "name": row["col_name"],
                        "type": row["col_type"],
                        "nullable": row["nullable"],
                        "is_primary_key": row["is_pk"],
                    })
            else:
                column_meta = (
                    _page_rows([], column_offset, column_page_size)[1]
                    if include_columns
                    else None
                )

            if global_scope and datasource_names:
                key_pattern_params["datasource_names"] = datasource_names
                raw_key_patterns = [
                    dict(row)
                    for row in session.run(key_pattern_query, **key_pattern_params)
                ]
                key_pattern_rows, key_pattern_meta = _page_rows(
                    raw_key_patterns, key_pattern_offset, key_pattern_page_size
                )
                for row in key_pattern_rows:
                    datasource = datasource_by_name.get(row.get("datasource"))
                    if datasource is not None:
                        datasource.setdefault("key_patterns", []).append({
                            "pattern": row["pattern"],
                            "key_type": row["key_type"],
                            "count": row["count"],
                        })
            elif global_scope:
                key_pattern_meta = _page_rows(
                    [], key_pattern_offset, key_pattern_page_size
                )[1]
            else:
                key_pattern_meta = None

        response = {
            "success": True,
            "datasources": datasources,
            "count": datasource_count,
            "page_count": len(datasources),
            **datasource_meta,
            "table_offset": table_meta["offset"],
            "table_page_size": table_meta["page_size"],
            "tables_truncated": table_meta["truncated"],
            "table_next_offset": table_meta["next_offset"],
            "table_continuation_limit_reached": table_meta[
                "continuation_limit_reached"
            ],
        }
        continuation = {
            "offset": offset,
            "page_size": page_size,
            "table_offset": table_offset,
            "table_page_size": table_page_size,
        }
        if include_columns:
            response.update({
                "column_offset": column_meta["offset"],
                "column_page_size": column_meta["page_size"],
                "columns_truncated": column_meta["truncated"],
                "column_next_offset": column_meta["next_offset"],
                "column_continuation_limit_reached": column_meta[
                    "continuation_limit_reached"
                ],
            })
            continuation.update({
                "column_offset": column_offset,
                "column_page_size": column_page_size,
            })
        if global_scope:
            response.update({
                "global_scope": True,
                "key_pattern_offset": key_pattern_meta["offset"],
                "key_pattern_page_size": key_pattern_meta["page_size"],
                "key_patterns_truncated": key_pattern_meta["truncated"],
                "key_pattern_next_offset": key_pattern_meta["next_offset"],
                "key_pattern_continuation_limit_reached": key_pattern_meta[
                    "continuation_limit_reached"
                ],
            })
            continuation.update({
                "key_pattern_offset": key_pattern_offset,
                "key_pattern_page_size": key_pattern_page_size,
            })
        else:
            response["repo_scope"] = repo_path
        response["continuation"] = continuation
        return response
    except Exception as exc:
        debug_log(f"Error finding datasource nodes: {exc}")
        return {"error": str(exc)}

