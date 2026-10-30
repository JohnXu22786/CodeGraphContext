# Confidence and evidence

CodeGraphContext reports confidence and evidence for two different questions:

| Field | Meaning | Compatibility |
| --- | --- | --- |
| Relationship `confidence_label` | Provenance of a graph relationship. `EXTRACTED` means the parser/index directly reported it; `INFERRED` means resolution or graph analysis derived it. | Existing graph relationships keep their labels. Older stored relationships with no label retain their current writer defaults (`EXTRACTED` for parser-backed relationships and `INFERRED` for inferred relationships). |
| Call relationship `confidence` | Numeric call-resolution score, used with `resolution_tier` and `resolution_method` to describe how a call target was selected. | Existing numeric values and tier ordering are unchanged. A missing value in older data remains unknown; it is not treated as certainty. |
| Direct caller/callee `resolution_status` | `RESOLVED`, `UNRESOLVED`, `AMBIGUOUS`, or `UNKNOWN`, derived from the existing relationship type and `confidence_label`. The original confidence, tier, method, and label are returned alongside it. | Additive fields. Older `HEURISTIC_CALLS` without a label still report `UNRESOLVED`; older `CALLS` without a label report `UNKNOWN`. Missing numeric resolution details stay null. |
| Dead-code row `confidence` | `high`, `medium`, or `low` ranking of a potentially unused function based on known exclusion signals such as decorators, entry-point names, and test locations. It is not a probability. | Existing values and default filtering are unchanged; `--show-all` includes low-confidence rows. |
| Dead-code row `confidence_label` | Provenance of the dead-code conclusion. It is always `INFERRED`: the graph can show no known inbound calls, but cannot prove a function is unreachable. | Additive response field. Existing consumers can continue using `confidence` and `confidence_reason`. |
| Impact-style response `analysis_basis` | `INFERRED_FROM_INDEXED_GRAPH` on dead-code, transitive caller/callee, and call-chain responses. The result reflects the indexed graph and its coverage. | Additive response field; existing `query_type`, `results`, paging, and confidence fields are unchanged. |

## Evidence locations and ambiguity

Search and dead-code results preserve the established `path` and `line_number` fields. Search results expose an inclusive `end_line` for declarations that have one and a repository-relative path when the query is scoped to a repository. Dead-code rows expose a repository-relative path and inclusive `end_line` when the graph has those values. Missing legacy end lines remain absent/null. MCP and HTTP handlers return these result objects; the CLI dead-code view prefers the relative path and displays the range when available.

Parser-backed search results carry `confidence_label: EXTRACTED` and `evidence_kind: PARSER_INDEXED_SOURCE`; a resolved call or relationship may be inferred. Call audit results report ambiguity as `resolution_status: AMBIGUOUS` with multiple candidate targets and bounded target locations; unresolved calls remain represented by the existing `HEURISTIC_CALLS` relationship and its confidence/resolution fields when returned. A missing field in an older database means unknown or unsupported, not a successful resolution.

Dead-code and impact-style conclusions describe what the indexed graph can currently establish. An absent edge can reflect dynamic dispatch, callbacks, external entry points, or incomplete indexing. Treat these results as investigation leads and inspect source evidence before acting on them.
