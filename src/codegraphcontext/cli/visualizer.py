# src/codegraphcontext/cli/visualizer.py
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .cli_helpers import visualize_helper


def resolve_visual_flag(ctx: Any, visual: bool) -> bool:
    """True when local or global --visual / -V was requested."""
    if visual:
        return True
    obj = getattr(ctx, "obj", None) if ctx is not None else None
    return bool(isinstance(obj, dict) and obj.get("visual"))


def check_visual_flag(ctx: Any, visual: bool) -> bool:
    """Return whether visualization was requested (does not launch UI)."""
    return resolve_visual_flag(ctx, visual)


ALLOWED_GRAPH_LABELS = frozenset({
    "Class", "DbTable", "Directory", "Enum", "EnumMember", "ExternalClass",
    "ExternalFunction", "Extension", "File", "Function", "Interface", "Macro",
    "Mixin", "Module", "Object", "Parameter", "Repository", "Struct", "Trait",
    "Union", "Variable",
})
_SCOPED_SEARCH_LABELS = frozenset({
    "Class", "Enum", "File", "Function", "Interface", "Struct", "Trait", "Variable",
})


def _escape_cypher_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _validate_graph_label(label: str) -> str:
    normalized = label.strip()
    if not normalized or not normalized.replace("_", "").isalnum():
        raise ValueError(f"Invalid node label: {label!r}")
    for allowed in ALLOWED_GRAPH_LABELS:
        if allowed.lower() == normalized.lower():
            return allowed
    raise ValueError(
        f"Unsupported node label {label!r}. "
        f"Allowed labels: {', '.join(sorted(ALLOWED_GRAPH_LABELS))}"
    )


def _file_props(name: str, file: Optional[str]) -> str:
    fn = _escape_cypher_string(name)
    if file:
        path = _escape_cypher_string(str(Path(file).resolve()))
        return f"{{name: '{fn}', path: '{path}'}}"
    return f"{{name: '{fn}'}}"


def _search_visualization_query(
    results: Any, name: str, search_type: str
) -> str:
    term = _escape_cypher_string(name)
    modules = False
    imports = False
    if search_type == "type":
        requested_label = _validate_graph_label(name)
        if requested_label == "Module":
            labels = set()
            modules = True
        elif requested_label in _SCOPED_SEARCH_LABELS:
            labels = {requested_label}
        else:
            raise ValueError(
                f"Repository-scoped visualization does not support {requested_label} nodes."
            )
    else:
        labels = set()
        for result in results or []:
            if not isinstance(result, dict):
                continue
            result_type = str(result.get("type") or "")
            if result_type.lower() == "import":
                imports = True
                continue
            try:
                label = _validate_graph_label(result_type)
            except ValueError:
                continue
            if label in _SCOPED_SEARCH_LABELS:
                labels.add(label)
            elif label == "Module":
                modules = True

    predicates = {
        "pattern": f"n.name CONTAINS '{term}'",
        "name": f"n.name = '{term}'",
    }
    predicate = predicates.get(search_type, predicates["name"])
    branches = []
    for label in sorted(labels):
        branch = f"MATCH (n:{label}) "
        if search_type != "type":
            branch += f"WHERE {predicate} "
        branches.append(branch + "RETURN n AS n, null AS rel, null AS m")
    if modules:
        if search_type == "type":
            module_predicate = ""
        elif search_type == "pattern":
            module_predicate = f"m.name CONTAINS '{term}'"
        else:
            module_predicate = f"m.name = '{term}'"
        branches.append(
            "MATCH (f:File)-[rel:IMPORTS]->(m:Module) "
            + (f"WHERE {module_predicate} " if module_predicate else "")
            + "RETURN f AS n, rel AS rel, m AS m"
        )
    if imports:
        branches.append(
            "MATCH (f:File)-[rel:IMPORTS]->(m:Module) "
            f"WHERE rel.alias = '{term}' OR rel.imported_name = '{term}' "
            "RETURN f AS n, rel AS rel, m AS m"
        )

    if not branches and results:
        raise ValueError(
            "Repository-scoped visualization cannot represent the returned node types."
        )
    if not branches:
        # Preserve the helper's direct-call behavior when the CLI did not
        # provide result metadata, while keeping the generated query scoped.
        branches.append(
            f"MATCH (n:Function) WHERE {predicate} "
            "RETURN n AS n, null AS rel, null AS m"
        )
    return " UNION ".join(branches) + " LIMIT 80"


def _launch_visualizer(
    cypher_query: str,
    *,
    repo_path: Optional[str] = None,
    context: Optional[str] = None,
    global_scope: bool = False,
    offset: int = 0,
    page_size: int = 100,
) -> None:
    visualize_helper(
        repo_path=repo_path,
        port=8000,
        context=context,
        cypher_query=cypher_query,
        global_scope=global_scope,
        offset=offset,
        page_size=page_size,
    )


def visualize_search_results(
    _results: Any,
    name: str,
    search_type: str = "name",
    *,
    repo_path: Optional[str] = None,
    context: Optional[str] = None,
    offset: int = 0,
    page_size: int = 100,
    **_: Any,
) -> None:
    query = _search_visualization_query(_results, name, search_type)
    _launch_visualizer(
        query,
        repo_path=repo_path,
        context=context,
        offset=offset,
        page_size=page_size,
    )


