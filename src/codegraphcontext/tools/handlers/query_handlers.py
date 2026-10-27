# src/codegraphcontext/tools/handlers/query_handlers.py
import urllib.parse
import re
from pathlib import Path
from typing import Any, Dict

try:
    from neo4j.exceptions import CypherSyntaxError
except ImportError:  # neo4j driver not installed (FalkorDB/Kuzu-only setups)
    CypherSyntaxError = type("CypherSyntaxError", (Exception,), {})

from ...utils.cypher_readonly import is_read_only_cypher, read_only_rejection_message
from ...utils.debug_log import debug_log
from ...utils.tool_limits import get_tool_result_limit
from ..code_finder import CodeFinder


_QUERY_LITERAL_OR_COMMENT_RE = re.compile(
    r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|//[^\n]*|/\*.*?\*/",
    re.DOTALL,
)
_CYPHER_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_CYPHER_NODE_NAME_RE = re.compile(r"\s*(`[^`]+`|[A-Za-z_]\w*)")
_CYPHER_LABEL_RE = re.compile(r"[:|&]\s*(`[^`]+`|[A-Za-z_]\w*)")
_CYPHER_NESTED_PATTERN_RE = re.compile(
    r"\)\s*(?:-|<-)\s*\[|\]\s*(?:-|<-)\s*\("
)
_CYPHER_UNBRACKETED_RELATIONSHIP_RE = re.compile(
    r"\)\s*(?:-->|<--|--|<-|->)\s*\("
)
_CYPHER_VARIABLE_LENGTH_RELATIONSHIP_RE = re.compile(
    r"-\s*\[\s*(?:`[^`]+`|[A-Za-z_]\w*)?\s*"
    r"(?::\s*(?:`[^`]+`|[A-Za-z_]\w*)"
    r"(?:\s*[|&]\s*(?:`[^`]+`|[A-Za-z_]\w*))*)?\s*\*[^]]*\]\s*-",
    re.IGNORECASE,
)
_CYPHER_NESTED_NODE_PATTERN_RE = re.compile(
    r"\[\s*(?:[A-Za-z_]\w*\s*=\s*)?\(\s*(?:`[^`]+`|[A-Za-z_]\w*)?\s*"
    r"(?::|[|&]|\{|\)\s*(?:WHERE\b|\|))",
    re.IGNORECASE,
)
_PATH_BEARING_NODE_LABELS = frozenset({
    "Annotation",
    "Class",
    "Directory",
    "DbTable",
    "Enum",
    "EnumMember",
    "Extension",
    "ExternalClass",
    "File",
    "Function",
    "GradleModule",
    "Interface",
    "Macro",
    "Mixin",
    "MavenModule",
    "Object",
    "Parameter",
    "Property",
    "Record",
    "Repository",
    "Struct",
    "Trait",
    "Union",
    "Variable",
})
_NODE_PRIMARY_KEYS = {
    "Annotation": "uid",
    "Class": "uid",
    "Datasource": "name",
    "DbColumn": "uid",
    "DbTable": "fqn",
    "Directory": "path",
    "Enum": "uid",
    "EnumMember": "uid",
    "Extension": "uid",
    "ExternalClass": "name",
    "ExternalLibrary": "uid",
    "File": "path",
    "Function": "uid",
    "GradleModule": "name",
    "Interface": "uid",
    "Macro": "uid",
    "MavenModule": "uid",
    "Mixin": "uid",
    "Module": "name",
    "Object": "uid",
    "Parameter": "uid",
    "Property": "uid",
    "RedisKeyPattern": "uid",
    "Record": "uid",
    "Repository": "path",
    "Struct": "uid",
    "Trait": "uid",
    "Union": "uid",
    "Variable": "uid",
}
_PATHLESS_SCOPED_NODE_LABELS = frozenset({"Module"})
_EXECUTE_CYPHER_MAX_OFFSET = 100_000


