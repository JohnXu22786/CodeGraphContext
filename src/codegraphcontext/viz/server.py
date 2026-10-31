from __future__ import annotations
# src/codegraphcontext/viz/server.py
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pathlib import Path
import uvicorn
import json
import os
import sys
import re
import requests
import traceback
from itertools import islice
from pydantic import BaseModel
from typing import Optional, List, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.database import DatabaseManager
from ..utils.debug_log import debug_log
from ..utils.path_sandbox import is_path_allowed
from ..utils.cypher_readonly import is_read_only_cypher as _is_read_only_cypher, read_only_rejection_message
from ..utils import graph_shapes

app = FastAPI()


def _read_session_kwargs(manager) -> dict:
    """Session kwargs that request READ access for read-only endpoints.

    Neo4j honours ``default_access_mode`` natively; the FalkorDB wrapper routes
    READ sessions through ``GRAPH.RO_QUERY`` for server-enforced read-only
    execution. Other backends' session shims ignore unknown kwargs, so this is
    safe to pass unconditionally for those we know enforce it.
    """
    backend = getattr(manager, "get_backend_type", lambda: "neo4j")()
    if backend in ("neo4j", "falkordb", "falkordb-remote"):
        return {"default_access_mode": "READ"}
    return {}


def _static_root() -> Path:
    if not _static_dir:
        raise HTTPException(status_code=500, detail="Static directory not configured")
    return Path(_static_dir).resolve()


def _safe_static_file(relative_path: str) -> Path:
    """Resolve *relative_path* under the static root; reject traversal."""
    root = _static_root()
    candidate = (root / relative_path).resolve()
    if not is_path_under_static(candidate, root):
        raise HTTPException(status_code=403, detail="Access denied")
    return candidate


