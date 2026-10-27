"""Unit tests for relationship query tool limits and truncation flags (Issue #1542)."""
import json
from pathlib import Path
import pytest

from codegraphcontext.core.database_kuzu import KuzuDBManager
from codegraphcontext.tools.code_finder import CodeFinder
from codegraphcontext.tools.handlers import analysis_handlers

kuzu = pytest.importorskip("kuzu")


class _KuzuDBAdapter:
    def __init__(self, driver):
        self._driver = driver

    def get_driver(self, graph_name=None):
        return self._driver

    def get_backend_type(self) -> str:
        return "kuzudb"


def test_find_callers_truncation(tmp_path, monkeypatch):
    manager = KuzuDBManager(str(tmp_path / "db"))
    driver = manager.get_driver()
    try:
        with driver.session() as session:
            session.run(
                "CREATE (:Function {uid: 'target:lib.py:1', name: 'target_fn', path: 'lib.py', "
                "line_number: 1, is_dependency: false})"
            )
            for i in range(25):
                caller_name = f"caller_{i}"
                session.run(
                    "CREATE (:Function {uid: $uid, name: $name, path: 'app.py', "
                    "line_number: $line, is_dependency: false})",
                    uid=f"{caller_name}:app.py:{i+10}",
                    name=caller_name,
                    line=i + 10,
                )
                session.run(
                    "MATCH (a:Function {name: $caller}), (b:Function {name: 'target_fn'}) "
                    "CREATE (a)-[:CALLS {line_number: $line}]->(b)",
                    caller=caller_name,
                    line=i + 10,
                )

        finder = CodeFinder(_KuzuDBAdapter(driver))

        # Default limit 20
        res = finder.analyze_code_relationships("find_callers", "target_fn")
        assert len(res["results"]) == 20
        assert res["truncated"] is True
        assert res["result_limit"] == 20
        assert "(truncated — more exist)" in res["summary"]

        # Handler forwarding
        handler_res = analysis_handlers.analyze_code_relationships(finder, query_type="find_callers", target="target_fn")
        assert handler_res["truncated"] is True
        assert handler_res["result_limit"] == 20

        # Custom config limit 50 via TOOL_RESULT_LIMITS
        monkeypatch.setenv("TOOL_RESULT_LIMITS", json.dumps({"find_callers": 50}))
        res_custom = finder.analyze_code_relationships("find_callers", "target_fn")
        assert len(res_custom["results"]) == 25
        assert res_custom["truncated"] is False
        assert res_custom["result_limit"] == 50
        assert "(truncated" not in res_custom["summary"]
    finally:
        manager.close_driver()


def test_find_callees_truncation(tmp_path):
    manager = KuzuDBManager(str(tmp_path / "db"))
    driver = manager.get_driver()
    try:
        with driver.session() as session:
            session.run(
                "CREATE (:Function {uid: 'caller:app.py:1', name: 'caller_fn', path: 'app.py', "
                "line_number: 1, is_dependency: false})"
            )
            for i in range(30):
                callee_name = f"callee_{i}"
                session.run(
                    "CREATE (:Function {uid: $uid, name: $name, path: 'lib.py', "
                    "line_number: $line, is_dependency: false})",
                    uid=f"{callee_name}:lib.py:{i+10}",
                    name=callee_name,
                    line=i + 10,
                )
                session.run(
                    "MATCH (a:Function {name: 'caller_fn'}), (b:Function {name: $callee}) "
                    "CREATE (a)-[:CALLS {line_number: $line}]->(b)",
                    callee=callee_name,
                    line=i + 10,
                )

        finder = CodeFinder(_KuzuDBAdapter(driver))
        res = finder.analyze_code_relationships("find_callees", "caller_fn")
        assert len(res["results"]) == 20
        assert res["truncated"] is True
        assert res["result_limit"] == 20
        assert "(truncated — more exist)" in res["summary"]
    finally:
        manager.close_driver()


def test_find_importers_truncation(tmp_path):
    manager = KuzuDBManager(str(tmp_path / "db"))
    driver = manager.get_driver()
    try:
        with driver.session() as session:
            session.run("CREATE (:Module {name: 'mod_a', full_import_name: 'mod_a'})")
            for i in range(25):
                file_path = f"src/file_{i}.py"
                session.run(
                    "CREATE (:File {name: $name, path: $path, relative_path: $path, is_dependency: false})",
                    name=f"file_{i}.py",
                    path=file_path,
                )
                session.run(
                    "MATCH (f:File {path: $path}), (m:Module {name: 'mod_a'}) "
                    "CREATE (f)-[:IMPORTS {alias: NULL}]->(m)",
                    path=file_path,
                )

        finder = CodeFinder(_KuzuDBAdapter(driver))
        res = finder.analyze_code_relationships("find_importers", "mod_a")
        assert len(res["results"]) == 20
        assert res["truncated"] is True
        assert res["result_limit"] == 20
        assert "(truncated — more exist)" in res["summary"]
    finally:
        manager.close_driver()


