"""MCP tool schemas must expose every handler-supported public input."""

from codegraphcontext.tool_definitions import TOOLS


def test_schemas_expose_existing_handler_inputs():
    analyze_properties = TOOLS["analyze_code_relationships"]["inputSchema"]["properties"]
    cypher_properties = TOOLS["execute_cypher_query"]["inputSchema"]["properties"]
    complexity_properties = TOOLS["calculate_cyclomatic_complexity"]["inputSchema"]["properties"]
    datasource_properties = TOOLS["find_datasource_nodes"]["inputSchema"]["properties"]
    visualization_properties = TOOLS["visualize_graph_query"]["inputSchema"]["properties"]

    assert analyze_properties["depth"]["type"] == "integer"
    assert analyze_properties["docstring_offset"]["minimum"] == 0
    assert analyze_properties["docstring_char_limit"]["maximum"] <= 1_536
    assert analyze_properties["value_offset"]["minimum"] == 0
    assert analyze_properties["context_offset"]["minimum"] == 0
    assert analyze_properties["evidence_char_limit"]["maximum"] <= 1_536
    assert cypher_properties["params"] == {
        "type": "object",
        "description": "Optional named parameters passed to the Cypher query.",
        "default": {},
    }
    assert complexity_properties["path"]["type"] == "string"
    assert datasource_properties["repo_path"]["type"] == "string"
    assert datasource_properties["global_scope"]["type"] == "boolean"
    assert visualization_properties["repo_path"]["type"] == "string"
    assert visualization_properties["global_scope"]["type"] == "boolean"