def is_path_under_static(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _safe_read_text_page(path: str, offset: int, page_size: int) -> tuple[str, bool]:
    file_path = Path(path).resolve()
    if not is_path_allowed(file_path):
        raise HTTPException(status_code=403, detail="File path is outside allowed roots")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    with open(file_path, "r", encoding="utf-8", errors="replace", newline="") as handle:
        handle.read(offset)
        raw_page = handle.read(page_size + 1)

    content = raw_page[:page_size]
    has_more = len(raw_page) > page_size
    line_boundaries = list(re.finditer(r"\r\n|\r|\n", content))
    if len(line_boundaries) >= MAX_GRAPH_FILE_PAGE_LINES:
        end_of_line_window = line_boundaries[MAX_GRAPH_FILE_PAGE_LINES - 1].end()
        if len(content) > end_of_line_window:
            content = content[:end_of_line_window]
            has_more = True
    return content, has_more


def _safe_read_text_preview(path: str, max_chars: int) -> tuple[str, bool]:
    file_path = Path(path).resolve()
    if not is_path_allowed(file_path):
        raise HTTPException(status_code=403, detail="File path is outside allowed roots")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read(max_chars + 1)
    return text[:max_chars], len(text) > max_chars

# CORS: only allow requests from the local visualization frontend.
# The server binds to 127.0.0.1 (localhost) so only local origins are legitimate.
_ALLOWED_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=_ALLOWED_ORIGIN_REGEX,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Global database manager (will be initialized when server starts)
db_manager: Optional[DatabaseManager] = None
# Path to static directory
_static_dir: Optional[str] = None

MAX_GRAPH_QUERY_ROWS = 2_000
MAX_GRAPH_PAGE_SIZE = 50
MAX_GRAPH_RESULT_OFFSET = 100_000
MAX_GRAPH_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_GRAPH_PROPERTY_CHARS = 1_024
MAX_GRAPH_PROPERTY_ITEMS = 100
MAX_GRAPH_PROPERTY_DEPTH = 4
MAX_GRAPH_FILE_PREVIEWS = 100
MAX_GRAPH_FILE_PREVIEW_CHARS = 2_048
MAX_GRAPH_FILE_PAGE_CHARS = 8_000
MAX_GRAPH_FILE_PAGE_LINES = 200
MAX_GRAPH_FILE_OFFSET = 1_000_000


def _graph_entity_order_key(variable: str, backend: str) -> str:
    if backend.lower() in {"kuzu", "kuzudb", "ladybug", "ladybugdb"}:
        return f"CAST(id({variable}) AS STRING)"
    return f"id({variable})"


def _graph_label_expression(variable: str, backend: str) -> str:
    if backend.lower() in {"kuzu", "kuzudb", "ladybug", "ladybugdb"}:
        return f"label({variable})"
    return f"labels({variable})[0]"


def _graph_property_expression(variable: str, field: str, backend: str) -> str:
    if backend.lower() in {"kuzu", "kuzudb", "ladybug", "ladybugdb"}:
        return f"struct_extract({variable}, '{field}')"
    return f"{variable}.{field}"


def _graph_node_projection(variable: str, backend: str) -> str:
    """Project only the scalar graph fields needed by the UI, with bounded text."""
    substring_start = (
        1 if backend.lower() in {"kuzu", "kuzudb", "ladybug", "ladybugdb"}
        else 0
    )
    text_fields = (
        "name", "path", "uid", "lang", "source", "docstring", "context",
        "class_context",
    )
    fields = [
        f"{field}: substring({_graph_property_expression(variable, field, backend)}, {substring_start}, "
        f"{MAX_GRAPH_PROPERTY_CHARS})"
        for field in text_fields
    ]
    fields.extend(
        f"_{field}_total_chars: size({_graph_property_expression(variable, field, backend)})"
        for field in text_fields
    )
    fields.extend((
        f"_id: id({variable})",
        f"_label: {_graph_label_expression(variable, backend)}",
        f"line_number: {_graph_property_expression(variable, 'line_number', backend)}",
        f"end_line: {_graph_property_expression(variable, 'end_line', backend)}",
        f"is_dependency: {_graph_property_expression(variable, 'is_dependency', backend)}",
        f"cyclomatic_complexity: {_graph_property_expression(variable, 'cyclomatic_complexity', backend)}",
    ))
    return (
        f"CASE WHEN {variable} IS NULL THEN NULL ELSE "
        "{ " + ", ".join(fields) + " } END"
    )


def _graph_relationship_projection(variable: str, source: str, target: str, backend: str) -> str:
    """Return only bounded relationship identity and endpoint metadata."""
    relationship_type = (
        f"label({variable})"
        if backend.lower() in {"kuzu", "kuzudb", "ladybug", "ladybugdb"}
        else f"type({variable})"
    )
    return (
        f"CASE WHEN {variable} IS NULL THEN NULL ELSE "
        f"{{_id: id({variable}), _label: {relationship_type}, "
        f"_src: id({source}), _dst: id({target})}} END"
    )


def _repository_graph_query(backend: str, offset: int, page_size: int) -> str:
    """Return a bounded row page without collecting the complete repository graph."""
    return f"""
    MATCH (r:Repository {{path: $repo_path}})
    OPTIONAL MATCH (r)-[:CONTAINS*0..]->(node)
    WITH DISTINCT r, node
    OPTIONAL MATCH (node)-[rel]->(target)
    WHERE target = r OR {_graph_label_expression('target', backend)} = 'Module'
       OR EXISTS {{ MATCH (r)-[:CONTAINS*0..]->(target) }}
    RETURN {_graph_node_projection('node', backend)} AS n,
           {_graph_relationship_projection('rel', 'node', 'target', backend)} AS rel,
           {_graph_node_projection('target', backend)} AS m
    ORDER BY {_graph_entity_order_key('node', backend)},
             {_graph_entity_order_key('rel', backend)},
             {_graph_entity_order_key('target', backend)}
    SKIP {offset} LIMIT {page_size + 1}
    """


def _rewrite_bounded_graph_entity_projections(
    query: str, backend: str
) -> tuple[Optional[str], bool]:
    """Replace raw n/rel/m entity returns with bounded map projections.

    Returns ``(query, True)`` when a raw graph entity projection was rewritten,
    ``(None, True)`` when it used an unsupported shape, and ``(query, False)``
    when no raw entity aliases were present.
    """
    from ..tools.handlers.query_handlers import (
        _mask_query_literals_and_comments,
        _match_node_pattern_segments,
        _top_level_cypher_clauses,
    )

    masked = _mask_query_literals_and_comments(query)
    clauses = _top_level_cypher_clauses(masked)
    identifier = r"(?:`[^`]+`|[A-Za-z_]\w*)"
    direct_entity_projection = re.compile(
        rf"\s*(?P<distinct>DISTINCT\s+)?(?P<value>{identifier})"
        rf"(?:\s+AS\s+(?P<alias>{identifier}))?\s*",
        re.IGNORECASE,
    )
    node_variables = set()
    for clause in clauses:
        if clause[0] not in {"MATCH", "OPTIONAL MATCH"}:
            continue
        next_clause = next(
            (later for later in clauses if later[1] > clause[1]),
            None,
        )
        pattern_end = next_clause[1] if next_clause else len(masked)
        pattern = masked[clause[2]:pattern_end]
        for segment in _match_node_pattern_segments(pattern):
            node_variables.update(
                variable.strip("`")
                for variable, _labels in segment
                if variable
            )

    nullable_expression = rf"(?:{identifier}|NULL)"
    projection_pattern = re.compile(
        rf"\s*(?P<distinct>DISTINCT\s+)?(?P<n>{identifier})\s+AS\s+`?n`?\s*,\s*"
        rf"(?P<rel>{nullable_expression})\s+AS\s+`?rel`?\s*,\s*"
        rf"(?P<m>{nullable_expression})\s+AS\s+`?m`?\s*",
        re.IGNORECASE,
    )
    raw_entity_alias = re.compile(
        rf"(?<![\w.`])(?P<value>{identifier})\s+AS\s+`?(?:n|rel|m)`?\b",
        re.IGNORECASE,
    )
    scalar_node_functions = {"count", "elementid", "id", "labels", "type"}

    def has_raw_node_value(projection: str) -> bool:
        for variable in node_variables:
            variable_pattern = re.compile(
                rf"(?<![\w.`])(?:{re.escape(variable)}|`{re.escape(variable)}`)(?![\w`])"
            )
            for match in variable_pattern.finditer(projection):
                suffix = projection[match.end():].lstrip()
                if suffix.startswith("."):
                    continue
                prefix = projection[:match.start()].rstrip()
                function_match = re.search(r"([A-Za-z_]\w*)\s*\($", prefix)
                if (
                    function_match
                    and function_match.group(1).casefold() in scalar_node_functions
                ):
                    continue
                return True
        return False

    replacements = []
    projected_alias_targets = {}
    found_raw_entity = False
    return_clauses = [clause for clause in clauses if clause[0] == "RETURN"]
    for return_clause in return_clauses:
        next_clause = next(
            (clause for clause in clauses if clause[1] > return_clause[1]),
            None,
        )
        projection_end = next_clause[1] if next_clause else len(masked)
        projection = masked[return_clause[2]:projection_end]
        projection_match = projection_pattern.fullmatch(projection)
        if not projection_match:
            direct_match = direct_entity_projection.fullmatch(projection)
            if (
                direct_match
                and direct_match.group("value").strip("`") in node_variables
            ):
                if any(
                    name in {"WITH", "UNWIND", "CALL"}
                    for name, _, _ in clauses
                ):
                    return None, True
                value = direct_match.group("value")
                distinct = direct_match.group("distinct") or ""
                output_alias = direct_match.group("alias") or value
                replacements.append((
                    return_clause[2],
                    projection_end,
                    f" {distinct}{_graph_node_projection(value, backend)} AS n ",
                ))
                projected_alias_targets[value.strip("`")] = "n"
                projected_alias_targets[output_alias.strip("`")] = "n"
                found_raw_entity = True
                continue
            if has_raw_node_value(projection):
                return None, True
            if any(
                match.group("value").upper() not in {"END", "NULL"}
                for match in raw_entity_alias.finditer(projection)
            ):
                return None, True
            continue

        n_value = projection_match.group("n")
        rel_value = projection_match.group("rel")
        m_value = projection_match.group("m")
        if rel_value.upper() != "NULL" and (
            n_value.upper() == "NULL" or m_value.upper() == "NULL"
        ):
            return None, True

        bounded_n = _graph_node_projection(n_value, backend)
        bounded_rel = (
            "NULL"
            if rel_value.upper() == "NULL"
            else _graph_relationship_projection(
                rel_value, n_value, m_value, backend
            )
        )
        bounded_m = (
            "NULL"
            if m_value.upper() == "NULL"
            else _graph_node_projection(m_value, backend)
        )
        distinct = projection_match.group("distinct") or ""
        replacements.append((
            return_clause[2],
            projection_end,
            f" {distinct}{bounded_n} AS n, {bounded_rel} AS rel, {bounded_m} AS m ",
        ))
        projected_alias_targets["n"] = "n"
        if rel_value.upper() != "NULL":
            projected_alias_targets["rel"] = "rel"
        if m_value.upper() != "NULL":
            projected_alias_targets["m"] = "m"
        found_raw_entity = True

    if not found_raw_entity:
        return query, False

    for clause in clauses:
        if clause[0] != "ORDER BY":
            continue
        next_clause = next(
            (later for later in clauses if later[1] > clause[1]),
            None,
        )
        order_end = next_clause[1] if next_clause else len(masked)
        order_mask = masked[clause[2]:order_end]
        order_text = query[clause[2]:order_end]
        order_replacements = []
        for alias, target in projected_alias_targets.items():
            if alias != target:
                alias_reference = re.compile(
                    rf"(?<![\w.`])(?:`{re.escape(alias)}`|{re.escape(alias)})(?=\s*\.)",
                    re.IGNORECASE,
                )
                order_replacements.extend(
                    (match.start(), match.end(), target)
                    for match in alias_reference.finditer(order_mask)
                )
            id_call = re.compile(
                rf"\bid\s*\(\s*`?{alias}`?\s*\)", re.IGNORECASE
            )
            replacement = _graph_property_expression(target, "_id", backend)
            order_replacements.extend(
                (match.start(), match.end(), replacement)
                for match in id_call.finditer(order_mask)
            )
        for start, end, replacement in sorted(order_replacements, reverse=True):
            order_text = order_text[:start] + replacement + order_text[end:]
        if order_replacements:
            replacements.append((clause[2], order_end, order_text))

    rewritten = query
    for start, end, replacement in sorted(replacements, reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    return rewritten, True


def _bounded_graph_projection_page(
    query: str,
    backend: str,
    offset: int,
    page_size: int,
    max_result_limit: int = MAX_GRAPH_QUERY_ROWS,
) -> tuple[Optional[str], Optional[int]]:
    """Page a bounded n/rel/m graph query by its complete entity identities.

    The CLI builds a small set of visualization queries with an explicit LIMIT
    but no ORDER BY. Those queries return every matched node and relationship
    as n/rel/m aliases, so the API can apply stable backend entity IDs before
    the existing result cap. Other custom query shapes stay fail-closed.
    """
    from ..tools.handlers.query_handlers import (
        _CYPHER_VARIABLE_LENGTH_RELATIONSHIP_RE,
        _mask_query_literals_and_comments,
        _match_node_pattern_segments,
        _top_level_cypher_clauses,
    )

    masked = _mask_query_literals_and_comments(query)
    clauses = _top_level_cypher_clauses(masked)
    if not clauses or clauses[-1][0] != "LIMIT":
        return None, None
    if any(
        name in {"CALL", "UNWIND", "WITH", "ORDER BY", "SKIP"}
        for name, _, _ in clauses
    ):
        return None, None
    if _CYPHER_VARIABLE_LENGTH_RELATIONSHIP_RE.search(masked):
        return None, None

    limit_clause = clauses[-1]
    limit_match = re.fullmatch(r"\s*(\d+)\s*;?\s*", masked[limit_clause[2]:])
    if not limit_match:
        return None, None
    result_limit = int(limit_match.group(1))
    if result_limit < 1 or result_limit > max_result_limit:
        return None, None

    identifier = r"(?:`[^`]+`|[A-Za-z_]\w*)"
    expression = rf"(?P<value>{identifier})"
    nullable_expression = rf"(?:{identifier}|NULL)"
    projection_pattern = re.compile(
        rf"\s*(?P<distinct>DISTINCT\s+)?{expression}\s+AS\s+`?n`?\s*,\s*"
        rf"(?P<rel>{nullable_expression})\s+AS\s+`?rel`?\s*,\s*"
        rf"(?P<m>{nullable_expression})\s+AS\s+`?m`?\s*",
        re.IGNORECASE,
    )
    relationship_variable = re.compile(
        rf"^\s*({identifier})\s*(?:[:|&]|$)", re.IGNORECASE
    )

    union_clauses = [clause for clause in clauses if clause[0] == "UNION"]
    branch_ranges = []
    branch_start = 0
    for union in union_clauses:
        branch_ranges.append((branch_start, union[1]))
        branch_start = union[2]
    branch_ranges.append((branch_start, limit_clause[1]))
    non_null_order_aliases = set()
    null_order_aliases = set()
    projection_replacements = []

    for branch_start, branch_end in branch_ranges:
        branch = masked[branch_start:branch_end]
        branch_clauses = _top_level_cypher_clauses(branch)
        if any(
            name in {"CALL", "UNWIND", "WITH", "ORDER BY", "SKIP", "UNION"}
            for name, _, _ in branch_clauses
        ):
            return None, None
        returns = [clause for clause in branch_clauses if clause[0] == "RETURN"]
        matches = [
            clause for clause in branch_clauses
            if clause[0] in {"MATCH", "OPTIONAL MATCH"}
        ]
        if len(returns) != 1 or not matches:
            return None, None

        return_clause = returns[0]
        next_clause = next(
            (clause for clause in branch_clauses if clause[1] > return_clause[1]),
            None,
        )
        projection_end = next_clause[1] if next_clause else len(branch)
        projection = branch[return_clause[2]:projection_end]
        projection_match = projection_pattern.fullmatch(projection)
        if not projection_match:
            return None, None

        output_variables = set()
        projection_values = (
            projection_match.group("value"),
            projection_match.group("rel"),
            projection_match.group("m"),
        )
        for alias, value in zip(("n", "rel", "m"), projection_values):
            if value.upper() != "NULL":
                output_variables.add(value.strip("`"))
                non_null_order_aliases.add(alias)
            else:
                null_order_aliases.add(alias)

        bound_variables = set()
        for match_clause in matches:
            next_clause = next(
                (clause for clause in branch_clauses if clause[1] > match_clause[1]),
                None,
            )
            pattern_end = next_clause[1] if next_clause else len(branch)
            pattern = branch[match_clause[2]:pattern_end]
            if next_clause and next_clause[0] == "WHERE":
                pattern = branch[match_clause[2]:next_clause[1]]
            segments = _match_node_pattern_segments(pattern)
            for segment in segments:
                for variable, _labels in segment:
                    if not variable:
                        return None, None
                    bound_variables.add(variable.strip("`"))
            relationship_patterns = re.findall(r"\[([^\]]*)\]", pattern)
            for relationship in relationship_patterns:
                variable_match = relationship_variable.match(relationship)
                if not variable_match:
                    return None, None
                bound_variables.add(variable_match.group(1).strip("`"))

        if not bound_variables or bound_variables != output_variables:
            return None, None

        n_value, rel_value, m_value = projection_values
        if rel_value.upper() != "NULL" and (
            n_value.upper() == "NULL" or m_value.upper() == "NULL"
        ):
            return None, None
        bounded_n = _graph_node_projection(n_value, backend)
        bounded_rel = (
            "NULL"
            if rel_value.upper() == "NULL"
            else _graph_relationship_projection(
                rel_value, n_value, m_value, backend
            )
        )
        bounded_m = (
            "NULL"
            if m_value.upper() == "NULL"
            else _graph_node_projection(m_value, backend)
        )
        bounded_projection = (
            f"{projection_match.group('distinct') or ''}"
            f"{bounded_n} AS n, {bounded_rel} AS rel, {bounded_m} AS m"
        )
        for alias, value in zip(("n", "rel", "m"), projection_values):
            sort_key = (
                _graph_entity_order_key(value.strip("`"), backend)
                if value.upper() != "NULL"
                else "NULL"
            )
            bounded_projection += f", {sort_key} AS _cgc_sort_{alias}"
        projection_replacements.append((
            branch_start + return_clause[2],
            branch_start + projection_end,
            f" {bounded_projection} ",
        ))

    # A key that is NULL in only some UNION branches cannot order the rows
    # from the other branches. Omitting it would let distinct relationships
    # with the same endpoints tie and make SKIP continuation lossy.
    if non_null_order_aliases & null_order_aliases:
        return None, None
    page_limit = min(page_size + 1, max(0, result_limit - offset))
    order = ", ".join(
        f"_cgc_sort_{alias}"
        for alias in ("n", "rel", "m")
        if alias in non_null_order_aliases
    )
    if not order:
        return None, None
    query_prefix = query[:limit_clause[1]].rstrip()
    for start, end, replacement in sorted(projection_replacements, reverse=True):
        query_prefix = (
            query_prefix[:start] + replacement + query_prefix[end:]
        )
    return (
        f"{query_prefix} ORDER BY {order} SKIP {offset} LIMIT {page_limit}",
        result_limit,
    )

def _bound_graph_value(value, depth=0):
    """Return a JSON-safe, bounded preview and whether any content was omitted."""
    if isinstance(value, str):
        if len(value) > MAX_GRAPH_PROPERTY_CHARS:
            return value[:MAX_GRAPH_PROPERTY_CHARS], True
        return value, False
    if isinstance(value, (bytes, bytearray)):
        preview = bytes(value[: MAX_GRAPH_PROPERTY_CHARS + 1]).decode("utf-8", errors="replace")
        return preview[:MAX_GRAPH_PROPERTY_CHARS], len(value) > MAX_GRAPH_PROPERTY_CHARS
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, dict):
        if depth >= MAX_GRAPH_PROPERTY_DEPTH:
            return {}, bool(value)
        bounded = {}
        truncated = False
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_GRAPH_PROPERTY_ITEMS:
                truncated = True
                break
            bounded_key = str(key)[:MAX_GRAPH_PROPERTY_CHARS]
            bounded[bounded_key], item_truncated = _bound_graph_value(item, depth + 1)
            truncated = truncated or item_truncated
        return bounded, truncated
    if isinstance(value, (list, tuple, set)):
        if depth >= MAX_GRAPH_PROPERTY_DEPTH:
            return [], bool(value)
        items = list(islice(iter(value), MAX_GRAPH_PROPERTY_ITEMS + 1))
        bounded = []
        truncated = len(items) > MAX_GRAPH_PROPERTY_ITEMS
        for item in items[:MAX_GRAPH_PROPERTY_ITEMS]:
            item, item_truncated = _bound_graph_value(item, depth + 1)
            bounded.append(item)
            truncated = truncated or item_truncated
        return bounded, truncated
    preview = str(value)
    return preview[:MAX_GRAPH_PROPERTY_CHARS], len(preview) > MAX_GRAPH_PROPERTY_CHARS


def _bound_graph_properties(properties):
    bounded = {}
    truncated = []
    for key, value in properties.items():
        original_name = str(key)
        name = original_name[:MAX_GRAPH_PROPERTY_CHARS]
        preview, was_truncated = _bound_graph_value(value)
        bounded[name] = preview
        if was_truncated or len(original_name) > MAX_GRAPH_PROPERTY_CHARS:
            truncated.append(name)
    return bounded, truncated


def _graph_json_response(data, truncation_reasons):
    """Return the graph payload under the final serialized HTTP byte limit."""
    page_metadata = {
        key: data[key]
        for key in (
            "offset",
            "page_size",
            "record_count",
            "result_limit",
            "has_more",
            "next_offset",
            "continuation_limit_reached",
        )
        if key in data
    }

    def render():
        data["truncated"] = bool(truncation_reasons)
        data["truncation_reasons"] = sorted(truncation_reasons)
        return JSONResponse(content=jsonable_encoder(data))

    response = render()
    if len(response.body) <= MAX_GRAPH_RESPONSE_BYTES:
        return response

    truncation_reasons.add("serialized_byte_limit")
    page_size = page_metadata.get("page_size")
    offset = page_metadata.get("offset")
    if isinstance(page_size, int) and isinstance(offset, int):
        page_metadata.update(
            {
                "has_more": True,
                "next_offset": None,
                "continuation_limit_reached": False,
                "retry_offset": offset,
                "retry_page_size": max(1, page_size // 2) if page_size > 1 else None,
                "retry_required": page_size > 1,
                "output_limit_unrecoverable": page_size <= 1,
            }
        )
        data.update(page_metadata)
    data["fileContents"] = {}
    data["fileContentsTruncated"] = {}
    response = render()
    while len(response.body) > MAX_GRAPH_RESPONSE_BYTES:
        if data["links"]:
            data["links"] = data["links"][: len(data["links"]) // 2]
        elif data["nodes"]:
            data["nodes"] = data["nodes"][: len(data["nodes"]) // 2]
            node_ids = {node["id"] for node in data["nodes"]}
            data["links"] = [
                link for link in data["links"]
                if link["source"] in node_ids and link["target"] in node_ids
            ]
            node_paths = {node.get("file") for node in data["nodes"] if node.get("file")}
            data["files"] = [path for path in data["files"] if path in node_paths]
        elif data["files"]:
            data["files"] = data["files"][: len(data["files"]) // 2]
        else:
            data = {
                "nodes": [],
                "links": [],
                "files": [],
                "fileContents": {},
                "fileContentsTruncated": {},
                **page_metadata,
            }
        response = render()
    return response

def set_db_manager(manager: DatabaseManager):
    global db_manager
    db_manager = manager

def _get_eid(element):
    if element is None: return None
    if isinstance(element, (int, str)):
        return str(element)
    
    # If element is a dict (like Neo4j returned items or KuzuDB/Ladybug node/rel
    # dicts). KùzuDB spells its internal id field '_id'; LadybugDB spells the
    # same field '_ID' (uppercase) — confirmed against a live query on each
    # embedded backend.
    if isinstance(element, dict):
        # KuzuDB _src / _dst are directly {'offset': X, 'table': Y}
        if 'offset' in element and 'table' in element:
            return f"{element.get('table')}_{element.get('offset')}"

        for key in ['_id', '_ID', 'id', 'element_id']:
            if key in element:
                val = element[key]
                if val is not None:
                    # KuzuDB returns dict IDs like {'offset': 1, 'table': 0} inside nodes
                    if isinstance(val, dict):
                        return f"{val.get('table', 0)}_{val.get('offset', 0)}"
                    return str(val)
        return str(id(element))
        
    # Try various ways to get ID (Neo4j, FalkorDB, etc. objects)
    for attr in ['element_id', 'id', '_id']:
        if hasattr(element, attr):
            val = getattr(element, attr)
            if val is not None:
                # KuzuDB objects if any
                if isinstance(val, dict):
                    return f"{val.get('table', 0)}_{val.get('offset', 0)}"
                return str(val)
    return str(id(element))


def _meta(d: dict, name: str):
    """Read a KùzuDB/LadybugDB internal dict field regardless of its casing.

    KùzuDB spells these fields lowercase (_label/_src/_dst); LadybugDB spells
    the same fields uppercase (_LABEL/_SRC/_DST) — confirmed against a live
    query on each embedded backend. Keying on one casing only silently drops
    every row on the other backend.
    """
    lower = f"_{name}"
    return d[lower] if lower in d else d.get(f"_{name.upper()}")


@app.get("/api/graph")
async def get_graph(
    repo_path: Optional[str] = None,
    cypher_query: Optional[str] = None,
    global_scope: bool = False,
    offset: int = 0,
    page_size: Optional[int] = None,
):
    if not db_manager:
        raise HTTPException(status_code=500, detail="Database not initialized")

    if offset < 0 or offset >= MAX_GRAPH_RESULT_OFFSET:
        raise HTTPException(
            status_code=400,
            detail=f"offset must be between 0 and {MAX_GRAPH_RESULT_OFFSET - 1}.",
        )
    requested_page_size = page_size
    if requested_page_size is None:
        requested_page_size = min(MAX_GRAPH_QUERY_ROWS, MAX_GRAPH_PAGE_SIZE)
    if requested_page_size < 1:
        raise HTTPException(status_code=400, detail="page_size must be positive.")
    page_size = min(
        requested_page_size,
        MAX_GRAPH_QUERY_ROWS,
        MAX_GRAPH_PAGE_SIZE,
        MAX_GRAPH_RESULT_OFFSET - offset,
    )
    if page_size < 1:
        raise HTTPException(status_code=400, detail="page_size must be positive.")
    
    get_eid = _get_eid
    backend = str(getattr(db_manager, "get_backend_type", lambda: "neo4j")()).lower()
    session_kwargs = _read_session_kwargs(db_manager)

    try:
        nodes_dict = {}
        edges = []
        stable_custom_order = False
        query_page_window_applied = False
        graph_query_result_limit: Optional[int] = None

        print(f"DEBUG: Starting get_graph with repo_path={repo_path}", flush=True)

        # This endpoint only ever reads. Open the session in READ access mode so
        # the database enforces read-only (Neo4j default_access_mode / FalkorDB
        # GRAPH.RO_QUERY) in addition to the regex guard on any user query.
        with db_manager.get_driver().session(**session_kwargs) as session:
            if cypher_query:
                if not _is_read_only_cypher(cypher_query):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "This endpoint only supports read-only Cypher queries. "
                            "Prohibited keywords like CREATE, MERGE, DELETE, SET, REMOVE, "
                            "DROP, or CALL apoc are not allowed."
                        ),
                    )
                executed_query = cypher_query
                query_params = {}
                from ..tools.handlers.query_handlers import _has_stable_unique_final_order_by

                stable_custom_order = _has_stable_unique_final_order_by(cypher_query)
                if not global_scope:
                    if not repo_path:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "Custom Cypher queries require repo_path or explicit "
                                "global_scope=true."
                            ),
                        )
                    from ..tools.handlers.query_handlers import _scope_cypher_query

                    try:
                        executed_query, query_params = _scope_cypher_query(
                            cypher_query, repo_path
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                if not stable_custom_order:
                    paged_query, graph_query_result_limit = _bounded_graph_projection_page(
                        executed_query, backend, offset, page_size
                    )
                    if paged_query is not None:
                        executed_query = paged_query
                        stable_custom_order = True
                        query_page_window_applied = True
                    elif offset:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "Paged custom Cypher requires a stable ORDER BY with a unique "
                                "tie-breaker or a bounded n/rel/m graph projection."
                            ),
                        )
                if not query_page_window_applied:
                    bounded_query, has_raw_entity_projection = (
                        _rewrite_bounded_graph_entity_projections(
                            executed_query, backend
                        )
                    )
                    if has_raw_entity_projection:
                        if bounded_query is None:
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    "Custom graph queries must return graph entities as a "
                                    "supported n/rel/m projection so properties can be bounded."
                                ),
                            )
                        executed_query = bounded_query
                print(f"DEBUG: Executing custom query: {executed_query}", flush=True)
                result = session.run(executed_query, **query_params)
            elif repo_path and not global_scope:
                repo_path = Path(repo_path).resolve().as_posix()
                print(f"DEBUG: Fetching subgraph for: {repo_path}", flush=True)
                query = _repository_graph_query(backend, offset, page_size)
                query_page_window_applied = True
                result = session.run(query, repo_path=repo_path)
            else:
                if not global_scope:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "A repository path or explicit global_scope=true is "
                            "required to fetch the graph."
                        ),
                    )
                print("DEBUG: Fetching global graph", flush=True)
                query = (
                    "MATCH (n) OPTIONAL MATCH (n)-[rel]->(m) "
                    f"RETURN {_graph_node_projection('n', backend)} AS n, "
                    f"{_graph_relationship_projection('rel', 'n', 'm', backend)} AS rel, "
                    f"{_graph_node_projection('m', backend)} AS m "
                    f"ORDER BY {_graph_entity_order_key('n', backend)}, "
                    f"{_graph_entity_order_key('rel', backend)}, "
                    f"{_graph_entity_order_key('m', backend)} "
                    f"SKIP {offset} LIMIT {page_size + 1}"
                )
                query_page_window_applied = True
                result = session.run(query)

            result_start = 0 if query_page_window_applied else offset
            result_rows = list(islice(iter(result), result_start, result_start + page_size + 1))
            has_more = len(result_rows) > page_size
            page_rows = result_rows[:page_size]
            if cypher_query and has_more and not stable_custom_order:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "This custom Cypher result spans multiple pages. Add a stable ORDER BY "
                        "with a unique tie-breaker before using visualization pagination."
                    ),
                )

            record_count = 0
            truncation_reasons = set()
            if has_more:
                truncation_reasons.add("row_limit")
            for record in page_rows:
                record_count += 1
                # Use .get() to avoid KeyError if the query doesn't return all fields (n, rel, m)
                for key in ['n', 'm']:
                    try:
                        node = record.get(key)
                        if node:
                            eid = get_eid(node)
                            if eid and eid not in nodes_dict:
                                # Extract labels
                                labels = []
                                if isinstance(node, dict):
                                    # KuzuDB node label is under '_label' (LadybugDB: '_LABEL')
                                    meta_label = _meta(node, 'label')
                                    if meta_label is not None:
                                        labels = [meta_label]
                                    elif 'label' in node:
                                        labels = [node['label']]
                                else:
                                    for label_attr in ['_labels', 'labels']:
                                        if hasattr(node, label_attr):
                                            attr_val = getattr(node, label_attr)
                                            if attr_val:
                                                labels = list(attr_val)
                                                break
                                
                                # Extract properties
                                props = {}
                                projected_property_totals = {}
                                if isinstance(node, dict):
                                    projected_property_totals = {
                                        key[1:-len("_total_chars")]: value
                                        for key, value in node.items()
                                        if key.startswith("_")
                                        and key.endswith("_total_chars")
                                    }
                                    props = {k: v for k, v in node.items() if not k.startswith('_')}
                                else:
                                    for prop_attr in ['properties', '_properties']:
                                        if hasattr(node, prop_attr):
                                            attr_val = getattr(node, prop_attr)
                                            if attr_val:
                                                props = dict(attr_val)
                                                break
                                                
                                    # Fallback if props still empty but node acts like dict
                                    if not props and hasattr(node, 'items'):
                                        try:
                                            props = dict(node.items())
                                        except: pass

                                props, truncated_properties = _bound_graph_properties(props)
                                for property_name, total_chars in projected_property_totals.items():
                                    preview = props.get(property_name)
                                    if (
                                        isinstance(total_chars, int)
                                        and isinstance(preview, str)
                                        and total_chars > len(preview)
                                        and property_name not in truncated_properties
                                    ):
                                        truncated_properties.append(property_name)
                                if truncated_properties:
                                    truncation_reasons.add("property_preview")
                                
                                # Extract name/label for frontend
                                # Prefer 'name' property, fallback to 'label', then 'path' or 'Unknown'
                                display_name = str(props.get('name', props.get('label', props.get('path', 'Unknown'))))
                                if display_name == "<module>":
                                    _fpath = str(props.get('path', props.get('file', '')))
                                    if _fpath:
                                        # Same plain-filename label as the payload builders in
                                        # cli_helpers/visualize_graph, so one node cannot
                                        # render two different names in different views.
                                        display_name = os.path.basename(_fpath)
                                        props['name'] = display_name
                                        props['label'] = display_name
                                
                                nodes_dict[eid] = {
                                    "id": eid,
                                    "name": display_name,
                                    "label": display_name,
                                    "type": str(labels[0]).capitalize() if labels else "Other",
                                    "file": str(props.get('path', props.get('file', ''))),
                                    "val": 4 if (labels and labels[0] in ['Repository', 'Class', 'Interface', 'Trait']) else 2,
                                    "properties": props,
                                    **({"truncatedProperties": truncated_properties} if truncated_properties else {}),
                                }
                    except Exception as e:
                        print(f"DEBUG: Error parsing node: {e}", file=sys.stderr, flush=True)
                        continue
                
                try:
                    rel = record.get('rel')
                    if rel is not None:
                        # One-shot debug: print type and keys/attrs of first rel
                        if not edges:
                            if isinstance(rel, dict):
                                print(f"DEBUG rel (dict): keys={list(rel.keys())}", file=sys.stderr, flush=True)
                            else:
                                print(f"DEBUG rel (obj): type={type(rel).__name__}, attrs={[a for a in dir(rel) if not a.startswith('__')]}", file=sys.stderr, flush=True)

                        # FalkorDB / KuzuDB may return rels as dicts OR objects
                        if isinstance(rel, dict):
                            rid = get_eid(rel)
                            # KuzuDB uses _src/_dst (LadybugDB: _SRC/_DST), FalkorDB uses src_node/dest_node
                            src = _meta(rel, 'src')
                            if src is None:
                                src = rel.get('src_node')
                            dst = _meta(rel, 'dst')
                            if dst is None:
                                dst = rel.get('dest_node')

                            source = get_eid(src) if src is not None else None
                            target = get_eid(dst) if dst is not None else None
                            rel_type = str(_meta(rel, 'label') or rel.get('relation', rel.get('type', 'RELATED'))).upper()
                        else:
                            rid = get_eid(rel)
                            start_node = None
                            end_node = None
                            for src_attr in ['start_node', 'src_node', '_src_node']:
                                if hasattr(rel, src_attr):
                                    start_node = getattr(rel, src_attr)
                                    break
                            for dest_attr in ['end_node', 'dest_node', '_dest_node']:
                                if hasattr(rel, dest_attr):
                                    end_node = getattr(rel, dest_attr)
                                    break
                            source = get_eid(start_node) if start_node is not None else None
                            target = get_eid(end_node) if end_node is not None else None
                            rel_type = "RELATED"
                            for rel_attr in ['type', 'relation', '_relation']:
                                if hasattr(rel, rel_attr):
                                    rel_type = str(getattr(rel, rel_attr)).upper()
                                    break

                        if source and target:
                            edges.append({
                                "id": rid,
                                "source": source,
                                "target": target,
                                "type": rel_type
                            })
                except Exception as e:
                    print(f"DEBUG: Error parsing relationship: {e}", file=sys.stderr, flush=True)
                    pass

        print(f"DEBUG: Processed {record_count} records. extracted {len(nodes_dict)} nodes and {len(edges)} edges.", file=sys.stderr, flush=True)

        # Build a list of unique file paths from File-type nodes for the tree
        file_paths = []
        for n in nodes_dict.values():
            if n.get("file") and str(n.get("type", "")).lower() == "file":
                file_paths.append(str(n["file"]))
        file_paths = sorted(list(set(file_paths)))
        if len(file_paths) > MAX_GRAPH_FILE_PREVIEWS:
            truncation_reasons.add("file_preview_limit")
            file_paths = file_paths[:MAX_GRAPH_FILE_PREVIEWS]

        # Send bounded file previews; full text remains available from /api/file on demand.
        file_contents: dict[str, str] = {}
        file_contents_truncated: dict[str, bool] = {}
        for fp in file_paths:
            try:
                resolved = Path(fp).resolve()
                if not is_path_allowed(resolved):
                    continue
                preview, was_truncated = _safe_read_text_preview(
                    str(resolved), MAX_GRAPH_FILE_PREVIEW_CHARS
                )
                file_contents[fp] = preview
                if was_truncated:
                    file_contents_truncated[fp] = True
                    truncation_reasons.add("file_preview")
            except HTTPException:
                continue
            except Exception:
                pass

        response_data = {
            "nodes": list(nodes_dict.values()), 
            "links": edges,
            "files": file_paths,
            "fileContents": file_contents,
            "fileContentsTruncated": file_contents_truncated,
            "offset": offset,
            "page_size": page_size,
            "has_more": has_more,
            "next_offset": (
                offset + record_count
                if (
                    has_more
                    and offset + record_count < MAX_GRAPH_RESULT_OFFSET
                    and (
                        graph_query_result_limit is None
                        or offset + record_count < graph_query_result_limit
                    )
                )
                else None
            ),
            "continuation_limit_reached": (
                (
                    has_more and offset + record_count >= MAX_GRAPH_RESULT_OFFSET
                )
                or (
                    graph_query_result_limit is not None
                    and offset + record_count >= graph_query_result_limit
                )
            ),
            "record_count": record_count,
        }
        if has_more:
            response_data["result_limit"] = page_size
        elif graph_query_result_limit is not None:
            response_data["result_limit"] = graph_query_result_limit
        if (
            graph_query_result_limit is not None
            and offset + record_count >= graph_query_result_limit
        ):
            truncation_reasons.add("query_result_limit")
        
        print(f"API SUCCESS: Returning graph with {len(response_data['nodes'])} nodes and {len(response_data['links'])} links.", file=sys.stderr, flush=True)
        return _graph_json_response(response_data, truncation_reasons)

    except HTTPException:
        # Preserve intentional HTTP errors (e.g. 400 for forbidden write queries).
        raise
    except Exception as e:
        debug_log(f"Error fetching graph: {str(e)}")
        import traceback
        traceback.print_exc()
        # Still return a valid structure so the frontend doesn't crash, but with 500 status if raised
        # Actually, let's just return a 500 error but with JSON body if possible
        raise HTTPException(status_code=500, detail=str(e))