def test_variable_queries_support_file_container_without_file_uid(tmp_path):
    manager = KuzuDBManager(str(tmp_path / "db"))
    driver = manager.get_driver()
    try:
        with driver.session() as session:
            session.run(
                "CREATE (:File {name: 'app.py', path: 'src/app.py', "
                "relative_path: 'src/app.py', is_dependency: false})"
            )
            session.run(
                "CREATE (:Variable {uid: 'value:src/app.py:1', name: 'value', "
                "path: 'src/app.py', line_number: 1, is_dependency: false, "
                "value: '1', context: 'module'})"
            )
            session.run(
                "MATCH (file:File {path: 'src/app.py'}), "
                "(variable:Variable {name: 'value'}) "
                "CREATE (file)-[:CONTAINS]->(variable)"
            )

        finder = CodeFinder(_KuzuDBAdapter(driver))
        modifiers = finder.who_modifies_variable("value")
        scope = finder.find_variable_usage_scope("value")

        assert len(modifiers) == 1
        assert modifiers[0]["container_type"] == "file"
        assert modifiers[0]["path"] == "src/app.py"
        assert scope["instances"]
        assert scope["instances"][0]["scope_type"] == "module"
    finally:
        manager.close_driver()


def test_kuzu_relationship_evidence_pages_preserve_unicode_offsets(tmp_path):
    manager = KuzuDBManager(str(tmp_path / "db"))
    driver = manager.get_driver()
    try:
        docstring = "被调用函数说明😀" * 20
        value = "变量值🧪" * 20
        context = "配置上下文🚀" * 20
        with driver.session() as session:
            session.run(
                "CREATE (:Function {uid: 'caller', name: 'caller', path: '/repo/a.py', "
                "line_number: 1, is_dependency: false})"
            )
            session.run(
                "CREATE (:Function {uid: 'callee', name: 'callee', path: '/repo/b.py', "
                "line_number: 2, is_dependency: false, docstring: $docstring})",
                docstring=docstring,
            )
            session.run(
                "MATCH (caller:Function {uid: 'caller'}), "
                "(callee:Function {uid: 'callee'}) "
                "CREATE (caller)-[:CALLS]->(callee)"
            )
            session.run(
                "CREATE (:Variable {uid: 'variable', name: 'setting', "
                "path: '/repo/settings.py', line_number: 4, is_dependency: false, "
                "value: $value, context: $context})",
                value=value,
                context=context,
            )

        finder = CodeFinder(_KuzuDBAdapter(driver))
        callee_response = analysis_handlers.analyze_code_relationships(
            finder,
            query_type="find_callees",
            target="caller",
            page_size=1,
            docstring_offset=3,
            docstring_char_limit=7,
        )
        scope_response = analysis_handlers.analyze_code_relationships(
            finder,
            query_type="variable_scope",
            target="setting",
            page_size=1,
            value_offset=5,
            context_offset=2,
            evidence_char_limit=8,
        )

        callees = callee_response["results"]["results"]
        assert callees[0]["called_docstring"] == docstring[3:10]
        assert callees[0]["called_docstring_offset"] == 3
        assert callees[0]["called_docstring_next_offset"] == 10
        instances = scope_response["results"]["results"]["instances"]
        instance = instances[0]
        assert instance["variable_value"] == value[5:13]
        assert instance["variable_value_next_offset"] == 13
        assert instance["context"] == context[2:10]
        assert instance["context_next_offset"] == 10
    finally:
        manager.close_driver()


