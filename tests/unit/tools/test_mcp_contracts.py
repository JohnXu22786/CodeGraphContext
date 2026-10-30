"""Regression tests for MCP tool contracts."""
import inspect

import pytest

from codegraphcontext import prompts
from codegraphcontext.server import MCPServer
from codegraphcontext.tool_definitions import TOOLS


def test_list_jobs_tool_tolerates_extra_arguments():
    """Dispatch is `handler(**args)` and the schema declares no
    additionalProperties:false, so a client may legitimately send extras.
    Without **args that raised TypeError, surfacing as -32603 Internal error
    and looking indistinguishable from a server crash."""
    sig = inspect.signature(MCPServer.list_jobs_tool)
    assert any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    ), "list_jobs_tool must accept **args like every sibling handler"


@pytest.mark.parametrize("tool_name", ["delete_repository", "load_bundle"])
def test_destructive_tools_warn_in_their_description(tool_name):
    """Descriptions were 'Delete a repository from the graph.' and 'Load a
    pre-indexed bundle.' — nothing told the model they are irreversible."""
    description = TOOLS[tool_name]["description"].lower()
    assert "irreversible" in description
    assert "destructive" in description


def test_clear_existing_documents_that_it_drops_existing_data():
    """`clear_existing` had no description at all, so to an LLM it read like
    'clear the existing copy of this bundle'."""
    prop = TOOLS["load_bundle"]["inputSchema"]["properties"]["clear_existing"]
    description = prop.get("description", "").lower()
    assert description, "clear_existing must document what it clears"
    assert "irreversible" in description or "destructive" in description
    assert "deleted" in description or "discards" in description


def test_spring_tools_declare_their_row_cap():
    from codegraphcontext.tools.handlers import analysis_handlers

    tools = (
        ("find_java_spring_endpoints", "endpoints"),
        ("find_java_spring_beans", "beans"),
    )

    class _Result:
        def __init__(self, rows):
            self.rows = rows

        def __iter__(self):
            return iter(self.rows)

    class _Session:
        def __init__(self, rows):
            self.rows = rows

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            assert params["spring_candidate_limit"] == 101
            return _Result(self.rows)

    class _Finder:
        def __init__(self, rows):
            self.driver = type("_Driver", (), {"session": lambda _: _Session(rows)})()

        @staticmethod
        def _normalize_repo_path_prefix(path):
            return path.rstrip("/\\") + "/"

    for tool_name, collection in tools:
        assert TOOLS[tool_name]["inputSchema"]["properties"]["page_size"]["maximum"] == 100

        handler = getattr(analysis_handlers, tool_name)
        for row_count, expected_count, expected_truncated, expected_next_offset in (
            (99, 99, False, None),
            (100, 100, False, None),
            (101, 100, True, 100),
        ):
            rows = [{"name": f"row-{index}"} for index in range(row_count)]
            response = handler(_Finder(rows))

            assert response["success"] is True
            assert len(response[collection]) == expected_count
            assert response["count"] == expected_count
            assert response["page_size"] == 100
            assert response["result_limit"] == 100
            assert response["truncated"] is expected_truncated
            assert response["next_offset"] == expected_next_offset


def test_cypher_fallback_instruction_is_not_self_contradictory():
    """The guidance read 'use the correct property names (e.g. `path` vs
    `path`)' — a corrupted instruction that teaches the model nothing."""
    assert "`path` vs `path`" not in prompts.LLM_SYSTEM_PROMPT
    # ...and it still tells the model to consult the schema.
    assert "Graph Schema Reference" in prompts.LLM_SYSTEM_PROMPT


def test_every_tool_definition_has_a_matching_wrapper():
    """Every public tool definition should have a corresponding wrapper
    method on MCPServer."""
    for tool_name in TOOLS:
        assert hasattr(
            MCPServer, f"{tool_name}_tool"
        ), f"Missing wrapper method: {tool_name}_tool"


def test_every_wrapper_has_a_tool_definition():
    """Every public *_tool wrapper should correspond to a declared MCP tool."""
    excluded = {
        "_init_tools",
        "_load_disabled_tools",
        "_normalize_tool_name",
        "handle_tool_call",
    }

    wrapper_methods = {
        name[:-5]
        for name, member in inspect.getmembers(MCPServer, inspect.isfunction)
        if name.endswith("_tool") and name not in excluded
    }

    missing = wrapper_methods - set(TOOLS.keys())
    assert not missing, (
        "Wrapper methods without matching tool definitions: "
        f"{sorted(missing)}"
    )


def test_tool_definition_keys_match_declared_names():
    """The dictionary key should always match the tool's declared name."""
    for key, definition in TOOLS.items():
        assert definition["name"] == key