def call_llm(system_prompt: str, user_prompt: str) -> str:
    gemini_key = os.getenv("GEMINI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    atlascloud_key = os.getenv("ATLASCLOUD_API_KEY") or os.getenv("ATLAS_CLOUD_API_KEY")
    cheaperinference_key = os.getenv("CHEAPER_INFERENCE_API_KEY")
    api_route_key = os.getenv("API_ROUTE_API_KEY")
    
    if not gemini_key and not openai_key and not atlascloud_key and not cheaperinference_key and not api_route_key:
        raise ValueError(
            "GEMINI_API_KEY, OPENAI_API_KEY, ATLASCLOUD_API_KEY, CHEAPER_INFERENCE_API_KEY, or API_ROUTE_API_KEY "
            "environment variable is required."
        )
    
    if gemini_key:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}"
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": f"{system_prompt}\n\nUser Prompt:\n{user_prompt}"}]
                }
            ]
        }
        res = requests.post(url, json=payload, timeout=30)
        if res.status_code != 200:
            raise RuntimeError(f"Gemini API returned error {res.status_code}: {res.text}")
        data = res.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Failed to parse Gemini API response: {data}") from e
    else:
        if openai_key:
            provider_name = "OpenAI"
            api_key = openai_key
            url = "https://api.openai.com/v1/chat/completions"
            model = "gpt-4o-mini"
        elif atlascloud_key:
            provider_name = "Atlas Cloud"
            api_key = atlascloud_key
            base_url = os.getenv("ATLASCLOUD_API_BASE") or os.getenv("ATLAS_CLOUD_API_BASE")
            base_url = (base_url or "https://api.atlascloud.ai/v1").rstrip("/")
            url = f"{base_url}/chat/completions"
            model = os.getenv("ATLASCLOUD_MODEL") or os.getenv("ATLAS_CLOUD_MODEL")
            model = model or "deepseek-ai/deepseek-v4-pro"
        elif cheaperinference_key:
            provider_name = "Cheaper Inference"
            api_key = cheaperinference_key
            base_url = os.getenv("CHEAPER_INFERENCE_API_BASE")
            base_url = (base_url or "https://api.cheaperinference.com/v1").rstrip("/")
            url = f"{base_url}/chat/completions"
            model = os.getenv("CHEAPER_INFERENCE_MODEL") or "gpt-5.4-mini"
        else:
            provider_name = "API Route"
            api_key = api_route_key
            base_url = os.getenv("API_ROUTE_API_BASE")
            base_url = (base_url or "https://global.api-route.com/v1").rstrip("/")
            url = f"{base_url}/chat/completions"
            model = os.getenv("API_ROUTE_MODEL") or "gpt-6.1-sol"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        }
        res = requests.post(url, json=payload, headers=headers, timeout=30)
        if res.status_code != 200:
            raise RuntimeError(f"{provider_name} API returned error {res.status_code}: {res.text}")
        data = res.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Failed to parse {provider_name} API response: {data}") from e


