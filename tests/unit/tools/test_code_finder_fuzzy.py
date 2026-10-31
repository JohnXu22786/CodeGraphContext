from codegraphcontext.tools.code_finder import (
    _levenshtein_distance,
    summarize_kotlin_call_ambiguity,
)


def test_levenshtein_single_typo():
    assert _levenshtein_distance("myfuncton", "myfunction") == 1


def test_levenshtein_identical():
    assert _levenshtein_distance("abc", "abc") == 0


def test_summarize_kotlin_call_ambiguity_groups_multi_target_calls():
    rows = [
        {
            "caller_name": "run",
            "caller_path": "/repo/A.kt",
            "caller_line": 10,
            "caller_end_line": 20,
            "call_line": 12,
            "full_call_name": "service.target",
            "target_name": "target",
            "target_path": "/repo/Service.kt",
            "target_line": 5,
            "target_context": "Service",
            "args": ["value"],
        },
        {
            "caller_name": "run",
            "caller_path": "/repo/A.kt",
            "caller_line": 10,
            "caller_end_line": 20,
            "call_line": 12,
            "full_call_name": "service.target",
            "target_name": "target",
            "target_path": "/repo/Service.kt",
            "target_line": 9,
            "target_context": "Service",
            "args": ["value"],
        },
        {
            "caller_name": "run",
            "caller_path": "/repo/A.kt",
            "caller_line": 10,
            "caller_end_line": 20,
            "call_line": 13,
            "full_call_name": "helper",
            "target_name": "helper",
            "target_path": "/repo/A.kt",
            "target_line": 30,
            "target_context": None,
            "args": [],
        },
    ]

    summary = summarize_kotlin_call_ambiguity(rows)

    assert summary["kotlin_fn_to_fn_edges"] == 3
    assert summary["ambiguous_groups"] == 1
    assert summary["ambiguous_edges"] == 2
    assert summary["top_names"] == [{"name": "target", "groups": 1}]
    assert summary["examples"][0]["target_count"] == 2


def test_summarize_kotlin_call_ambiguity_caps_targets_and_marks_context_truncation():
    rows = [
        {
            "caller_name": "run",
            "caller_path": "/repo/A.kt",
            "caller_line": 10,
            "caller_end_line": 20,
            "call_line": 12,
            "full_call_name": "service.target",
            "target_name": "target",
            "target_path": f"/repo/Service{i}.kt",
            "target_line": 5,
            "target_context": "x" * 128,
            "target_context_total_chars": 4096,
            "args": ["value"],
        }
        for i in range(5)
    ]

    summary = summarize_kotlin_call_ambiguity(rows, target_limit=2)

    example = summary["examples"][0]
    assert example["target_count"] == 5
    assert example["targets_truncated"] is True
    assert len(example["targets"]) == 2
    assert example["targets"][0]["context_total_chars"] == 4096
    assert example["targets"][0]["context_truncated"] is True


def test_summarize_kotlin_call_ambiguity_uses_uid_for_matching_context_previews():
    common = {
        "caller_name": "run",
        "caller_path": "/repo/A.kt",
        "caller_line": 10,
        "caller_end_line": 20,
        "call_line": 12,
        "full_call_name": "service.target",
        "target_name": "target",
        "target_path": "/repo/Service.kt",
        "target_line": 5,
        "target_context": "x" * 256,
        "target_context_total_chars": 512,
    }
    rows = [
        {**common, "_target_uid": "target-overload-a"},
        {**common, "_target_uid": "target-overload-b"},
    ]

    summary = summarize_kotlin_call_ambiguity(rows)

    assert summary["ambiguous_groups"] == 1
    assert summary["examples"][0]["target_count"] == 2
    assert summary["ambiguous_edges"] == 2


def test_summarize_kotlin_call_ambiguity_caps_total_target_previews_per_page():
    rows = []
    for group_index in range(4):
        for target_index in range(2):
            rows.append({
                "caller_name": "run",
                "caller_path": f"/repo/A{group_index}.kt",
                "caller_line": group_index + 1,
                "caller_end_line": 20,
                "call_line": group_index + 2,
                "full_call_name": "service.target",
                "target_name": "target",
                "target_path": f"/repo/Service{target_index}.kt",
                "target_line": 5,
                "target_context": "x" * 512,
                "target_context_total_chars": 512,
                "args": ["value"],
            })

    summary = summarize_kotlin_call_ambiguity(
        rows, limit=10, target_limit=5, total_target_limit=3
    )

    assert sum(len(group["targets"]) for group in summary["examples"]) == 3
    assert summary["examples"][0]["targets_truncated"] is False
    assert summary["examples"][1]["targets_truncated"] is True
    assert all(len(group["targets"][0]["context"]) <= 256 for group in summary["examples"] if group["targets"])


def test_summarize_kotlin_call_audit_recognizes_boundary_proven_ambiguity():
    row = {
        "caller_name": "run",
        "caller_path": "/repo/A.kt",
        "caller_line": 10,
        "caller_end_line": 20,
        "call_line": 12,
        "full_call_name": "service.target",
        "target_name": "target",
        "target_path": "/repo/ServiceA.kt",
        "target_line": 5,
        "target_context": "A",
        "_caller_uid": "caller-1",
        "_target_uid": "target-a",
    }
    next_row = {
        **row,
        "target_path": "/repo/ServiceB.kt",
        "_target_uid": "target-b",
    }

    summary = summarize_kotlin_call_ambiguity(
        [row], offset=0, page_size=1, has_more=True, next_row=next_row
    )

    assert summary["ambiguous_groups"] == 1
    assert summary["examples"][0]["target_count"] == 1
    assert summary["examples"][0]["partial"] is True
    assert summary["examples"][0]["target_count_complete"] is False

    duplicate_target_page = summarize_kotlin_call_ambiguity(
        [row],
        offset=0,
        page_size=1,
        has_more=True,
        next_row={**row, "_edge_type": "HEURISTIC_CALLS"},
    )
    assert duplicate_target_page["ambiguous_groups"] == 0


def test_summarize_kotlin_call_audit_allocates_previews_to_ranked_examples():
    rows = []
    for caller_path, target_count in (("/repo/A.kt", 2), ("/repo/B.kt", 3)):
        for target_index in range(target_count):
            rows.append({
                "caller_name": "run",
                "caller_path": caller_path,
                "caller_line": 10,
                "caller_end_line": 20,
                "call_line": 12,
                "full_call_name": "service.target",
                "target_name": "target",
                "target_path": f"/repo/Target{target_index}.kt",
                "target_line": target_index + 1,
                "target_context": "context",
            })

    summary = summarize_kotlin_call_ambiguity(
        rows, limit=1, target_limit=5, total_target_limit=1
    )

    assert summary["examples"][0]["caller_path"] == "/repo/B.kt"
    assert len(summary["examples"][0]["targets"]) == 1