def visualize_call_graph(
    _results: Any,
    function: str,
    direction: str = "outgoing",
    file: Optional[str] = None,
    repo_path: Optional[str] = None,
    context: Optional[str] = None,
    offset: int = 0,
    page_size: int = 100,
    **_: Any,
) -> None:
    props = _file_props(function, file)
    if direction == "incoming":
        query = (
            f"MATCH (caller:Function)-[rel:CALLS]->(target:Function {props}) "
            f"RETURN caller AS n, rel AS rel, target AS m LIMIT 120"
        )
    else:
        query = (
            f"MATCH (caller:Function {props})-[rel:CALLS]->(called:Function) "
            "RETURN caller AS n, rel AS rel, called AS m "
            "UNION "
            f"MATCH (caller:Function {props})-[rel:CALLS]->(called:Class) "
            "RETURN caller AS n, rel AS rel, called AS m LIMIT 120"
        )
    _launch_visualizer(
        query, repo_path=repo_path, context=context, offset=offset,
        page_size=page_size,
    )


def visualize_call_chain(
    _results: Any,
    from_func: str,
    to_func: str,
    max_depth: int = 5,
    from_file: Optional[str] = None,
    to_file: Optional[str] = None,
    repo_path: Optional[str] = None,
    context: Optional[str] = None,
    offset: int = 0,
    page_size: int = 100,
    **_: Any,
) -> None:
    edge_conditions = []
    for chain in _results or []:
        if not isinstance(chain, dict):
            continue
        functions = chain.get("function_chain") or []
        call_details = chain.get("call_details") or []
        for index, (start, end) in enumerate(zip(functions, functions[1:])):
            if not isinstance(start, dict) or not isinstance(end, dict):
                continue
            start_uid = start.get("uid")
            end_uid = end.get("uid")
            if start_uid and end_uid:
                condition = (
                    f"(start.uid = '{_escape_cypher_string(str(start_uid))}' "
                    f"AND end.uid = '{_escape_cypher_string(str(end_uid))}'"
                )
            else:
                start_path = start.get("path")
                end_path = end.get("path")
                if (
                    not start_path
                    or not end_path
                    or start.get("path_truncated")
                    or end.get("path_truncated")
                ):
                    continue
                start_name = _escape_cypher_string(str(start.get("name") or ""))
                end_name = _escape_cypher_string(str(end.get("name") or ""))
                start_path = _escape_cypher_string(str(start_path))
                end_path = _escape_cypher_string(str(end_path))
                condition = (
                    f"(start.name = '{start_name}' AND start.path = '{start_path}' "
                    f"AND end.name = '{end_name}' AND end.path = '{end_path}'"
                )
            detail = call_details[index] if index < len(call_details) else None
            call_line = detail.get("call_line") if isinstance(detail, dict) else None
            if isinstance(call_line, int):
                condition += f" AND rel.line_number = {call_line}"
            edge_conditions.append(condition + ")")

    where_clause = " OR ".join(edge_conditions) if edge_conditions else "false"
    query = (
        "MATCH (start:Function)-[rel:CALLS|HEURISTIC_CALLS]->(end:Function) "
        f"WHERE {where_clause} "
        "RETURN DISTINCT start AS n, rel AS rel, end AS m LIMIT 200"
    )
    _launch_visualizer(
        query, repo_path=repo_path, context=context, offset=offset,
        page_size=page_size,
    )


def visualize_dependencies(
    _results: Any,
    target: str,
    repo_path: Optional[str] = None,
    context: Optional[str] = None,
    offset: int = 0,
    page_size: int = 100,
    **_: Any,
) -> None:
    term = _escape_cypher_string(target)
    query = (
        f"MATCH (f:File)-[rel:IMPORTS]->(m:Module) "
        f"WHERE m.name CONTAINS '{term}' OR m.full_import_name CONTAINS '{term}' "
        f"RETURN f AS n, rel AS rel, m AS m LIMIT 120"
    )
    _launch_visualizer(
        query,
        repo_path=repo_path,
        context=context,
        offset=offset,
        page_size=page_size,
    )


def visualize_inheritance_tree(
    _results: Any,
    class_name: str,
    file: Optional[str] = None,
    repo_path: Optional[str] = None,
    context: Optional[str] = None,
    offset: int = 0,
    page_size: int = 100,
    **_: Any,
) -> None:
    props = _file_props(class_name, file)
    query = (
        f"MATCH (c:Class {props})-[rel:INHERITS]->(parent:Class) "
        f"RETURN c AS n, rel AS rel, parent AS m "
        f"UNION "
        f"MATCH (child:Class)-[rel:INHERITS]->(c:Class {props}) "
        f"RETURN child AS n, rel AS rel, c AS m "
        f"LIMIT 120"
    )
    _launch_visualizer(
        query,
        repo_path=repo_path,
        context=context,
        offset=offset,
        page_size=page_size,
    )


def visualize_overrides(
    _results: Any,
    function_name: str,
    repo_path: Optional[str] = None,
    context: Optional[str] = None,
    offset: int = 0,
    page_size: int = 100,
    **_: Any,
) -> None:
    fn = _escape_cypher_string(function_name)
    query = (
        f"MATCH (class:Class)-[rel:CONTAINS]->(func:Function {{name: '{fn}'}}) "
        f"RETURN class AS n, rel AS rel, func AS m LIMIT 120"
    )
    _launch_visualizer(
        query, repo_path=repo_path, context=context, offset=offset,
        page_size=page_size,
    )


def visualize_cypher_results(
    cypher_query: str,
    repo_path: Optional[str] = None,
    context: Optional[str] = None,
    global_scope: bool = False,
    offset: int = 0,
    page_size: int = 100,
    **_: Any,
) -> None:
    _launch_visualizer(
        cypher_query,
        repo_path=repo_path,
        context=context,
        global_scope=global_scope,
        offset=offset,
        page_size=page_size,
    )
