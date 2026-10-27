# src/codegraphcontext/tool_definitions.py

# Common optional property for multi-graph support
_GRAPH_NAME_PROP = {
    "type": "string",
    "description": "Optional: Name of the FalkorDB graph to query. Each indexed repository can have its own graph. Use 'list_graphs' to see available graphs. Defaults to the server's configured graph name."
}

TOOLS = {
    "add_code_to_graph": {
        "name": "add_code_to_graph",
        "description": "Performs a one-time scan of a local folder to add its code to the graph. Ideal for indexing libraries, dependencies, or projects not being actively modified. Returns a job ID for background processing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the repository root"
                },
                "is_dependency": {
                    "type": "boolean",
                    "description": "Whether this code is a dependency.",
                    "default": False
                },
                "timeout_seconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "Optional indexing deadline in seconds. A deadline stops the job at the next safe database boundary."
                },
                "graph_name": _GRAPH_NAME_PROP
            },
            "required": ["repo_path"]
        }
    },

    "check_job_status": {
        "name": "check_job_status",
        "description": "Check the status and progress of a background job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job ID from a previous tool call"
                }
            },
            "required": ["job_id"]
        }
    },

    "cancel_indexing_job": {
        "name": "cancel_indexing_job",
        "description": (
            "Cancel an indexing job and wait until it has stopped and its partial graph data is removed. "
            "A database operation already in progress must return before cancellation is complete."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job ID returned by add_code_to_graph or add_package_to_graph"
                }
            },
            "required": ["job_id"]
        }
    },

    "list_jobs": {
        "name": "list_jobs",
        "description": "List all background jobs and their current status.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },

    "find_code": {
        "name": "find_code",
        "description": "Find concise evidence in the active repository (or repo_path). Results are paged; request include_source/include_docstring to retrieve bounded source pages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword or phrase to search for"
                },
                "fuzzy_search": {
                    "type": "boolean",
                    "description": "Whether to use fuzzy search",
                    "default": False
                },
                "edit_distance": {
                    "type": "number",
                    "description": "Edit distance for fuzzy search (between 0-2)",
                    "default": 2
                },
                "repo_path": {
                    "type": "string",
                    "description": "Optional repository root; defaults to the active repository."
                },
                "offset": {"type": "integer", "minimum": 0, "maximum": 99999, "default": 0},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 3},
                "include_source": {
                    "type": "boolean",
                    "description": "Return a bounded source page for each ranked result.",
                    "default": False
                },
                "include_docstring": {
                    "type": "boolean",
                    "description": "Return a bounded docstring page for each ranked result.",
                    "default": False
                },
                "source_offset": {"type": "integer", "minimum": 0, "default": 0},
                "docstring_offset": {"type": "integer", "minimum": 0, "default": 0},
                "source_char_limit": {"type": "integer", "minimum": 1, "maximum": 1536, "default": 1024},
                "docstring_char_limit": {"type": "integer", "minimum": 1, "maximum": 1536, "default": 1024},
                "source_line_limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "graph_name": _GRAPH_NAME_PROP
            },
            "required": ["query"]
        }
    },

    "analyze_code_relationships": {
        "name": "analyze_code_relationships",
        "description": "Analyze code relationships like 'who calls this function' or 'class hierarchy'. Callee docstrings and variable value/context evidence are returned in bounded character pages with independent offsets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query_type": {
                    "type": "string",
                    "enum": [
                        "find_callers",
                        "find_callees",
                        "find_all_callers",
                        "find_all_callees",
                        "find_importers",
                        "who_modifies",
                        "class_hierarchy",
                        "overrides",
                        "dead_code",
                        "call_chain",
                        "module_deps",
                        "variable_scope",
                        "find_complexity",
                        "find_functions_by_argument",
                        "find_functions_by_decorator"
                    ]
                },
                "target": {
                    "type": "string",
                    "description": "The primary query target (for example, a function name, class name, or 'start_func->end_func' for call chains). For find_functions_by_argument, use a parameter name or type."
                },
                "context": {
                    "type": "string",
                    "description": "Additional context parameter: acts as a file path for precise scoping or a numeric string (e.g., depth/limit) depending on the query type."
                },
                "depth": {
                    "type": "integer",
                    "description": "Optional traversal depth for transitive caller and callee queries (1-20)."
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 99999,
                    "default": 0,
                    "description": "Offset for continuing a truncated find_callers/find_callees, find_all_callers/find_all_callees, class_hierarchy, or find_complexity page."
                },
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 500,
                    "description": "Maximum number of rows to return for find_callers, find_callees, find_all_callers, find_all_callees, class_hierarchy collections, or find_complexity; lower this value to retry a truncated response."
                },
                "docstring_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Character offset for the called-function docstring page when query_type is find_callees."
                },
                "docstring_char_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1536,
                    "default": 1024,
                    "description": "Maximum called-function docstring characters per result for find_callees."
                },
                "value_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Character offset for each variable value page when query_type is variable_scope."
                },
                "context_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Character offset for each variable context page when query_type is variable_scope."
                },
                "decorator_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 99999,
                    "default": 0,
                    "description": "Offset for each function's decorator list when query_type is find_functions_by_decorator. Use decorators_next_offset to retrieve the next bounded slice."
                },
                "decorator_page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 16,
                    "default": 16,
                    "description": "Maximum decorators to return per matching function when query_type is find_functions_by_decorator."
                },
                "decorator_value_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 99999,
                    "default": 0,
                    "description": "Character offset for each decorator value when query_type is find_functions_by_decorator. Use decorators_value_next_offset to retrieve the next bounded slice."
                },
                "decorator_value_page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 128,
                    "default": 128,
                    "description": "Maximum characters per decorator value when query_type is find_functions_by_decorator."
                },
                "evidence_char_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1536,
                    "default": 1024,
                    "description": "Maximum characters per variable value and context page for variable_scope."
                },
                "repo_path": {
                    "type": "string",
                    "description": "Optional repository path."
                },
                "graph_name": _GRAPH_NAME_PROP
            },
            "required": ["query_type", "target"]
        }
    },

    "watch_directory": {
        "name": "watch_directory",
        "description": "Continuously monitors a directory and keeps graph updated.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to repository root"
                }
            },
            "required": ["repo_path"]
        }
    },

    "execute_cypher_query": {
        "name": "execute_cypher_query",
        "description": "Run a read-only Cypher query scoped to the active repository by default. Set global_scope=true only for an intentional cross-repository query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cypher_query": {
                    "type": "string",
                    "description": "The Cypher query to execute"
                },
                "params": {
                    "type": "object",
                    "description": "Optional named parameters passed to the Cypher query.",
                    "default": {}
                },
                "repo_path": {
                    "type": "string",
                    "description": "Optional repository root; defaults to the active repository when global_scope is false."
                },
                "global_scope": {
                    "type": "boolean",
                    "description": "Explicitly query across repositories instead of applying repo_path scope.",
                    "default": False
                },
                "offset": {"type": "integer", "minimum": 0, "maximum": 99999, "default": 0},
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 100,
                    "description": "Pagination requires a final ORDER BY containing the primary key of every matched node and a simple node-only query. Relationship patterns, CALL, UNWIND, UNION, and WITH clauses are rejected for continuation because they can change row identity.",
                },
                "graph_name": _GRAPH_NAME_PROP
            },
            "required": ["cypher_query"]
        }
    },

    "add_package_to_graph": {
        "name": "add_package_to_graph",
        "description": "Add a package to the graph.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "package_name": {
                    "type": "string"
                },
                "language": {
                    "type": "string",
                    "enum": ["python", "javascript", "typescript", "java", "c", "go", "ruby", "php", "cpp"]
                },
                "is_dependency": {
                    "type": "boolean",
                    "default": True
                },
                "timeout_seconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "Optional indexing deadline in seconds. A deadline stops the job at the next safe database boundary."
                },
                "graph_name": _GRAPH_NAME_PROP
            },
            "required": ["package_name", "language"]
        }
    },

    "find_dead_code": {
        "name": "find_dead_code",
        "description": (
            "Find potentially unused functions with bounded paged results. Use "
            "find_code to retrieve source or docstring evidence on demand."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "exclude_decorated_with": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": []
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 99999,
                    "default": 0,
                },
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 50,
                    "description": "Maximum dead-code results in this page; use next_offset to continue.",
                },
                "repo_path": {
                    "type": "string"
                },
                "graph_name": _GRAPH_NAME_PROP
            }
        }
    },

    "calculate_cyclomatic_complexity": {
        "name": "calculate_cyclomatic_complexity",
        "description": "Calculate complexity of a function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_name": {"type": "string"},
                "path": {"type": "string", "description": "Optional file path to disambiguate the function."},
                "repo_path": {"type": "string"},
                "graph_name": _GRAPH_NAME_PROP
            },
            "required": ["function_name"]
        }
    },

    "find_most_complex_functions": {
        "name": "find_most_complex_functions",
        "description": "Find most complex functions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10},
                "repo_path": {"type": "string"},
                "graph_name": _GRAPH_NAME_PROP
            }
        }
    },

    "list_indexed_repositories": {
        "name": "list_indexed_repositories",
        "description": "List all indexed repositories.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "graph_name": _GRAPH_NAME_PROP
            }
        }
    },

    "delete_repository": {
        "name": "delete_repository",
        "description": (
            "DESTRUCTIVE AND IRREVERSIBLE. Permanently deletes a repository and "
            "every node and relationship belonging to it from the graph. There is "
            "no undo, and recovering the data requires a full re-index, which can "
            "take a long time on a large repository. Only call this when the user "
            "has explicitly asked for that repository to be removed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "The path of the repository to delete."
                },
                "graph_name": _GRAPH_NAME_PROP
            },
            "required": ["repo_path"]
        }
    },

    "visualize_graph_query": {
        "name": "visualize_graph_query",
        "description": "Generate a visualization URL for a Cypher query scoped to the active repository by default. Set global_scope=true to visualize across repositories.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cypher_query": {"type": "string"},
                "repo_path": {
                    "type": "string",
                    "description": "Optional repository root; defaults to the active repository.",
                },
                "global_scope": {
                    "type": "boolean",
                    "description": "Allow results across repositories; defaults to the active repository.",
                },
                "graph_name": _GRAPH_NAME_PROP
            },
            "required": ["cypher_query"]
        }
    },

    "list_watched_paths": {
        "name": "list_watched_paths",
        "description": "List all watched directories.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },

    "unwatch_directory": {
        "name": "unwatch_directory",
        "description": "Stop watching a directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"}
            },
            "required": ["repo_path"]
        }
    },

    "load_bundle": {
        "name": "load_bundle",
        "description": (
            "Load a pre-indexed graph bundle (.cgc) into the database. Note that "
            "clear_existing=True is DESTRUCTIVE AND IRREVERSIBLE — see its "
            "description before setting it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "bundle_name": {
                    "type": "string",
                    "description": "Name of the bundle to load from the registry, or a path to a local .cgc file."
                },
                "clear_existing": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "DESTRUCTIVE AND IRREVERSIBLE. When true, the existing "
                        "repository data is deleted from the graph before the bundle "
                        "is imported — this discards previously indexed content, not "
                        "just a previous copy of this bundle. Leave false unless the "
                        "user has explicitly asked to replace what is already indexed."
                    )
                },
                "graph_name": _GRAPH_NAME_PROP
            },
            "required": ["bundle_name"]
        }
    },

    "search_registry_bundles": {
        "name": "search_registry_bundles",
        "description": "Search registry bundles.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "unique_only": {"type": "boolean", "default": False}
            }
        }
    },

    "get_repository_stats": {
        "name": "get_repository_stats",
        "description": "Get repository statistics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "graph_name": _GRAPH_NAME_PROP
            }
        }
    },

    "discover_codegraph_contexts": {
        "name": "discover_codegraph_contexts",
        "description": "Discover repository-local graph context folders (default: .codegraphcontext).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "max_depth": {"type": "integer", "default": 1}
            }
        }
    },

    "switch_context": {
        "name": "switch_context",
        "description": (
            "Switch active graph context. Refuses while any indexing job is "
            "PENDING or RUNNING — wait for check_job_status / list_jobs, then retry."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context_path": {"type": "string"},
                "save": {"type": "boolean", "default": True}
            },
            "required": ["context_path"]
        }
    },

    "list_graphs": {
        "name": "list_graphs",
        "description": "List all available graphs in the FalkorDB instance. Each graph typically corresponds to an indexed repository. Use the graph names with the 'graph_name' parameter in other tools.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },

    "generate_report": {
        "name": "generate_report",
        "description": "Generate codegraph report.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_path": {"type": "string"},
                "include_java": {"type": "boolean", "default": False},
                "repo_path": {
                    "type": "string",
                    "description": "Repository root; defaults to the active repository.",
                },
                "god_node_limit": {"type": "integer", "default": 15},
                "complexity_limit": {"type": "integer", "default": 15},
                "cross_module_limit": {"type": "integer", "default": 20}
            }
        }
    },

    "find_java_spring_endpoints": {
        "name": "find_java_spring_endpoints",
        "description": "Find Spring endpoints in bounded, stable pages using offset and page_size.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "http_method": {"type": "string"},
                "path_pattern": {"type": "string"},
                "repo_path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "maximum": 99999, "default": 0},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
                "graph_name": _GRAPH_NAME_PROP
            }
        }
    },

    "find_java_spring_beans": {
        "name": "find_java_spring_beans",
        "description": "Find Spring beans in bounded, stable pages using offset and page_size.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "stereotype": {
                    "type": "string",
                    "enum": ["CONTROLLER", "REST_CONTROLLER", "SERVICE", "REPOSITORY", "COMPONENT", "CONFIGURATION"]
                },
                "repo_path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "maximum": 99999, "default": 0},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
                "graph_name": _GRAPH_NAME_PROP
            }
        }
    },

    "find_datasource_nodes": {
        "name": "find_datasource_nodes",
        "description": "Query datasource nodes linked to code in the active repository by default. Set global_scope=true to include unscoped datasource and Redis key-pattern metadata. Datasources, tables, columns, and key patterns each use bounded offset/page_size inputs and return continuation offsets when more rows remain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["mysql", "cassandra", "redis"]
                },
                "name": {"type": "string"},
                "include_columns": {"type": "boolean"},
                "offset": {"type": "integer", "minimum": 0, "maximum": 99999, "default": 0},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
                "table_offset": {"type": "integer", "minimum": 0, "maximum": 99999, "default": 0},
                "table_page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
                "column_offset": {"type": "integer", "minimum": 0, "maximum": 99999, "default": 0},
                "column_page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
                "key_pattern_offset": {"type": "integer", "minimum": 0, "maximum": 99999, "default": 0},
                "key_pattern_page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
                "repo_path": {"type": "string", "description": "Repository path used to scope data through indexed code references."},
                "global_scope": {"type": "boolean", "description": "Include datasource data across the active graph."},
                "graph_name": _GRAPH_NAME_PROP
            }
        }
    },

    "simulate_metrics": {
        "name": "simulate_metrics",
        "description": "Calculate repository architectural metrics (coupling, cohesion, circular dependencies, complexity, and maintainability).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the repository (defaults to current workspace)."
                },
                "context": {
                    "type": "string",
                    "description": "Optional: Specific CGC context to use."
                }
            }
        }
    },

    "simulate_architectural_change": {
        "name": "simulate_architectural_change",
        "description": "Simulate architectural modifications (service decomposition, adding/removing dependencies, deleting nodes) and compare metrics against the baseline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the repository (defaults to current workspace)."
                },
                "changes": {
                    "type": "array",
                    "description": "List of simulation mutation steps.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["decompose", "remove_dependency", "add_dependency", "remove_node"]
                            },
                            "mapping": {
                                "type": "object",
                                "description": "For decompose: mapping of node_id/path to service name."
                            },
                            "source": {
                                "type": "string",
                                "description": "For dependencies: source node id or name."
                            },
                            "target": {
                                "type": "string",
                                "description": "For dependencies: target node id or name."
                            },
                            "rel_type": {
                                "type": "string",
                                "description": "Optional: relationship type (e.g. CALLS, IMPORTS)."
                            },
                            "node_id": {
                                "type": "string",
                                "description": "For remove_node: node id, path, or name to delete."
                            }
                        },
                        "required": ["type"]
                    }
                },
                "context": {
                    "type": "string",
                    "description": "Optional: Specific CGC context to use."
                }
            },
            "required": ["changes"]
        }
    },

    "analyze_architectural_evolution": {
        "name": "analyze_architectural_evolution",
        "description": "Analyze repository growth trend and identify Technical Debt Hotspots (combining code complexity and Git commit churn).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to the repository (defaults to current workspace)."
                },
                "commits": {
                    "type": "integer",
                    "description": "Number of commits to analyze (default: 50).",
                    "default": 50
                },
                "context": {
                    "type": "string",
                    "description": "Optional: Specific CGC context to use."
                }
            }
        }
    }
}