def parse_json_from_llm(text: str) -> dict:
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1)
    else:
        match = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            text = match.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\})", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse valid JSON from LLM response:\n{text}")


def parse_node(node, nodes_dict):
    eid = _get_eid(node)
    if not eid or eid in nodes_dict:
        return
        
    labels = []
    if isinstance(node, dict):
        meta_label = _meta(node, 'label')
        if meta_label is not None:
            labels = [meta_label]
        elif 'label' in node:
            labels = [node['label']]
    else:
        for label_attr in ['_labels', 'labels']:
            if hasattr(node, label_attr):
                attr_val = getattr(node, label_attr)
                if attr_val:
                    labels = list(attr_val)
                    break
                    
    props = {}
    if isinstance(node, dict):
        props = {k: v for k, v in node.items() if not k.startswith('_')}
    else:
        for prop_attr in ['properties', '_properties']:
            if hasattr(node, prop_attr):
                attr_val = getattr(node, prop_attr)
                if attr_val:
                    props = dict(attr_val)
                    break
        if not props and hasattr(node, 'items'):
            try:
                props = dict(node.items())
            except: pass
            
    display_name = str(props.get('name', props.get('label', props.get('path', 'Unknown'))))
    if display_name == "<module>":
        _fpath = str(props.get('path', props.get('file', '')))
        if _fpath:
            display_name = os.path.basename(_fpath)
            props['name'] = display_name
            props['label'] = display_name
    
    nodes_dict[eid] = {
        "id": eid,
        "name": display_name,
        "label": display_name,
        "type": str(labels[0]).capitalize() if labels else "Other",
        "file": str(props.get('path', props.get('file', ''))),
        "val": 4 if (labels and labels[0] in ['Repository', 'Class', 'Interface', 'Trait']) else 2,
        "properties": props
    }