def _require_integer_argument(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer.")
    return value


def _mask_query_literals_and_comments(query: str) -> str:
    def mask(match):
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return _QUERY_LITERAL_OR_COMMENT_RE.sub(mask, query)


def _top_level_cypher_clauses(masked_query: str) -> list[tuple[str, int, int]]:
    clauses = []
    depth = 0
    index = 0
    while index < len(masked_query):
        char = masked_query[index]
        if char == "`":
            end = masked_query.find("`", index + 1)
            index = len(masked_query) if end < 0 else end + 1
            continue
        if char in "([{":
            depth += 1
            index += 1
            continue
        if char in ")]}" and depth:
            depth -= 1
            index += 1
            continue
        if depth or not (char.isalpha() or char == "_"):
            index += 1
            continue

        match = _CYPHER_IDENTIFIER_RE.match(masked_query, index)
        if not match:
            index += 1
            continue
        word = match.group(0).upper()
        end = match.end()
        previous_index = index - 1
        while previous_index >= 0 and masked_query[previous_index].isspace():
            previous_index -= 1
        if previous_index >= 0 and masked_query[previous_index] == ".":
            index = end
            continue
        if word == "OPTIONAL":
            next_match = re.match(r"\s+MATCH\b", masked_query[end:], re.IGNORECASE)
            if next_match:
                end += next_match.end()
                clauses.append(("OPTIONAL MATCH", index, end))
                index = end
                continue
        elif word == "ORDER":
            next_by = re.match(r"\s+BY\b", masked_query[end:], re.IGNORECASE)
            if next_by:
                end += next_by.end()
                clauses.append(("ORDER BY", index, end))
                index = end
                continue
        elif word in {"MATCH", "WHERE", "WITH", "RETURN", "UNION", "SKIP", "LIMIT", "UNWIND", "CALL"}:
            clauses.append((word, index, end))
        index = end
    return clauses


def _split_top_level_cypher_expressions(expression: str) -> list[str]:
    """Split a masked Cypher projection at commas outside nested expressions."""
    expressions = []
    start = 0
    depth = 0
    index = 0
    while index < len(expression):
        char = expression[index]
        if char == "`":
            end = expression.find("`", index + 1)
            index = len(expression) if end < 0 else end + 1
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            expressions.append(expression[start:index])
            start = index + 1
        index += 1
    expressions.append(expression[start:])
    return expressions


def _carry_cypher_node_labels_through_with(
    projection: str, bound_node_labels: Dict[str, list[str]]
) -> Dict[str, list[str]]:
    """Keep node-label knowledge only for direct node variables projected by WITH."""
    projected_labels: Dict[str, list[str]] = {}
    projection_item_re = re.compile(
        r"(`[^`]+`|[A-Za-z_]\w*)(?:\s+AS\s+(`[^`]+`|[A-Za-z_]\w*))?",
        re.IGNORECASE,
    )
    for item in _split_top_level_cypher_expressions(projection):
        item = item.strip()
        if item.upper() == "*":
            projected_labels.update(
                (variable, list(labels))
                for variable, labels in bound_node_labels.items()
            )
            continue
        distinct_prefix = re.match(r"DISTINCT\b\s+", item, re.IGNORECASE)
        if distinct_prefix:
            item = item[distinct_prefix.end():].lstrip()
        match = projection_item_re.fullmatch(item)
        if not match:
            continue
        source = match.group(1)
        target = match.group(2) or source
        if source in bound_node_labels:
            projected_labels[target] = list(bound_node_labels[source])
    return projected_labels


def _has_nested_match_clause(masked_query: str) -> bool:
    """Fail closed on MATCH clauses inside expressions the top-level parser skips."""
    depth = 0
    index = 0
    while index < len(masked_query):
        char = masked_query[index]
        if char == "`":
            end = masked_query.find("`", index + 1)
            index = len(masked_query) if end < 0 else end + 1
            continue
        if char in "([{":
            depth += 1
            index += 1
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth and (char.isalpha() or char == "_"):
            match = _CYPHER_IDENTIFIER_RE.match(masked_query, index)
            if match:
                if match.group(0).upper() == "MATCH":
                    return True
                index = match.end()
                continue
        index += 1
    return False


def _has_nested_call_clause(masked_query: str) -> bool:
    """Detect procedure and subquery CALL clauses inside nested expressions."""
    depth = 0
    index = 0
    while index < len(masked_query):
        char = masked_query[index]
        if char == "`":
            end = masked_query.find("`", index + 1)
            index = len(masked_query) if end < 0 else end + 1
            continue
        if char in "([{":
            depth += 1
            index += 1
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth and (char.isalpha() or char == "_"):
            match = _CYPHER_IDENTIFIER_RE.match(masked_query, index)
            if match:
                if match.group(0).upper() == "CALL":
                    previous_index = index - 1
                    while previous_index >= 0 and masked_query[previous_index].isspace():
                        previous_index -= 1
                    next_index = match.end()
                    while next_index < len(masked_query) and masked_query[next_index].isspace():
                        next_index += 1
                    is_procedure_or_subquery = (
                        next_index < len(masked_query)
                        and (
                            masked_query[next_index] == "{"
                            or _CYPHER_IDENTIFIER_RE.match(masked_query, next_index)
                        )
                    )
                    is_property = previous_index >= 0 and masked_query[previous_index] == "."
                    if is_procedure_or_subquery and not is_property:
                        return True
                index = match.end()
                continue
        index += 1
    return False


def _match_node_pattern_segments(pattern: str) -> list[list[tuple[str, list[str]]]]:
    """Return named nodes grouped by comma-separated MATCH pattern segment."""
    segments: list[list[tuple[str, list[str]]]] = [[]]
    index = 0
    paren_depth = bracket_depth = brace_depth = 0
    node_start = None

    while index < len(pattern):
        char = pattern[index]
        if char == "`":
            end = pattern.find("`", index + 1)
            index = len(pattern) if end < 0 else end + 1
            continue
        if char == "(" and not paren_depth and not bracket_depth and not brace_depth:
            node_start = index
        if char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth = max(0, paren_depth - 1)
            if not paren_depth and node_start is not None:
                node = pattern[node_start + 1:index]
                node_start = None
                variable_match = _CYPHER_NODE_NAME_RE.match(node)
                if not variable_match:
                    segments[-1].append(("", []))
                else:
                    variable = variable_match.group(1)
                    remaining = node[variable_match.end():].split("{", 1)[0]
                    labels = [match.group(1) for match in _CYPHER_LABEL_RE.finditer(remaining)]
                    segments[-1].append((variable, [label.strip("`") for label in labels]))
        elif char == "[":
            bracket_depth += 1
        elif char == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth = max(0, brace_depth - 1)
        elif char == "," and not paren_depth and not bracket_depth and not brace_depth:
            segments.append([])
        index += 1

    return segments


def _scoped_match_node_variables(
    pattern: str, bound_node_labels: Dict[str, list[str]] | None = None
) -> list[tuple[str, bool]]:
    """Return scoped variables and whether each may be a pathless Module."""
    if bound_node_labels is None:
        bound_node_labels = {}
    previously_bound_variables = set(bound_node_labels)
    variables: list[tuple[str, bool]] = []
    for segment in _match_node_pattern_segments(pattern):
        path_nodes = []
        module_nodes = []
        for variable, labels in segment:
            if not variable:
                raise ValueError(
                    "Repository scope requires every matched node to have a named, "
                    "path-bearing label; set global_scope=true explicitly."
                )
            if not labels:
                labels = bound_node_labels.get(variable, [])
            if not labels:
                raise ValueError(
                    "Repository scope cannot be applied to unlabeled node; "
                    "set global_scope=true explicitly."
                )
            if len(labels) == 1 and labels[0] in _PATHLESS_SCOPED_NODE_LABELS:
                module_nodes.append(variable)
            elif any(label not in _PATH_BEARING_NODE_LABELS for label in labels):
                label_summary = ", ".join(labels)
                raise ValueError(
                    f"Repository scope cannot be applied to {label_summary}; "
                    "set global_scope=true explicitly."
                )
            else:
                path_nodes.append(variable)
            bound_node_labels[variable] = list(labels)

        has_path_bearing_file = any(
            "File" in labels and all(label in _PATH_BEARING_NODE_LABELS for label in labels)
            for _, labels in segment
        )
        modules_previously_bound = all(
            variable in previously_bound_variables for variable in module_nodes
        )
        if module_nodes and not has_path_bearing_file and not modules_previously_bound:
            raise ValueError(
                "Repository scope requires each pathless Module to be connected "
                "in the same pattern to a path-bearing File; set global_scope=true explicitly."
            )
        variables.extend((variable, False) for variable in path_nodes)
        variables.extend((variable, True) for variable in module_nodes)

    return list(dict.fromkeys(variables))


def _has_unscoped_graph_pattern(query: str, scoped_ranges: list[tuple[int, int]]) -> bool:
    for pattern_re in (
        _CYPHER_NESTED_PATTERN_RE,
        _CYPHER_NESTED_NODE_PATTERN_RE,
        _CYPHER_UNBRACKETED_RELATIONSHIP_RE,
    ):
        for match in pattern_re.finditer(query):
            if not any(start <= match.start() < end for start, end in scoped_ranges):
                return True
    return False


def _has_stable_unique_final_order_by(query: str) -> bool:
    """Require each bound node's primary key in the final ORDER BY for paging."""
    masked = _mask_query_literals_and_comments(query)
    clauses = _top_level_cypher_clauses(masked)
    # A node-key tuple does not identify a result row when paths, procedures,
    # UNWIND, UNION, or WITH aliases/rebindings can change row identity.
    if any(name in {"CALL", "UNWIND", "UNION", "WITH"} for name, _, _ in clauses):
        return False
    if (
        _CYPHER_NESTED_PATTERN_RE.search(masked)
        or _CYPHER_UNBRACKETED_RELATIONSHIP_RE.search(masked)
    ):
        return False
    order_by_index = next(
        (
            index for index, (name, _, _) in enumerate(clauses)
            if name == "ORDER BY"
            and all(later_name in {"SKIP", "LIMIT"} for later_name, _, _ in clauses[index + 1:])
        ),
        None,
    )
    if order_by_index is None:
        return False
    if any(name in {"SKIP", "LIMIT"} for name, _, _ in clauses[:order_by_index]):
        return False

    order_start = clauses[order_by_index][2]
    next_clause_index = order_by_index + 1
    order_end = clauses[next_clause_index][1] if next_clause_index < len(clauses) else len(masked)
    order_text = masked[order_start:order_end]
    return_aliases = {}
    return_clauses = [
        clause for clause in clauses[:order_by_index]
        if clause[0] == "RETURN"
    ]
    if len(return_clauses) == 1:
        return_clause = return_clauses[0]
        projection = masked[return_clause[2]:clauses[order_by_index][1]]
        direct_projection = re.fullmatch(
            r"\s*(?:DISTINCT\s+)?(`[^`]+`|[A-Za-z_]\w*)\s+AS\s+"
            r"(`[^`]+`|[A-Za-z_]\w*)\s*",
            projection,
            re.IGNORECASE,
        )
        if direct_projection:
            source = direct_projection.group(1).strip("`")
            alias = direct_projection.group(2).strip("`")
            return_aliases[alias] = source

    order_items = []
    item_start = 0
    depth = 0
    index = 0
    while index < len(order_text):
        char = order_text[index]
        if char == "`":
            end = order_text.find("`", index + 1)
            if end < 0:
                return False
            index = end + 1
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            order_items.append(order_text[item_start:index].strip())
            item_start = index + 1
        index += 1
    order_items.append(order_text[item_start:].strip())

    ordered_properties = set()
    for item in order_items:
        match = re.fullmatch(
            r"(`[^`]+`|[A-Za-z_]\w*)\s*\.\s*(`[^`]+`|[A-Za-z_]\w*)"
            r"(?:\s+(?:ASC|DESC))?",
            item,
            re.IGNORECASE,
        )
        if match:
            variable = match.group(1).strip("`")
            prop = match.group(2).strip("`")
            variable = return_aliases.get(variable, variable)
            ordered_properties.add((variable, prop))

    match_clauses = [
        index for index, (name, _, _) in enumerate(clauses)
        if name in {"MATCH", "OPTIONAL MATCH"}
    ]
    if not match_clauses:
        return False

    required_properties = set()
    for clause_index in match_clauses:
        _, _, match_end = clauses[clause_index]
        next_index = clause_index + 1
        pattern_end = clauses[next_index][1] if next_index < len(clauses) else len(masked)
        if next_index < len(clauses) and clauses[next_index][0] == "WHERE":
            pattern_end = clauses[next_index][1]
        for segment in _match_node_pattern_segments(masked[match_end:pattern_end]):
            for variable, labels in segment:
                if not variable or not labels:
                    return False
                keys = {_NODE_PRIMARY_KEYS.get(label) for label in labels}
                if None in keys or len(keys) != 1:
                    return False
                required_properties.add((variable.strip("`"), next(iter(keys))))

    return bool(required_properties) and required_properties <= ordered_properties


def _scope_cypher_query(query: str, repo_path: str) -> tuple[str, Dict[str, str]]:
    """Add parameterized path predicates to MATCH patterns for one repository."""
    root = Path(repo_path).expanduser().resolve().as_posix().rstrip("/") or "/"
    prefix = root if root.endswith("/") else root + "/"
    masked = _mask_query_literals_and_comments(query)
    if _CYPHER_VARIABLE_LENGTH_RELATIONSHIP_RE.search(masked):
        raise ValueError(
            "Repository scope cannot guarantee variable-length relationship paths; "
            "set global_scope=true explicitly."
        )
    if _has_nested_match_clause(masked):
        raise ValueError(
            "Repository scope cannot be guaranteed for nested MATCH clauses; "
            "set global_scope=true explicitly."
        )
    if _has_nested_call_clause(masked):
        raise ValueError(
            "Repository scope cannot be guaranteed for nested CALL procedure clauses, which may "
            "return global graph metadata; set global_scope=true explicitly."
        )
    clauses = _top_level_cypher_clauses(masked)
    matches = [i for i, clause in enumerate(clauses) if clause[0] in {"MATCH", "OPTIONAL MATCH"}]
    if not matches:
        raise ValueError(
            "Repository scope could not be applied because the query has no MATCH clause; "
            "set global_scope=true explicitly."
        )
    if any(name == "CALL" for name, _, _ in clauses):
        raise ValueError(
            "Repository scope cannot be guaranteed for CALL procedure clauses, which may "
            "return global graph metadata; set global_scope=true explicitly."
        )
    insertions: list[tuple[int, str]] = []
    scoped_ranges: list[tuple[int, int]] = []
    bound_node_labels: Dict[str, list[str]] = {}
    last_match_clause_index = -1
    trailing_comment_at_eof = any(
        match.group(0).startswith("//") and match.end() == len(query)
        for match in _QUERY_LITERAL_OR_COMMENT_RE.finditer(query)
    )

    for clause_index in matches:
        for preceding_index in range(last_match_clause_index + 1, clause_index):
            preceding_name, _, preceding_end = clauses[preceding_index]
            if preceding_name == "UNION":
                bound_node_labels.clear()
            elif preceding_name == "WITH":
                projection_end = (
                    clauses[preceding_index + 1][1]
                    if preceding_index + 1 < len(clauses)
                    else len(masked)
                )
                bound_node_labels = _carry_cypher_node_labels_through_with(
                    masked[preceding_end:projection_end], bound_node_labels
                )
        _, _, match_end = clauses[clause_index]
        next_index = clause_index + 1
        next_start = clauses[next_index][1] if next_index < len(clauses) else len(query)
        where_index = next_index if next_index < len(clauses) and clauses[next_index][0] == "WHERE" else None
        pattern_end = next_start
        insertion_position = next_start
        if where_index is not None:
            next_where_index = where_index + 1
            pattern_end = clauses[where_index][1]
            insertion_position = clauses[next_where_index][1] if next_where_index < len(clauses) else len(query)
            if not (trailing_comment_at_eof and insertion_position == len(query)):
                while insertion_position > clauses[where_index][2] and query[insertion_position - 1].isspace():
                    if query[insertion_position - 1] in "\r\n":
                        break
                    insertion_position -= 1

        pattern = masked[match_end:pattern_end]
        scoped_ranges.append((match_end, pattern_end))
        variables = _scoped_match_node_variables(pattern, bound_node_labels)
        last_match_clause_index = clause_index
        if not variables:
            raise ValueError(
                "Repository scope could not be applied because the MATCH pattern has no named node; "
                "name a path-bearing node or set global_scope=true explicitly."
            )

        predicates = []
        for variable, pathless_module in variables:
            path_predicate = (
                f"({variable}.path = $_cgc_repo_root OR "
                f"{variable}.path STARTS WITH $_cgc_repo_prefix)"
            )
            if pathless_module:
                path_predicate = f"({variable}.path IS NULL OR {path_predicate})"
            predicates.append(path_predicate)
        predicate = " AND ".join(predicates)
        if where_index is not None:
            condition_start = clauses[where_index][2]
            while condition_start < len(masked) and masked[condition_start].isspace():
                condition_start += 1
            insertions.append((condition_start, "("))
            separator = ""
            if insertion_position < len(query) and not query[insertion_position].isspace():
                separator = " "
            closing_predicate = f") AND ({predicate}){separator}"
            if trailing_comment_at_eof and insertion_position == len(query):
                closing_predicate = f"\n{closing_predicate}"
            insertions.append((insertion_position, closing_predicate))
        else:
            predicate_clause = f" WHERE {predicate} "
            if trailing_comment_at_eof and insertion_position == len(query):
                predicate_clause = f"\n{predicate_clause}"
            insertions.append((insertion_position, predicate_clause))

    if _has_unscoped_graph_pattern(masked, scoped_ranges):
        raise ValueError(
            "Repository scope cannot be guaranteed for nested graph patterns; "
            "set global_scope=true explicitly."
        )

    scoped = query
    for position, insertion in sorted(insertions, reverse=True):
        scoped = scoped[:position] + insertion + scoped[position:]
    return scoped, {"_cgc_repo_root": root, "_cgc_repo_prefix": prefix}


def execute_cypher_query(db_manager, **args) -> Dict[str, Any]:
    """
    Tool implementation for executing a read-only Cypher query.

    Write protection is layered: the regex guard rejects write keywords on
    every backend, and the session is additionally opened in READ access mode
    so the database itself refuses writes. Neo4j honours ``default_access_mode``
    natively; FalkorDB routes READ sessions through ``GRAPH.RO_QUERY``.
    """
    cypher_query = args.get("cypher_query")
    graph_name = args.get("graph_name")
    if not cypher_query:
        return {"error": "Cypher query cannot be empty."}

    params = args.get("params") or args.get("parameters") or {}
    if not isinstance(params, dict):
        return {"error": "Query parameters must be an object/dictionary."}

    try:
        offset = _require_integer_argument(args.get("offset", 0), "offset")
        if offset < 0 or offset >= _EXECUTE_CYPHER_MAX_OFFSET:
            raise ValueError
        result_limit = get_tool_result_limit("execute_cypher_query", default=100) or 100
        requested_page_size = _require_integer_argument(
            args.get("page_size", result_limit), "page_size"
        )
        if requested_page_size < 1:
            raise ValueError
        page_size = min(
            requested_page_size,
            result_limit,
            _EXECUTE_CYPHER_MAX_OFFSET - offset,
        )
    except (TypeError, ValueError):
        return {
            "error": (
                "offset and page_size must be integers; offset must be between "
                "0 and 99999 and page_size must be positive."
            )
        }

    if not is_read_only_cypher(cypher_query):
        return {"error": read_only_rejection_message()}

    ordered_results = _has_stable_unique_final_order_by(cypher_query)
    repo_path = args.get("repo_path")
    global_scope = args.get("global_scope", False) is True
    executed_query = cypher_query
    query_params = dict(params)
    if repo_path and not global_scope:
        try:
            executed_query, scope_params = _scope_cypher_query(cypher_query, repo_path)
        except ValueError as exc:
            return {"error": str(exc)}
        query_params.update(scope_params)

    if offset and not ordered_results:
        return {"error": "Add a stable ORDER BY with a unique tie-breaker before requesting a nonzero offset."}

    backend = getattr(db_manager, "get_backend_type", lambda: "neo4j")()
    session_kwargs: Dict[str, Any] = {}
    # Backends that enforce read access at the session/protocol layer.
    # Neo4j honours default_access_mode natively; the FalkorDB wrapper routes
    # READ sessions through GRAPH.RO_QUERY (see FalkorDBSessionWrapper).
    if backend in ("neo4j", "falkordb", "falkordb-remote"):
        session_kwargs["default_access_mode"] = "READ"

    try:
        debug_log(f"Executing Cypher query: {cypher_query}")
        driver = CodeFinder(db_manager).get_read_driver(graph_name)
        with driver.session(**session_kwargs) as session:
            # Unpack as kwargs: Neo4j accepts them, and the FalkorDB/Kuzu
            # session shims only accept parameters via **kwargs.
            result = iter(session.run(executed_query, **query_params))
            for _ in range(offset):
                try:
                    next(result)
                except StopIteration:
                    break
            page = []
            for _ in range(page_size + 1):
                try:
                    page.append(next(result).data())
                except StopIteration:
                    break

            has_more = len(page) > page_size
            records = page[:page_size]
            page_end = offset + len(records)
            if has_more and not ordered_results:
                return {
                    "error": "Results span multiple pages; add a stable ORDER BY with a unique tie-breaker and retry."
                }

            response = {
                "success": True,
                "query": cypher_query,
                "record_count": len(records),
                "results": records,
                "offset": offset,
                "page_size": page_size,
                "has_more": has_more,
                "continuation_limit_reached": has_more and page_end >= _EXECUTE_CYPHER_MAX_OFFSET,
                "truncated": has_more,
            }
            if has_more and page_end < _EXECUTE_CYPHER_MAX_OFFSET:
                response["next_offset"] = page_end
            else:
                response["next_offset"] = None
            if has_more:
                response["result_limit"] = page_size
            if repo_path and not global_scope:
                response["repo_scope"] = str(Path(repo_path).expanduser().resolve())
            return response

    except CypherSyntaxError as e:
        debug_log(f"Cypher syntax error: {str(e)}")
        return {
            "error": "Cypher syntax error.",
            "details": str(e),
            "query": cypher_query,
        }
    except Exception as e:
        debug_log(f"Error executing Cypher query: {str(e)}")
        # FalkorDB/Kuzu raise their own exception types for malformed Cypher;
        # surface those as structured syntax errors like the Neo4j path does.
        message = str(e)
        if "syntax" in message.lower() or "parser" in message.lower():
            return {
                "error": "Cypher syntax error.",
                "details": message,
                "query": cypher_query,
            }
        return {
            "error": "An unexpected error occurred while executing the query.",
            "details": message,
        }


def visualize_graph_query(db_manager, **args) -> Dict[str, Any]:
    """Tool to generate a visualization URL for the local Playground UI."""
    cypher_query = args.get("cypher_query")
    if not cypher_query:
        return {"error": "Cypher query cannot be empty."}

    if not is_read_only_cypher(cypher_query):
        return {"error": read_only_rejection_message()}

    try:
        port = 8000
        params = {"cypher_query": cypher_query}
        repo_path = args.get("repo_path")
        if repo_path:
            params["repo_path"] = str(repo_path)
        if args.get("global_scope") is True:
            params["global_scope"] = "true"
        visualization_url = (
            f"http://localhost:{port}/index.html?{urllib.parse.urlencode(params)}"
        )

        return {
            "success": True,
            "visualization_url": visualization_url,
            "message": "Click the URL to visualize this specific query in the Playground UI. (Ensure 'cgc visualize' is running)",
        }
    except Exception as e:
        debug_log(f"Error generating visualization URL: {str(e)}")
        return {"error": f"Failed to generate visualization URL: {str(e)}"}