def test_kuzu_kotlin_call_audit_uses_stable_supported_relationship_ordering(tmp_path):
    manager = KuzuDBManager(str(tmp_path / "kotlin-call-audit-db"))
    driver = manager.get_driver()
    try:
        with driver.session() as session:
            session.run(
                "CREATE (:Function {uid: 'audit-caller', name: 'run', path: '/repo/A.kt', "
                "line_number: 10, end_line: 20, context: 'caller context'})"
            )
            for index in range(4):
                session.run(
                    "CREATE (:Function {uid: $uid, name: 'target', path: $path, "
                    "line_number: 5, context: $context})",
                    uid=f"audit-target-{index}",
                    path=f"/repo/Target{index}.kt",
                    context="目标上下文😀" * 120,
                )
            for index in range(4):
                session.run(
                    "MATCH (caller:Function {uid: 'audit-caller'}), "
                    "(target:Function {uid: $uid}) "
                    "CREATE (caller)-[:CALLS {line_number: 12, "
                    "full_call_name: 'service.target', args: ['value'], "
                    "args_key: 'value'}]->(target)",
                    uid=f"audit-target-{index}",
                )

        finder = CodeFinder(_KuzuDBAdapter(driver))
        first_page = finder.audit_kotlin_call_ambiguity(
            repo_path="/repo", page_size=2
        )
        second_page = finder.audit_kotlin_call_ambiguity(
            repo_path="/repo", offset=first_page["next_offset"], page_size=2
        )

        assert first_page["next_offset"] == 2
        assert second_page["next_offset"] is None
        first_targets = first_page["examples"][0]["targets"]
        second_targets = second_page["examples"][0]["targets"]
        assert {target["path"] for target in first_targets}.isdisjoint(
            target["path"] for target in second_targets
        )
        assert len(first_targets[0]["context"]) == 256
        assert first_targets[0]["context_total_chars"] == len("目标上下文😀" * 120)
        assert first_targets[0]["context_truncated"] is True
        assert first_page["examples"][0]["partial"] is True
        assert second_page["examples"][0]["partial"] is True
    finally:
        manager.close_driver()


def test_override_query_does_not_require_file_uid(tmp_path):
    manager = KuzuDBManager(str(tmp_path / "db"))
    driver = manager.get_driver()
    args = [f"parameter_{index}_" + "x" * 512 for index in range(20)]
    try:
        with driver.session() as session:
            session.run(
                "CREATE (:File {name: 'app.py', path: 'src/app.py', "
                "relative_path: 'src/app.py', is_dependency: false})"
            )
            session.run(
                "CREATE (:Class {uid: 'widget:src/app.py:1', name: 'Widget', "
                "path: 'src/app.py', line_number: 1, is_dependency: false})"
            )
            session.run(
                "CREATE (:Function {uid: 'render:src/app.py:2', name: 'render', "
                "path: 'src/app.py', line_number: 2, is_dependency: false, args: $args})",
                args=args,
            )
            session.run(
                "MATCH (file:File {path: 'src/app.py'}), "
                "(class:Class {name: 'Widget'}), (method:Function {name: 'render'}) "
                "CREATE (file)-[:CONTAINS]->(class), (class)-[:CONTAINS]->(method)"
            )

        finder = CodeFinder(_KuzuDBAdapter(driver))
        overrides = finder.find_function_overrides("render")
        hierarchy = finder.find_class_hierarchy("Widget", page_size=2)

        assert len(overrides) == 1
        assert overrides[0]["class_name"] == "Widget"
        for row, field in (
            (overrides[0], "function_args"),
            (hierarchy["methods"][0], "method_args"),
        ):
            assert len(row[field]) == 16
            assert max(map(len, row[field])) == 128
            assert row[f"{field}_total_count"] == 20
            assert row[f"{field}_truncated"] is True
    finally:
        manager.close_driver()


def test_regression_untruncated_results(tmp_path):
    manager = KuzuDBManager(str(tmp_path / "db"))
    driver = manager.get_driver()
    try:
        with driver.session() as session:
            session.run(
                "CREATE (:Function {uid: 'target:lib.py:1', name: 'target_fn', path: 'lib.py', "
                "line_number: 1, is_dependency: false})"
            )
            for i in range(5):
                caller_name = f"caller_{i}"
                session.run(
                    "CREATE (:Function {uid: $uid, name: $name, path: 'app.py', "
                    "line_number: $line, is_dependency: false})",
                    uid=f"{caller_name}:app.py:{i+10}",
                    name=caller_name,
                    line=i + 10,
                )
                session.run(
                    "MATCH (a:Function {name: $caller}), (b:Function {name: 'target_fn'}) "
                    "CREATE (a)-[:CALLS {line_number: $line}]->(b)",
                    caller=caller_name,
                    line=i + 10,
                )

        finder = CodeFinder(_KuzuDBAdapter(driver))
        res = finder.analyze_code_relationships("find_callers", "target_fn")
        assert len(res["results"]) == 5
        assert res["truncated"] is False
        assert res["result_limit"] == 20
        assert "(truncated" not in res["summary"]
    finally:
        manager.close_driver()