def parse_rel(rel, edges):
    rid = _get_eid(rel)
    if isinstance(rel, dict):
        src = _meta(rel, 'src')
        if src is None:
            src = rel.get('src_node')
        dst = _meta(rel, 'dst')
        if dst is None:
            dst = rel.get('dest_node')
        source = _get_eid(src) if src is not None else None
        target = _get_eid(dst) if dst is not None else None
        rel_type = str(_meta(rel, 'label') or rel.get('relation', rel.get('type', 'RELATED'))).upper()
    else:
        start_node = None
        end_node = None
        for src_attr in ['start_node', 'src_node', '_src_node']:
            if hasattr(rel, src_attr):
                start_node = getattr(rel, src_attr)
                break
        for dest_attr in ['end_node', 'dest_node', '_dest_node']:
            if hasattr(rel, dest_attr):
                end_node = getattr(rel, dest_attr)
                break
        source = _get_eid(start_node) if start_node is not None else None
        target = _get_eid(end_node) if end_node is not None else None
        rel_type = "RELATED"
        for rel_attr in ['type', 'relation', '_relation']:
            if hasattr(rel, rel_attr):
                rel_type = str(getattr(rel, rel_attr)).upper()
                break
                
    if source and target:
        edges.append({
            "id": rid,
            "source": source,
            "target": target,
            "type": rel_type
        })


def parse_element(val, nodes_dict, edges):
    if val is None:
        return
    
    # If it is a Path
    if hasattr(val, 'nodes') and hasattr(val, 'relationships'):
        for n in val.nodes:
            parse_element(n, nodes_dict, edges)
        for r in val.relationships:
            parse_element(r, nodes_dict, edges)
        return
        
    # Classification is duck-typed and shared with the offline renderer (see
    # utils/graph_shapes). Matching on class name used to miss FalkorDB's
    # `Edge`, so every FalkorDB relationship was dropped here even though
    # `parse_rel` below reads it correctly.
    if graph_shapes.is_node(val):
        parse_node(val, nodes_dict)
    elif graph_shapes.is_relationship(val):
        parse_rel(val, edges)
    elif isinstance(val, (list, tuple)):
        for item in val:
            parse_element(item, nodes_dict, edges)


class AIQueryRequest(BaseModel):
    query: str
    repo_path: Optional[str] = None


@app.post("/api/ai_query")
async def ai_query(request: AIQueryRequest):
    if not db_manager:
        raise HTTPException(status_code=500, detail="Database not initialized")
        
    gemini_key = os.getenv("GEMINI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    atlascloud_key = os.getenv("ATLASCLOUD_API_KEY") or os.getenv("ATLAS_CLOUD_API_KEY")
    cheaperinference_key = os.getenv("CHEAPER_INFERENCE_API_KEY")
    api_route_key = os.getenv("API_ROUTE_API_KEY")
    
    if not gemini_key and not openai_key and not atlascloud_key and not cheaperinference_key and not api_route_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "AI querying requires GEMINI_API_KEY, OPENAI_API_KEY, ATLASCLOUD_API_KEY, "
                "CHEAPER_INFERENCE_API_KEY, or API_ROUTE_API_KEY. "
                "Please set one in your configuration or .env."
            )
        )
        
    query = request.query
    repo_path = request.repo_path
    
    translation_prompt = """You are an expert graph database administrator. Translate the user's natural language question about a codebase into a valid read-only Cypher query.

The graph database represents the codebase structure with the following schema:
### Nodes & Properties:
* **`Repository`**: `name` (string), `path` (string, absolute path), `is_dependency` (boolean)
* **`File`**: `name` (string), `path` (string, absolute path), `relative_path` (string), `is_dependency` (boolean)
* **`Function`**: `name` (string), `path` (string, absolute path), `line_number` (int), `end_line` (int), `args` (list of strings), `cyclomatic_complexity` (int), `decorators` (list of strings), `lang` (string), `source` (string), `is_dependency` (boolean)
* **`Class`**: `name` (string), `path` (string, absolute path), `line_number` (int), `end_line` (int), `bases` (list of strings), `decorators` (list of strings), `lang` (string), `source` (string), `is_dependency` (boolean)
* **`Interface`**, **`Struct`**, **`Enum`**, **`Trait`**: similar properties to Class/Function
* **`Module`**: `name` (string)

### Relationships:
* **`CONTAINS`**: (Repository)-[:CONTAINS]->(File), (File)-[:CONTAINS]->(Function), (File)-[:CONTAINS]->(Class)
* **`CALLS`**: (Function)-[:CALLS]->(Function)
* **`IMPORTS`**: (File)-[:IMPORTS]->(Module)
* **`INHERITS`**: (Class)-[:INHERITS]->(Class)
* **`IMPLEMENTS`**: (Class)-[:IMPLEMENTS]->(Interface)

### Query Writing Rules:
1. Only write SELECT/MATCH read-only queries. Do NOT use CREATE, MERGE, DELETE, SET, REMOVE, or other modifying keywords.
2. Return nodes using the variable names `n` and `m`, and relationships using `rel` where possible. This is critical for the visualizer.
   Example: MATCH (n:Class)-[rel:INHERITS]->(m:Class) RETURN n, rel, m
3. Keep the query simple and efficient. Limit results using `LIMIT 150` unless more are needed.
4. If the user asks about dependencies or paths, try to return paths using variables, and return nodes/relationships.
5. Make property matches case-insensitive or fuzzy if appropriate (e.g. using `toLower(n.name) CONTAINS 'auth'` or `n.name =~ '(?i).*auth.*'`).
6. If the user is querying a specific repository, you can filter by `Repository.path` or `Repository.name` if path is supplied.

Respond ONLY with a JSON object in this format:
{
  "cypher_query": "MATCH ... RETURN n, rel, m LIMIT 100",
  "explanation": "Brief description of what this Cypher query does"
}
"""
    
    try:
        # Call LLM to get Cypher
        llm_response = call_llm(translation_prompt, query)
        parsed_response = parse_json_from_llm(llm_response)
        cypher_query = parsed_response.get("cypher_query", "")
        translation_explanation = parsed_response.get("explanation", "")
        
        if not cypher_query:
            raise ValueError("No Cypher query was generated by the AI.")
            
        # Validate Cypher
        if not _is_read_only_cypher(cypher_query):
            raise HTTPException(
                status_code=400,
                detail=f"The generated Cypher query was rejected for safety reasons: {read_only_rejection_message()}"
            )
            
        # Step 2: Run Cypher query
        nodes_dict = {}
        edges = []
        records_data = []

        with db_manager.get_driver().session(**_read_session_kwargs(db_manager)) as session:
            result = session.run(cypher_query)
            for record in result:
                # Store truncated record data for explanation phase
                record_dict = {}
                for k, v in record.items():
                    if isinstance(v, dict):
                        record_dict[k] = {prop_k: (prop_v[:200] + "..." if isinstance(prop_v, str) and len(prop_v) > 200 else prop_v) for prop_k, prop_v in v.items()}
                    elif hasattr(v, 'properties'):
                        props = dict(v.properties)
                        record_dict[k] = {prop_k: (prop_v[:200] + "..." if isinstance(prop_v, str) and len(prop_v) > 200 else prop_v) for prop_k, prop_v in props.items()}
                    else:
                        record_dict[k] = str(v)[:200] + "..." if isinstance(v, str) and len(v) > 200 else v
                records_data.append(record_dict)
                
                # Parse elements for visualization
                for val in record.values():
                    parse_element(val, nodes_dict, edges)
                    
        # Truncate results for LLM context (e.g. limit to first 30 records to save tokens)
        results_for_explanation = records_data[:30]
        results_json = json.dumps(results_for_explanation, indent=2)
        
        # Step 3: Generate Explanation
        explanation_prompt = f"""You are an expert software developer and repository assistant. Explain the answer to the user's question about the codebase based on the query results.

User Question: {query}
Cypher Query Executed: {cypher_query}
Query Results (truncated if large):
{results_json}

Write a clear, concise, and professional explanation of the results in Markdown format.
Focus on explaining:
1. What the query found.
2. The architectural relationships between the components.
3. How this answers the user's question.

After your explanation, provide a section:
### Context-Aware Recommendations
Suggest 2-3 specific follow-up questions or related areas the user might want to explore based on this result. Keep suggestions bulleted and actionable.
"""
        explanation = call_llm(explanation_prompt, "Explain the results.")
        
        return {
            "success": True,
            "cypher_query": cypher_query,
            "translation_explanation": translation_explanation,
            "explanation": explanation,
            "nodes": list(nodes_dict.values()),
            "links": edges
        }
        
    except HTTPException:
        # Preserve intentional HTTP errors (e.g. the 400 raised above when the
        # generated Cypher is not read-only); re-wrapping them as 500 made a
        # safety rejection look like a server failure to the frontend.
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/file")
async def get_file(path: str, offset: int = 0, page_size: Optional[int] = None):
    try:
        if offset < 0 or offset >= MAX_GRAPH_FILE_OFFSET:
            raise HTTPException(
                status_code=400,
                detail=f"offset must be between 0 and {MAX_GRAPH_FILE_OFFSET - 1}.",
            )
        requested_page_size = page_size
        if requested_page_size is None:
            requested_page_size = MAX_GRAPH_FILE_PAGE_CHARS
        if requested_page_size < 1:
            raise HTTPException(status_code=400, detail="page_size must be positive.")
        effective_page_size = min(
            requested_page_size,
            MAX_GRAPH_FILE_PAGE_CHARS,
            MAX_GRAPH_FILE_OFFSET - offset,
        )
        content, has_more = _safe_read_text_page(path, offset, effective_page_size)
        page_end = offset + len(content)
        continuation_limit_reached = has_more and page_end >= MAX_GRAPH_FILE_OFFSET
        return {
            "content": content,
            "offset": offset,
            "page_size": effective_page_size,
            "line_limit": MAX_GRAPH_FILE_PAGE_LINES,
            "truncated": has_more,
            "has_more": has_more,
            "next_offset": (
                page_end
                if has_more and not continuation_limit_reached
                else None
            ),
            "continuation_limit_reached": continuation_limit_reached,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# SPA fallback handler
@app.get("/{full_path:path}")
async def spa_fallback(request: Request, full_path: str):
    try:
        root = _static_root()
    except HTTPException as exc:
        return HTMLResponse(exc.detail, status_code=exc.status_code)

    try:
        file_path = _safe_static_file(full_path)
    except HTTPException:
        file_path = None

    if file_path and file_path.exists() and file_path.is_file():
        return FileResponse(file_path)

    index_path = root / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    
    return HTMLResponse("Not Found (Built UI not found in viz/dist)", status_code=404)

def run_server(host: str = "127.0.0.1", port: int = 8000, static_dir: Optional[str] = None):
    global _static_dir
    _static_dir = static_dir
    uvicorn.run(app, host=host, port=port)
