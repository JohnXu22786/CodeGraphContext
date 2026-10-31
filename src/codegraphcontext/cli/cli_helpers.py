# src/codegraphcontext/cli/cli_helpers.py
import asyncio
from contextlib import contextmanager
import json
import re
import shlex
import uuid
import urllib.parse
from collections import Counter
from pathlib import Path
import time
import os
from typing import Optional, List, Dict, Any
import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeRemainingColumn,
    MofNCompleteColumn,
)

from ..core import get_database_manager
from ..core.database_ownership import (
    DatabaseOwnershipError,
    GatewayRequestError,
    GatewayUnavailableError,
    find_active_gateway,
)
from ..core.jobs import JobManager
from ..tools.code_finder import CodeFinder
from ..tools.graph_builder import GraphBuilder, PARSER_EXTENSIONS
from ..tools.indexing.discovery import (
    inspect_files_to_index,
    root_file_symlink_exclusion_reason,
)
from ..utils import graph_shapes
from ..tools.package_resolver import get_local_package_path
from ..utils.debug_log import info_logger, warning_logger
from ..core.database import Neo4jConnectionError
from ..utils.repo_path import any_repo_matches_path
from .config_manager import (
    resolve_context,
    ResolvedContext,
    register_repo_in_context,
    ensure_first_run_bootstrap,
    ContextNotFoundError,
    is_db_deletion_allowed,
)

console = Console()


def _fail_services_init() -> None:
    """Abort the CLI command when database/services could not be initialized."""
    raise typer.Exit(code=1)


@contextmanager
def _generation_update_for_context(db_manager, ctx):
    """Track a synchronous CLI graph mutation for the selected local DB."""
    db_path = getattr(db_manager, "db_path", None)
    backend = getattr(db_manager, "get_backend_type", lambda: "")()
    if not db_path or backend not in {"falkordb", "kuzudb", "ladybugdb"}:
        yield None
        return

    from ..core.index_generation import generation_update
    from .snapshot_commands import effective_snapshot_identity

    identity, _ = effective_snapshot_identity(ctx, Path.cwd())
    with generation_update(db_path, identity) as update:
        yield update


def _reject_external_file_symlink(path: Path) -> None:
    """Reject a file symlink that would move indexing outside its requested root."""
    reason = root_file_symlink_exclusion_reason(path)
    if reason:
        console.print(f"[red]Error: Cannot index {path.absolute()}: {reason}[/red]")
        raise typer.Exit(code=1)


def _kuzu_fallback_path(ctx: ResolvedContext) -> Optional[str]:
    """Derive a KùzuDB directory when falling back from another backend."""
    runtime = os.getenv("CGC_RUNTIME_DB_PATH")
    if runtime:
        return str(Path(runtime).expanduser().resolve())
    if ctx.db_path:
        return str(Path(ctx.db_path).parent / "kuzudb")
    try:
        from .config_manager import _default_global_db_path
        return _default_global_db_path("kuzudb")
    except Exception:
        return None


def _embedded_gateway_candidates(
    database_path: Optional[str], backend: Optional[str]
) -> list[tuple[str, str]]:
    """Return effective embedded paths that the configured backend can use.

    The database manager factory can fall back from FalkorDB Lite to KùzuDB or
    LadybugDB, and from KùzuDB to LadybugDB. Resolve those same local paths
    before opening a CLI manager so it can find the gateway that owns a fallback
    database.
    """
    if not database_path:
        return []

    requested = (backend or "").lower()
    if requested in {"neo4j", "nornic", "falkordb-remote"}:
        return []

    fallbacks = {
        "falkordb": ("kuzudb", "ladybugdb"),
        "kuzudb": ("ladybugdb",),
        "ladybugdb": (),
        "": ("kuzudb", "ladybugdb"),
    }
    if requested not in fallbacks:
        return []
    if not requested and os.getenv("FALKORDB_HOST"):
        return []

    from ..core import _fallback_db_path_for

    candidates: list[tuple[str, str]] = []
    if requested in {"kuzudb", "ladybugdb"}:
        candidates.append((database_path, requested))
    for fallback_backend in fallbacks[requested]:
        fallback_path = _fallback_db_path_for(database_path, fallback_backend)
        if fallback_path:
            candidates.append((fallback_path, fallback_backend))
    return candidates


def _print_call_resolution_diagnostics(graph_builder: GraphBuilder, limit: int = 5) -> None:
    diagnostics = getattr(graph_builder, "last_call_resolution_diagnostics", [])
    if not diagnostics:
        return

    reason_counts = Counter(d.get("reason", "unknown") for d in diagnostics)
    summary = ", ".join(
        f"{reason}={count}" for reason, count in reason_counts.most_common()
    )
    console.print(
        f"[yellow]Skipped {len(diagnostics)} unresolved call relationship(s): {summary}[/yellow]"
    )
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Call", style="cyan", overflow="fold")
    table.add_column("Reason", style="yellow")
    table.add_column("Location", style="dim", overflow="fold")
    for diagnostic in diagnostics[:limit]:
        table.add_row(
            str(diagnostic.get("full_call_name") or ""),
            str(diagnostic.get("reason") or ""),
            f"{diagnostic.get('caller_file_path')}:{diagnostic.get('line_number')}",
        )
    console.print(table)


def _format_extension_counts(files_by_extension: Dict[str, int]) -> str:
    if not files_by_extension:
        return "None"
    return ", ".join(
        f"{extension}: {count}" for extension, count in files_by_extension.items()
    )


def _print_index_execution_summary(graph_builder: GraphBuilder) -> None:
    summary = getattr(graph_builder, "last_index_summary", None)
    if not summary:
        return

    table = Table(
        title="CGC Index Execution Summary",
        show_header=True,
        header_style="bold cyan",
        box=box.ASCII,
    )
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", style="green", overflow="fold")
    table.add_row("Total scanned files", str(summary.get("total_scanned_files", 0)))
    table.add_row(
        "Files by extension",
        _format_extension_counts(summary.get("files_by_extension", {})),
    )
    failed_files = summary.get("failed_files", 0)
    if failed_files:
        table.add_row("[red]Files failed to parse[/red]", f"[red]{failed_files}[/red]")
    table.add_row("Function nodes", str(summary.get("function_nodes", 0)))
    table.add_row("Class nodes", str(summary.get("class_nodes", 0)))
    table.add_row("CALLS edges", str(summary.get("call_edges", 0)))
    table.add_row(
        "Serialization seconds",
        f"{summary.get('serialization_seconds', 0.0):.2f}",
    )
    console.print(table)
    # Always-visible warnings (#1597): these describe an explicitly enabled
    # feature that did NOT run — they must not depend on the log level.
    for warning in summary.get("warnings", []):
        console.print(f"[bold yellow]⚠ {warning}[/bold yellow]")


def _initialize_services(
    cli_context_flag: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> tuple[Any, Any, Any, ResolvedContext]:
    """
    Initializes and returns core service managers based on the resolved context.
    Returns (db_manager, graph_builder, code_finder, resolved_context).
    """
    ensure_first_run_bootstrap()
    console.print("[dim]Resolving context...[/dim]")
    try:
        ctx = resolve_context(cli_context_flag, cwd=cwd)
    except ContextNotFoundError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1)
    
    # Let the user know what context we're operating in
    if ctx.mode == "named":
        console.print(f"[cyan]Context:[/cyan] {ctx.context_name} (Database: {ctx.database})")
    elif ctx.mode == "per-repo":
        console.print(f"[cyan]Context:[/cyan] Per-repo local mode (Database: {ctx.database})")
    else:
        # Default global mode — silent to keep CLI clean for existing users
        pass

    console.print("[dim]Initializing services and database connection...[/dim]")
    try:
        # Respect runtime/backend overrides. Context DB is only a default when
        # neither runtime override nor DEFAULT_DATABASE is already set.
        if (
            ctx.database
            and not os.getenv("CGC_RUNTIME_DB_TYPE")
            and not os.getenv("DEFAULT_DATABASE")
        ):
            os.environ["DEFAULT_DATABASE"] = ctx.database
        
        # Pass the exact DB path resolved from the context, or the runtime override
        runtime_path = os.getenv("CGC_RUNTIME_DB_PATH")
        db_manager = get_database_manager(db_path=runtime_path or ctx.db_path)
    except ValueError as e:
        console.print(f"[bold red]Database Configuration Error:[/bold red] {e}")
        _fail_services_init()

    try:
        db_manager.get_driver()
    except Exception as e:
        if isinstance(e, DatabaseOwnershipError):
            if console.quiet:
                typer.echo(f"Database Ownership Error: {e}", err=True)
            else:
                console.print(f"[bold red]Database Ownership Error:[/bold red] {e}")
            _fail_services_init()

        # Check if this is a FalkorDB failure that should trigger a KùzuDB fallback
        from ..core.database_falkordb import FalkorDBUnavailableError
        if isinstance(e, FalkorDBUnavailableError):
            from ..core import mark_falkordb_unavailable
            mark_falkordb_unavailable()
            console.print(f"[yellow]⚠ FalkorDB Lite is not functional in this environment: {e}[/yellow]")
            console.print("[cyan]Falling back to KùzuDB for a reliable experience...[/cyan]")

            # Close the broken driver/socket
            try:
                db_manager.close_driver()
            except Exception:
                pass
            
            # Re-initialize explicitly with KùzuDB (never reuse the FalkorDB directory)
            from ..core.database_kuzu import KuzuDBManager
            kuzu_path = _kuzu_fallback_path(ctx)
            db_manager = KuzuDBManager(db_path=kuzu_path)
            try:
                db_manager.get_driver()
                console.print("[green]✓[/green] Successfully switched to KùzuDB fallback")
            except Exception as kuzu_e:
                console.print(f"[bold red]Critical Error:[/bold red] Both FalkorDB and KùzuDB failed: {kuzu_e}")
                _fail_services_init()
        else:
            selected_db = (
                os.environ.get("CGC_RUNTIME_DB_TYPE")
                or os.environ.get("DATABASE_TYPE")
                or os.environ.get("DEFAULT_DATABASE")
                or ""
            ).lower()

            if isinstance(e, Neo4jConnectionError):
                console.print(f"[bold red]{e}[/bold red]")
                allow_fallback = os.environ.get("CGC_ALLOW_NEO4J_FALLBACK", "false").lower() in {"1", "true", "yes", "on"}

                if selected_db == "neo4j" and allow_fallback:
                    console.print("[cyan]Neo4j failed and CGC_ALLOW_NEO4J_FALLBACK=true. Falling back to KuzuDB...[/cyan]")
                    try:
                        from ..core.database_kuzu import KuzuDBManager
                        db_manager = KuzuDBManager(db_path=_kuzu_fallback_path(ctx))
                        db_manager.get_driver()
                        console.print("[green]✓[/green] Successfully switched to KuzuDB fallback")
                    except Exception as kuzu_e:
                        console.print(f"[bold red]Critical Error:[/bold red] Neo4j failed and KuzuDB fallback failed: {kuzu_e}")
                        _fail_services_init()
                else:
                    if selected_db == "neo4j":
                        console.print("[yellow]Tip:[/yellow] To continue without Neo4j, rerun with --db kuzudb")
                    _fail_services_init()
            else:
                console.print(f"[bold red]Database Connection Error:[/bold red] {e}")
                if "Could not set lock on file" in str(e):
                    console.print(
                        "[yellow]Another CGC process already has this embedded database open. "
                        "If a CGC Gateway or watcher is running, use its MCP/HTTP interface "
                        "or stop it before running database-backed CLI commands.[/yellow]"
                    )
                else:
                    console.print("Please ensure your database is configured correctly or run 'cgc doctor'.")
                _fail_services_init()
    
    # The GraphBuilder requires an event loop, even for synchronous-style execution
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    graph_builder = GraphBuilder(db_manager, JobManager(), loop)
    code_finder = CodeFinder(db_manager)
    console.print("[dim]Services initialized.[/dim]")
    return db_manager, graph_builder, code_finder, ctx


async def _run_index_with_progress(graph_builder: GraphBuilder, path_obj: Path, is_dependency: bool = False, cgcignore_path: str = None, disable_progress: bool = False):
    """Internal helper to run indexing with a Live progress bar."""
    job_id = graph_builder.job_manager.create_job(str(path_obj), is_dependency=is_dependency)
    
    disable_progress = (
        disable_progress
        or not console.is_terminal
        or os.environ.get("CI", "").lower() == "true"
    )

    if not disable_progress:
        os.environ["CGC_ACTIVE_PROGRESS_BAR"] = "1"

    try:
        # Create the progress bar
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            TextColumn("[dim]{task.fields[filename]}"),
            console=console,
            transient=True,
            disable=disable_progress,
        ) as progress:
        
            task_id = progress.add_task(
                "Indexing...", 
                total=None,  # Will be updated once file discovery is done
                filename=""
            )

            indexing_task = asyncio.create_task(
                graph_builder.build_graph_from_path_async(path_obj, is_dependency=is_dependency, job_id=job_id, cgcignore_path=cgcignore_path)
            )

            from ..core.jobs import JobStatus
            
            # Poll for updates
            while not indexing_task.done():
                job = graph_builder.job_manager.get_job(job_id)
                if job:
                    if job.total_files > 0:
                        progress.update(task_id, total=job.total_files, completed=job.processed_files)
                    
                    # Prefer post-processing status over the last parsed file path
                    current_file = job.status_message or job.current_file or ""
                    if len(current_file) > 40:
                        current_file = "..." + current_file[-37:]
                    progress.update(task_id, filename=current_file)

                    if job.status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]:
                        break
                
                await asyncio.sleep(0.1)

            # Wait for actual completion and handle final state
            try:
                await indexing_task
                job = graph_builder.job_manager.get_job(job_id)
                if job and job.status == JobStatus.FAILED:
                    error_msg = job.errors[0] if job.errors else "Unknown error"
                    raise RuntimeError(error_msg)
            except Exception as e:
                raise e
    finally:
        os.environ.pop("CGC_ACTIVE_PROGRESS_BAR", None)


def index_helper(path: str, context: Optional[str] = None, no_progress: bool = False):
    """Synchronously indexes a repository in a given context."""
    time_start = time.time()
    requested_path = Path(path)
    _reject_external_file_symlink(requested_path)
    path_obj = requested_path.resolve()
    # Normalize to forward slashes for cross-platform DB consistency.
    # The graph DB always stores paths via Path.resolve().as_posix(),
    # so Cypher queries must also use forward slashes on Windows.
    repo_path_str = path_obj.as_posix()
    if not path_obj.exists():
        console.print(f"[red]Error: Path does not exist: {path_obj}[/red]")
        raise typer.Exit(code=1)

    index_cwd = path_obj if path_obj.is_dir() else path_obj.parent
    services = _initialize_services(context, cwd=index_cwd)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, graph_builder, code_finder, ctx = services
    from contextlib import nullcontext
    from codegraphcontext.core.index_generation import generation_is_current, generation_update
    from .snapshot_commands import effective_snapshot_identity

    generation_identity, generation_repository_root = effective_snapshot_identity(ctx, path_obj)
    generation_db_path = getattr(db_manager, "db_path", None)
    generation_marker_enabled = bool(
        generation_db_path
        and getattr(db_manager, "get_backend_type", lambda: "")()
        in {"falkordb", "kuzudb", "ladybugdb"}
    )

    index_generation_update = (
        generation_update(
            generation_db_path,
            generation_identity,
            generation_id=(
                f"{generation_identity.get('storage_key', '')}:"
                f"{generation_identity.get('commit') or 'uncommitted'}"
            ),
        )
        if generation_marker_enabled else nullcontext()
    )

    # Register the repo in its named context BEFORE the already-indexed skip
    # guard below: the early return used to sit above this call, so a graph
    # populated by any path that skipped registration (MCP add_code_to_graph,
    # an interrupted run) could never reconcile config.yaml through the normal
    # command — 'cgc context list' showed "Repos Linked: 0" forever (#1317).
    # Registration is idempotent, so running it on every invocation is safe.
    # The resolved context name is used so a *default* named context registers
    # too, not only an explicit --context flag.
    if ctx.mode == "named":
        register_context_name = context or getattr(ctx, "context_name", None)
        if register_context_name:
            if not register_repo_in_context(register_context_name, str(path_obj), auto_create=False):
                db_manager.close_driver()
                raise typer.Exit(code=1)

    indexed_repos = code_finder.list_indexed_repositories()
    repo_exists = any_repo_matches_path(indexed_repos, path_obj)
    marker_stale = generation_marker_enabled and not generation_is_current(
        generation_db_path, generation_identity
    )

    if repo_exists:
        # Check if the repository actually has files (not just an empty node from interrupted indexing)
        # Use variable-length path to handle both flat (Repository->File) and
        # hierarchical (Repository->Directory->...->File) graph structures
        try:
            with db_manager.get_driver().session() as session:
                result = session.run(
                    "MATCH (r:Repository {path: $path})-[:CONTAINS*]->(f:File) RETURN count(DISTINCT f) as file_count",
                    path=repo_path_str
                )
                record = result.single()
                file_count = record["file_count"] if record else 0
                
                if file_count > 0:
                    expected = graph_builder.estimate_processing_time(path_obj, cgcignore_path=ctx.cgcignore_path) if path_obj.is_dir() else None
                    expected_file_count = expected[0] if expected else None
                    if marker_stale:
                        console.print(
                            "[yellow]Stored index generation does not match the current "
                            "branch/worktree/commit; rebuilding it.[/yellow]"
                        )
                    elif expected_file_count is None or file_count >= expected_file_count:
                        console.print(f"[yellow]Repository '{path}' is already indexed with {file_count} files. Skipping.[/yellow]")
                        console.print("[dim]💡 Tip: Use 'cgc index --force' to re-index[/dim]")
                        db_manager.close_driver()
                        return
                    console.print(
                        f"[yellow]Repository '{path}' has only {file_count} of {expected_file_count} files indexed. Continuing.[/yellow]"
                    )
                    # Name the gap: without this, a pipeline that can never
                    # produce nodes for some files loops on the message above
                    # forever with nothing actionable (#1673).
                    try:
                        from codegraphcontext.tools.indexing.discovery import discover_files_to_index
                        discovered, _ = discover_files_to_index(
                            path_obj,
                            cgcignore_path=ctx.cgcignore_path,
                            supported_extensions=set(graph_builder.parsers.keys()),
                        )
                        with db_manager.get_driver().session() as s2:
                            res2 = s2.run(
                                "MATCH (r:Repository {path: $path})-[:CONTAINS*]->(f:File) RETURN DISTINCT f.path AS p",
                                path=repo_path_str,
                            )
                            have = {row["p"] for row in res2 if row.get("p")}
                        missing = [
                            str(f) for f in discovered
                            if str(f.resolve()) not in have and f.resolve().as_posix() not in have
                        ]
                        if missing:
                            shown = ", ".join(Path(m).name for m in missing[:5])
                            more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
                            console.print(f"[dim]Missing: {shown}{more}[/dim]")
                    except Exception:
                        pass
                else:
                    console.print(f"[yellow]Repository '{path}' exists but has no files (likely interrupted). Re-indexing...[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not check file count: {e}. Proceeding with indexing...[/yellow]")

    console.print(f"Starting indexing for: {path_obj}")

    try:
        with index_generation_update as generation_update_state:
            if marker_stale and repo_exists:
                console.print(
                    "[yellow]Clearing records from the stale generation before rebuilding.[/yellow]"
                )
                graph_builder.delete_repository_from_graph(str(path_obj))
            asyncio.run(_run_index_with_progress(graph_builder, path_obj, is_dependency=False, cgcignore_path=ctx.cgcignore_path, disable_progress=no_progress))
            if (
                getattr(ctx, "is_local", False)
                and generation_identity.get("kind") in {"git", "non-git"}
                and generation_update_state is not None
                and path_obj.is_dir()
                and generation_repository_root is not None
                and path_obj.resolve() == generation_repository_root.resolve()
            ):
                generation_update_state.mark_complete()
        time_end = time.time()
        elapsed = time_end - time_start
        _print_call_resolution_diagnostics(graph_builder)
        _print_index_execution_summary(graph_builder)
        console.print(f"[green]Successfully finished indexing: {path_obj} in {elapsed:.2f} seconds[/green]")
        
        # Check if auto-watch is enabled
        try:
            from codegraphcontext.cli.config_manager import get_config_value
            auto_watch = get_config_value('ENABLE_AUTO_WATCH')
            if auto_watch and str(auto_watch).lower() == 'true':
                console.print("\n[cyan]🔍 ENABLE_AUTO_WATCH is enabled. Starting watcher...[/cyan]")
                db_manager.close_driver()  # Close before starting watcher
                watch_helper(path)  # This will block the terminal
                return  # watch_helper handles its own cleanup
        except Exception as e:
            console.print(f"[yellow]Warning: Could not check ENABLE_AUTO_WATCH: {e}[/yellow]")
            
    except Exception as e:
        console.print(f"[bold red]An error occurred during indexing:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()


def dry_run_index_helper(path: str, context: Optional[str] = None) -> None:
    """Print the exact indexing scope without opening the graph database."""
    requested_path = Path(path)
    try:
        # Keep an outside file symlink lexical so discovery can explain why it
        # was excluded instead of silently scanning its resolved target.
        if root_file_symlink_exclusion_reason(requested_path):
            path_obj = requested_path.absolute()
        else:
            path_obj = requested_path.resolve()
    except OSError as exc:
        console.print(f"[red]Error resolving path: {exc}[/red]")
        raise typer.Exit(code=1)

    if not path_obj.exists():
        console.print(f"[red]Error: Path does not exist: {path_obj}[/red]")
        raise typer.Exit(code=1)

    index_cwd = path_obj if path_obj.is_dir() else path_obj.parent
    try:
        ctx = resolve_context(
            context,
            cwd=index_cwd,
            create_local_context=False,
            read_only=True,
        )
    except ContextNotFoundError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1)

    preview = inspect_files_to_index(
        path_obj,
        cgcignore_path=ctx.cgcignore_path,
        supported_extensions=set(PARSER_EXTENSIONS),
    )
    console.print(f"[bold cyan]Index dry run for {preview.ignore_root}[/bold cyan]")
    console.print(f"[green]Files to index ({len(preview.files)}):[/green]")
    if preview.files:
        for file_path in preview.files:
            console.print(f"  + {file_path.relative_to(preview.ignore_root).as_posix()}")
    else:
        console.print("  (none)")

    console.print(f"[yellow]Excluded files and directories ({len(preview.excluded)}):[/yellow]")
    if preview.excluded:
        for excluded_path, reason in preview.excluded:
            relative_path = excluded_path.relative_to(preview.ignore_root).as_posix()
            console.print(f"  - {relative_path}: {reason}")
    else:
        console.print("  (none)")


def add_package_helper(package_name: str, language: str, context: Optional[str] = None):
    """Synchronously indexes a package."""
    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, graph_builder, code_finder, ctx = services

    package_path_str = get_local_package_path(package_name, language)
    if not package_path_str:
        console.print(f"[red]Error: Could not find package '{package_name}' for language '{language}'.[/red]")
        db_manager.close_driver()
        raise typer.Exit(code=1)

    package_path = Path(package_path_str)
    
    indexed_repos = code_finder.list_indexed_repositories()
    if any(repo.get("name") == package_name for repo in indexed_repos if repo.get("is_dependency")):
        console.print(f"[yellow]Package '{package_name}' is already indexed. Skipping.[/yellow]")
        db_manager.close_driver()
        return

    console.print(f"Starting indexing for package '{package_name}' at: {package_path}")

    try:
        with _generation_update_for_context(db_manager, ctx):
            asyncio.run(_run_index_with_progress(graph_builder, package_path, is_dependency=True, cgcignore_path=ctx.cgcignore_path))
        _print_call_resolution_diagnostics(graph_builder)
        _print_index_execution_summary(graph_builder)
        console.print(f"[green]Successfully finished indexing package: {package_name}[/green]")
    except Exception as e:
        console.print(f"[bold red]An error occurred during package indexing:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()


def list_repos_helper(context: Optional[str] = None):
    """Lists all indexed repositories."""
    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()
    
    db_manager, _, code_finder, ctx = services
    
    try:
        repos = code_finder.list_indexed_repositories()
        if not repos:
            console.print("[yellow]No repositories indexed yet.[/yellow]")
            return

        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Name", style="dim")
        table.add_column("Path")
        table.add_column("Type")

        for repo in repos:
            repo_type = "Dependency" if repo.get("is_dependency") else "Project"
            table.add_row(repo.get("name") or "", str(repo.get("path") or ""), repo_type)
        
        console.print(table)
    except Exception as e:
        console.print(f"[bold red]An error occurred:[/bold red] {e}")
    finally:
        db_manager.close_driver()


def delete_helper(repo_path: str, context: Optional[str] = None):
    """Deletes a repository from the graph."""
    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, graph_builder, _, ctx = services
    
    try:
        with _generation_update_for_context(db_manager, ctx):
            if graph_builder.delete_repository_from_graph(repo_path):
                console.print(f"[green]Successfully deleted repository: {repo_path}[/green]")
            else:
                console.print(f"[yellow]Repository not found in graph: {repo_path}[/yellow]")
                console.print("[dim]Tip: Use 'cgc list' to see available repositories.[/dim]")
                raise typer.Exit(code=1)
    except typer.Exit:
        # Control flow, not an error — let it through so the exit code survives.
        raise
    except Exception as e:
        console.print(f"[bold red]An error occurred:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()

def _print_query_exception(e: Exception, query: str) -> None:
    """
    Pretty-print a database query exception, surfacing the raw driver
    error message so Cypher syntax problems are clearly visible.
    """
    import traceback

    error_type = type(e).__name__
    error_module = type(e).__module__ or ""

    # Neo4j: CypherSyntaxError and other ClientError subclasses carry
    # a .message and .code attribute with the full server-side detail.
    if "neo4j" in error_module:
        code = getattr(e, "code", None)
        msg = getattr(e, "message", None) or str(e)
        console.print(f"[bold red]Query Error ({error_type}):[/bold red]")
        if code:
            console.print(f"  [yellow]Code:[/yellow] {code}")
        console.print(f"  [yellow]Message:[/yellow] {msg}")

    # FalkorDB: ResponseError / exceptions in falkordb or redis packages
    elif "falkordb" in error_module or "redis" in error_module:
        console.print(f"[bold red]Query Error ({error_type}):[/bold red]")
        console.print(f"  [yellow]Database message:[/yellow] {e}")

    # KuzuDB: RuntimeError from the kuzu extension
    # KuzuDB: RuntimeError from the kuzu extension or database_kuzu wrapper
    elif "kuzu" in error_module or (
        error_type == "RuntimeError" and "Parser exception" in str(e)
    ):
        console.print(f"[bold red]Query Error ({error_type}):[/bold red]")
        console.print(f"  [yellow]Database message:[/yellow] {e}")

    else:
        # Fallback: unknown backend — print type + message + traceback
        console.print(f"[bold red]An error occurred while executing query ({error_type}):[/bold red]")
        console.print(f"  [yellow]Message:[/yellow] {e}")
        console.print("[dim]--- Traceback ---[/dim]")
        console.print(f"[dim]{traceback.format_exc()}[/dim]")

    console.print(f"\n[dim]Failed query:[/dim]")
    console.print(f"[dim]  {query}[/dim]")


def cypher_helper(
    query: str,
    context: Optional[str] = None,
    *,
    repo_path: Optional[str] = None,
    global_scope: bool = False,
    offset: int = 0,
    page_size: int = 100,
):
    """Execute one bounded, repository-scoped read-only Cypher page."""
    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, _, _, ctx = services

    from ..utils.cypher_readonly import is_read_only_cypher, read_only_rejection_message

    if not is_read_only_cypher(query):
        console.print(f"[bold red]Error:[/bold red] {read_only_rejection_message()}")
        db_manager.close_driver()
        raise typer.Exit(code=1)

    try:
        if not repo_path and not global_scope:
            raise ValueError(
                "A repository scope is required; pass --global-scope to query all repositories."
            )

        from ..tools.handlers.query_handlers import execute_cypher_query
        from ..server import _apply_final_output_limits

        result = execute_cypher_query(
            db_manager,
            cypher_query=query,
            repo_path=repo_path,
            global_scope=global_scope,
            offset=offset,
            page_size=page_size,
        )
        if "error" in result:
            details = result.get("details") or result["error"]
            _print_query_exception(RuntimeError(details), query)
            raise typer.Exit(code=1)

        response_text = json.dumps(result, ensure_ascii=False, indent=2)
        bounded_text = _apply_final_output_limits(
            "execute_cypher_query",
            response_text,
            lambda text: text.encode("utf-8"),
        )
        console.file.write(bounded_text)
        console.file.flush()
    except typer.Exit:
        raise
    except Exception as e:
        _print_query_exception(e, query)
        raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()


def cypher_helper_visual(
    query: str,
    context: Optional[str] = None,
    *,
    repo_path: Optional[str] = None,
    global_scope: bool = False,
    offset: int = 0,
    page_size: int = 100,
):
    """Executes a read-only Cypher query and visualizes the results."""
    from .visualizer import visualize_cypher_results
    from ..utils.cypher_readonly import is_read_only_cypher, read_only_rejection_message

    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, _, _, ctx = services

    if not is_read_only_cypher(query):
        console.print(f"[bold red]Error:[/bold red] {read_only_rejection_message()}")
        db_manager.close_driver()
        raise typer.Exit(code=1)

    try:
        visualize_cypher_results(
            query,
            repo_path=repo_path,
            global_scope=global_scope,
            offset=offset,
            page_size=page_size,
        )
    except Exception as e:
        _print_query_exception(e, query)
        db_manager.close_driver()
        raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()


import uvicorn
import urllib.parse
from ..viz.server import run_server, set_db_manager

_OFFLINE_VIZ_DEFAULT_QUERY = """
MATCH (n)-[r]->(m)
RETURN n AS n, r AS rel, m AS m
LIMIT 300
"""

_OFFLINE_VIZ_MAX_QUERY_ROWS = 50
_OFFLINE_VIZ_MAX_RESULT_OFFSET = 99_999
_OFFLINE_VIZ_MAX_RESULT_ROWS = _OFFLINE_VIZ_MAX_RESULT_OFFSET + 1
_OFFLINE_VIZ_MAX_QUERY_RESULT_ROWS = _OFFLINE_VIZ_MAX_RESULT_ROWS + 1


def _bound_offline_visualization_query(
    query: str, *, max_rows: Optional[int] = None
) -> str:
    """Cap rows returned by an offline custom visualization query in Cypher."""
    from ..tools.handlers.query_handlers import (
        _mask_query_literals_and_comments,
        _top_level_cypher_clauses,
    )

    row_limit = max_rows or (_OFFLINE_VIZ_MAX_QUERY_ROWS + 1)
    masked = _mask_query_literals_and_comments(query)
    clauses = _top_level_cypher_clauses(masked)
    if clauses and clauses[-1][0] == "LIMIT":
        _, _, content_start = clauses[-1]
        limit_text = masked[content_start:].strip()
        match = re.fullmatch(r"(\d+)\s*;?", limit_text)
        if not match:
            raise ValueError(
                "Offline visualization requires a numeric final LIMIT so its "
                "database result window can be bounded."
            )
        bounded_limit = min(int(match.group(1)), row_limit)
        has_semicolon = masked.rstrip().endswith(";")
        prefix = query[:content_start].rstrip()
        return prefix + " " + str(bounded_limit) + (";" if has_semicolon else "")

    return query.rstrip().rstrip(";") + f"\nLIMIT {row_limit}"


def _render_offline_visualization(
    db_manager,
    repo_path: Optional[str] = None,
    cypher_query: Optional[str] = None,
    output_path: Optional[str] = None,
    global_scope: bool = False,
    offset: int = 0,
    page_size: int = _OFFLINE_VIZ_MAX_QUERY_ROWS,
) -> Dict[str, Any]:
    """Render the graph with the dependency-free single-file HTML renderer.

    The React frontend in ``viz/dist`` is not shipped in the wheel, which made
    every visualization entry point a hard failure on a plain ``pip install``.
    ``utils/visualize_graph.py`` is a complete, zero-dependency renderer that
    was sitting in the package with no callers — this wires it up so the
    command degrades to a working (simpler) view instead of exiting 1.
    """
    from ..utils.visualize_graph import open_in_browser, build_graph_data

    if type(offset) is not int or not 0 <= offset <= _OFFLINE_VIZ_MAX_RESULT_OFFSET:
        raise ValueError("offset must be an integer from 0 through 99999")
    if type(page_size) is not int or page_size < 1:
        raise ValueError("page_size must be a positive integer")

    page_size = min(
        page_size,
        _OFFLINE_VIZ_MAX_QUERY_ROWS,
        _OFFLINE_VIZ_MAX_RESULT_ROWS - offset,
    )
    query = cypher_query or _OFFLINE_VIZ_DEFAULT_QUERY
    query_params = {}
    truncated = False
    can_continue = False
    result_limit = None
    if not global_scope:
        if not repo_path:
            raise ValueError(
                "Offline visualization requires a repository scope or explicit global scope."
            )
        if cypher_query:
            from ..tools.handlers.query_handlers import _scope_cypher_query

            query, query_params = _scope_cypher_query(cypher_query, repo_path)
        else:
            normalized_repo_path = Path(repo_path).expanduser().resolve().as_posix()
            from ..viz.server import _repository_graph_query

            backend = getattr(db_manager, "get_backend_type", lambda: "neo4j")()
            query = _repository_graph_query(
                backend, offset, page_size
            )
            query_params = {"repo_path": normalized_repo_path}
            can_continue = True
    if not can_continue:
        from ..viz.server import _bounded_graph_projection_page

        backend = getattr(db_manager, "get_backend_type", lambda: "neo4j")()
        bounded_query = _bound_offline_visualization_query(
            query, max_rows=_OFFLINE_VIZ_MAX_QUERY_RESULT_ROWS
        )
        paged_query, result_limit = _bounded_graph_projection_page(
            bounded_query,
            backend,
            offset,
            page_size,
            max_result_limit=_OFFLINE_VIZ_MAX_QUERY_RESULT_ROWS,
        )
        if paged_query is not None:
            query = paged_query
            can_continue = True
        elif offset:
            raise ValueError(
                "Offline visualization continuation requires a stable n/rel/m "
                "graph query; use a generated graph query or add a supported result shape."
            )
        else:
            query = _bound_offline_visualization_query(query)
            from ..viz.server import _rewrite_bounded_graph_entity_projections

            bounded_entity_query, has_raw_entity_projection = (
                _rewrite_bounded_graph_entity_projections(query, backend)
            )
            if has_raw_entity_projection:
                if bounded_entity_query is None:
                    raise ValueError(
                        "Offline custom graph queries must return graph entities in a "
                        "supported shape so properties can be bounded."
                    )
                query = bounded_entity_query
            result_limit = None
    nodes: Dict[Any, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []

    def _ident(value) -> Optional[str]:
        return None if value is None else str(value)

    # Backend shape handling — which driver returns what, and why each spelling
    # has to be accepted — lives in utils/graph_shapes, shared with the live
    # web renderer in viz/server.py so the two cannot drift apart again.
    _meta = graph_shapes.meta
    _has_meta = graph_shapes.has_meta
    _is_relationship = graph_shapes.is_relationship
    _is_node = graph_shapes.is_node

    def _node_payload(value) -> Dict[str, Any]:
        if hasattr(value, "labels"):
            # FalkorDB's Node keeps properties in `.properties` and is not
            # itself iterable; Neo4j's Node supports dict() via Mapping.
            props = dict(value.properties) if hasattr(value, "properties") else dict(value)
            labels = list(getattr(value, "labels", []) or [])
            label = labels[0] if labels else "Node"
            node_id = getattr(value, "element_id", None) or getattr(value, "id", None)
        else:
            props, label = dict(value), _meta(value, "_label", "Node")
            node_id = _meta(value, "_id")
        if node_id is None:
            node_id = props.get("path") or props.get("name")
        
        name = props.get("name") or props.get("path") or ""
        if name == "<module>":
            _fpath = props.get("path") or props.get("file_path") or ""
            if _fpath:
                name = Path(_fpath).name
            else:
                name = "<module>"

        return {
            "id": _ident(node_id),
            "label": label,
            "name": name,
            "file_path": props.get("path", ""),
            "line_number": props.get("line_number"),
        }

    def _node_ref_id(ref) -> Any:
        # Neo4j's start_node/end_node are full Node objects; FalkorDB's
        # src_node/dest_node are already the bare node id (an int).
        if isinstance(ref, (int, str)):
            return ref
        return getattr(ref, "element_id", None) or getattr(ref, "id", None)

    def _edge_payload(value) -> Optional[Dict[str, Any]]:
        # Neo4j: .start_node/.end_node/.type. FalkorDB: .src_node/.dest_node/
        # .relation.
        start = getattr(value, "start_node", None)
        if start is None:
            start = getattr(value, "src_node", None)
        end = getattr(value, "end_node", None)
        if end is None:
            end = getattr(value, "dest_node", None)
        if start is not None and end is not None:
            return {
                "source": _ident(_node_ref_id(start)),
                "target": _ident(_node_ref_id(end)),
                "type": getattr(value, "type", None) or getattr(value, "relation", "RELATED"),
            }
        if isinstance(value, dict):
            return {
                "source": _ident(_meta(value, "_src")),
                "target": _ident(_meta(value, "_dst")),
                "type": _meta(value, "_label", "RELATED"),
            }
        return None

    record_count = 0
    with db_manager.get_driver().session() as session:
        for row_index, record in enumerate(session.run(query, **query_params)):
            if row_index >= page_size:
                truncated = True
                break
            record_count += 1
            values = list(record.values()) if hasattr(record, "values") else list(record)
            for value in values:
                if _is_node(value):
                    payload = _node_payload(value)
                    if payload["id"] is not None:
                        nodes[payload["id"]] = payload
                elif _is_relationship(value):
                    edge = _edge_payload(value)
                    if edge and edge["source"] and edge["target"]:
                        edges.append(edge)

    if not nodes:
        console.print(
            "[yellow]No graph data returned — index a repository first "
            "with 'cgc index <path>'.[/yellow]"
        )
        raise typer.Exit(code=1)

    title = f"CGC Code Graph — {Path(repo_path).name}" if repo_path else "CGC Code Graph"
    graph_data = build_graph_data(list(nodes.values()), edges)
    next_offset = offset + record_count if truncated and can_continue else None
    if next_offset is not None and next_offset > _OFFLINE_VIZ_MAX_RESULT_OFFSET:
        next_offset = None
    continuation_limit_reached = truncated and can_continue and next_offset is None
    graph_data["truncated"] = truncated
    graph_data["record_limit"] = page_size
    graph_data["offset"] = offset
    graph_data["page_size"] = page_size
    graph_data["record_count"] = record_count
    graph_data["continuation_supported"] = can_continue
    graph_data["next_offset"] = next_offset
    graph_data["has_more"] = next_offset is not None
    graph_data["continuation_limit_reached"] = continuation_limit_reached
    if result_limit is not None:
        graph_data["result_limit"] = result_limit
    path = open_in_browser(graph_data, title=title, output_path=output_path)
    console.print(
        f"[green]Rendered {len(nodes)} nodes and {len(edges)} edges to[/green] {path}"
    )
    if truncated:
        if next_offset is not None:
            if cypher_query:
                continuation = (
                    f"re-run the same offline visualization with --offset {next_offset} "
                    f"--page-size {page_size}"
                )
            else:
                command = ["cgc", "visualize", "--offset", str(next_offset)]
                if global_scope:
                    command.append("--global-scope")
                elif repo_path:
                    command.extend(["--repo", shlex.quote(repo_path)])
                continuation = "continue with " + " ".join(command)
            console.print(
                f"[yellow]Truncated after {record_count} query rows; "
                f"{continuation}.[/yellow]"
            )
        elif continuation_limit_reached:
            console.print(
                "[yellow]Truncated at the offline continuation limit; "
                "narrow the query to view a smaller subset.[/yellow]"
            )
        else:
            console.print(
                f"[yellow]Truncated after {page_size} query rows; this query shape cannot "
                "be paged safely, so narrow the query to view a smaller subset.[/yellow]"
            )
    return {
        "offset": offset,
        "page_size": page_size,
        "record_count": record_count,
        "result_limit": result_limit,
        "continuation_supported": can_continue,
        "has_more": next_offset is not None,
        "next_offset": next_offset,
        "truncated": truncated,
        "continuation_limit_reached": continuation_limit_reached,
    }


def visualize_helper(
    repo_path: Optional[str] = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    context: Optional[str] = None,
    cypher_query: Optional[str] = None,
    global_scope: bool = False,
    offset: int = 0,
    page_size: int = 100,
):
    """Generates an interactive visualization using the Playground UI."""
    if global_scope:
        repo_path = None
    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, _, _, ctx = services
    
    # Set the DB manager for the server
    set_db_manager(db_manager)
    
    # Determine the static directory (built React app)
    # This points to src/codegraphcontext/viz/dist where we build the website
    # (relative to src/codegraphcontext/cli/cli_helpers.py)
    # Using .resolve() is more robust for path comparison and existence checks
    this_file = Path(__file__).resolve()
    package_root = this_file.parent.parent
    static_dir = package_root / "viz" / "dist"
    
    # Fallback for development if not yet built in viz/dist
    if not static_dir.exists():
        # Look for website/dist in the project root (3 levels up from cli/cli_helpers.py, 4 parents)
        # 1: cli/, 2: codegraphcontext/, 3: src/, 4: project_root/
        project_root = this_file.parent.parent.parent.parent
        dev_static_dir = project_root / "website" / "dist"
        
        # Also try one level up from package_root just in case of different layouts
        alt_dev_dir = package_root.parent.parent / "website" / "dist"
        
        if dev_static_dir.exists():
            static_dir = dev_static_dir
        elif alt_dev_dir.exists():
            static_dir = alt_dev_dir
        else:
            # Last resort: try current working directory
            cwd_static_dir = Path.cwd() / "website" / "dist"
            if cwd_static_dir.exists():
                static_dir = cwd_static_dir
            else:
                console.print("[bold red]Visualization assets not found.[/bold red]")
                console.print("[dim]Checked paths:[/dim]")
                console.print(f"  [dim]- {package_root / 'viz' / 'dist'}[/dim]")
                console.print(f"  [dim]- {dev_static_dir}[/dim]")
                console.print(f"  [dim]- {alt_dev_dir}[/dim]")
                console.print(f"  [dim]- {cwd_static_dir}[/dim]")
                console.print(
                    "[dim]If you installed from PyPI, upgrade after the next release "
                    "(wheels must bundle viz/dist). If you are developing from source, run:[/dim]"
                )
                console.print("  [cyan]./scripts/sync_viz_dist.sh[/cyan]")
                console.print(
                    "[dim]or[/dim] [cyan]cd website && npm ci && npm run build[/cyan] "
                    "[dim]then sync[/dim] [cyan]website/dist[/cyan] [dim]→[/dim] "
                    "[cyan]src/codegraphcontext/viz/dist[/cyan][dim].[/dim]"
                )
                console.print(
                    "\n[yellow]Falling back to the built-in offline renderer.[/yellow]"
                )
                try:
                    return _render_offline_visualization(
                        db_manager,
                        repo_path=repo_path,
                        cypher_query=cypher_query,
                        global_scope=global_scope,
                        offset=offset,
                        page_size=page_size,
                    )
                finally:
                    db_manager.close_driver()

    index_html = static_dir / "index.html"
    if not index_html.is_file():
        console.print(
            f"[bold red]Invalid visualization bundle:[/bold red] missing {index_html}"
        )
        db_manager.close_driver()
        raise SystemExit(1)

    # Construct the URL
    backend_url = f"http://localhost:{port}"
    params = {"backend": backend_url}
    if repo_path:
        params["repo_path"] = str(Path(repo_path).resolve())
    if global_scope:
        params["global_scope"] = "true"
    if cypher_query:
        params["cypher_query"] = cypher_query
    params["offset"] = str(offset)
    params["page_size"] = str(page_size)

    query_string = urllib.parse.urlencode(params)
    visualization_url = f"{backend_url}/explore?{query_string}"
    
    console.print(f"[green]Starting visualizer server on {backend_url}...[/green]")
    console.print(f"[cyan]Opening Playground UI:[/cyan] {visualization_url}")
    
    # Open browser in a separate thread/process if possible, or just before starting server
    def open_browser():
        import time
        import webbrowser
        time.sleep(1.5) # Give the server a moment to start
        webbrowser.open(visualization_url)
    
    import threading
    threading.Thread(target=open_browser, daemon=True).start()
    
    try:
        run_server(host=host, port=port, static_dir=str(static_dir))
    except Exception as e:
        console.print(f"[bold red]An error occurred while running the server:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()


def reindex_helper(path: str, context: Optional[str] = None, no_progress: bool = False):
    """Force re-index by deleting and rebuilding the repository."""
    time_start = time.time()
    requested_path = Path(path)
    _reject_external_file_symlink(requested_path)
    path_obj = requested_path.resolve()
    if not path_obj.exists():
        console.print(f"[red]Error: Path does not exist: {path_obj}[/red]")
        raise typer.Exit(code=1)

    index_cwd = path_obj if path_obj.is_dir() else path_obj.parent
    # A live embedded gateway owns its database. Resolve the same context that
    # the local path below would use before constructing any CLI DB services,
    # then submit the update to that owner when its registration is verified.
    ensure_first_run_bootstrap()
    try:
        gateway_ctx = resolve_context(context, cwd=index_cwd)
    except ContextNotFoundError:
        gateway_ctx = None
    from contextlib import nullcontext
    from codegraphcontext.core.index_generation import generation_update
    from .snapshot_commands import effective_snapshot_identity

    if gateway_ctx is not None:
        if (
            gateway_ctx.database
            and not os.getenv("CGC_RUNTIME_DB_TYPE")
            and not os.getenv("DEFAULT_DATABASE")
        ):
            os.environ["DEFAULT_DATABASE"] = gateway_ctx.database
        backend = (
            os.getenv("CGC_RUNTIME_DB_TYPE")
            or os.getenv("DEFAULT_DATABASE")
            or gateway_ctx.database
            or ""
        ).lower()
        database_path = os.getenv("CGC_RUNTIME_DB_PATH") or gateway_ctx.db_path
        gateway_identity, gateway_repository_root = effective_snapshot_identity(
            gateway_ctx, path_obj
        )
        gateway_generation_enabled = bool(
            database_path and backend in {"falkordb", "kuzudb", "ladybugdb"}
        )

        gateway_generation_update = (
            generation_update(
                database_path,
                gateway_identity,
                generation_id=(
                    f"{gateway_identity.get('storage_key', '')}:"
                    f"{gateway_identity.get('commit') or 'uncommitted'}"
                ),
            )
            if gateway_generation_enabled else nullcontext()
        )

        registration = None
        for candidate_path, candidate_backend in _embedded_gateway_candidates(
            database_path, backend
        ):
            registration = find_active_gateway(candidate_path, candidate_backend)
            if registration is not None:
                break
        if registration is not None:
            console.print(
                f"[cyan]Delegating re-index to active gateway at "
                f"{registration.endpoint}...[/cyan]"
            )
            try:
                with gateway_generation_update as generation_update_state:
                    registration.update_repository(
                        path_obj, cgcignore_path=gateway_ctx.cgcignore_path
                    )
                    if (
                        getattr(gateway_ctx, "is_local", False)
                        and gateway_identity.get("kind") in {"git", "non-git"}
                        and generation_update_state is not None
                        and path_obj.is_dir()
                        and gateway_repository_root is not None
                        and path_obj.resolve() == gateway_repository_root.resolve()
                    ):
                        generation_update_state.mark_complete()
            except GatewayUnavailableError:
                # The endpoint was live during discovery but failed before
                # the operation completed. The subsequent lease attempt
                # remains authoritative and fails safely if the owner is
                # still holding the database.
                pass
            except GatewayRequestError as exc:
                console.print(f"[bold red]Gateway update failed:[/bold red] {exc}")
                raise typer.Exit(code=1)
            else:
                console.print(f"[green]Successfully re-indexed through gateway: {path}[/green]")
                return

    services = _initialize_services(context, cwd=index_cwd)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, graph_builder, code_finder, ctx = services
    generation_identity, generation_repository_root = effective_snapshot_identity(
        ctx, path_obj
    )
    generation_db_path = getattr(db_manager, "db_path", None)
    generation_marker_enabled = bool(
        generation_db_path
        and getattr(db_manager, "get_backend_type", lambda: "")()
        in {"falkordb", "kuzudb", "ladybugdb"}
    )

    reindex_generation_update = (
        generation_update(
            generation_db_path,
            generation_identity,
            generation_id=(
                f"{generation_identity.get('storage_key', '')}:"
                f"{generation_identity.get('commit') or 'uncommitted'}"
            ),
        )
        if generation_marker_enabled else nullcontext()
    )

    # Check if already indexed
    indexed_repos = code_finder.list_indexed_repositories()
    repo_exists = any_repo_matches_path(indexed_repos, path_obj)

    console.print(f"[cyan]Re-indexing: {path_obj}[/cyan]")
    
    try:
        with reindex_generation_update as generation_update_state:
            if repo_exists:
                console.print(f"[yellow]Deleting existing index for: {path_obj}[/yellow]")
                try:
                    graph_builder.delete_repository_from_graph(str(path_obj))
                    console.print("[green]✓[/green] Deleted old index")
                except Exception as e:
                    console.print(f"[red]Error deleting old index: {e}[/red]")
                    raise typer.Exit(code=1)
            asyncio.run(_run_index_with_progress(graph_builder, path_obj, is_dependency=False, cgcignore_path=ctx.cgcignore_path, disable_progress=no_progress))
            if (
                getattr(ctx, "is_local", False)
                and generation_identity.get("kind") in {"git", "non-git"}
                and generation_update_state is not None
                and path_obj.is_dir()
                and generation_repository_root is not None
                and path_obj.resolve() == generation_repository_root.resolve()
            ):
                generation_update_state.mark_complete()
        time_end = time.time()
        elapsed = time_end - time_start
        _print_call_resolution_diagnostics(graph_builder)
        _print_index_execution_summary(graph_builder)
        console.print(f"[green]Successfully re-indexed: {path} in {elapsed:.2f} seconds[/green]")
    except Exception as e:
        console.print(f"[bold red]An error occurred during re-indexing:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        db_manager.close_driver()


def update_helper(path: str, context: Optional[str] = None, quiet: bool = False):
    """Update/refresh index for a path (alias for reindex).

    When *quiet* is True (e.g. when invoked from Git hooks with --quiet),
    Rich console output, including progress rendering, is suppressed.
    """
    if quiet:
        console.quiet = True
        try:
            reindex_helper(path, context)
        finally:
            console.quiet = False
        return
    console.print("[cyan]Updating repository index...[/cyan]")
    reindex_helper(path, context)


def clean_helper(context: Optional[str] = None):
    """Remove orphaned nodes and relationships from the database."""
    if not is_db_deletion_allowed():
        console.print(
            "[bold red]Error:[/bold red] Database cleanup is disabled. "
            "Set ALLOW_DB_DELETION=true in config to enable."
        )
        raise typer.Exit(code=1)

    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, _, _, ctx = services
    
    console.print("[cyan]🧹 Cleaning database (removing orphaned nodes)...[/cyan]")
    
    try:
        with _generation_update_for_context(db_manager, ctx):
            total_deleted = 0
            batch_size = 500

            with db_manager.get_driver().session() as session:
                # Delete nodes with no incoming relationships (true orphans).
                # Parameters (HAS_PARAMETER), import Modules (IMPORTS), etc. are kept.
                while True:
                    result = session.run("""
                        MATCH (n)
                        WHERE NOT n:Repository
                          AND NOT ()-[]->(n)
                        WITH n LIMIT $batch_size
                        DETACH DELETE n
                        RETURN count(n) as deleted
                    """, batch_size=batch_size)
                    record = result.single()
                    deleted_count = record["deleted"] if record else 0
                    total_deleted += deleted_count

                    if deleted_count == 0:
                        break

                    console.print(f"[dim]  Deleted {deleted_count} orphaned nodes (batch)...[/dim]")
            
            if total_deleted > 0:
                console.print(f"[green]✓[/green] Deleted {total_deleted} orphaned nodes total")
            else:
                console.print("[green]✓[/green] No orphaned nodes found")

        console.print("[green]✅ Database cleanup complete![/green]")
    except Exception as e:
        console.print(f"[bold red]An error occurred during cleanup:[/bold red] {e}")
    finally:
        db_manager.close_driver()


def stats_helper(path: str = None, context: Optional[str] = None):
    """Show indexing statistics for a repository or overall."""
    services = _initialize_services(context)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, _, code_finder, ctx = services
    
    try:
        if path:
            # Stats for specific repository
            path_obj = Path(path).resolve()
            # Paths are stored with forward slashes (as_posix) in the graph DB,
            # so lookups must use the same normalization on Windows too.
            repo_path_str = path_obj.as_posix()
            console.print(f"[cyan]📊 Statistics for: {path_obj}[/cyan]\n")
            
            with db_manager.get_driver().session() as session:
                # Get repository node
                repo_query = """
                MATCH (r:Repository {path: $path})
                RETURN r
                """
                result = session.run(repo_query, path=repo_path_str)
                if not result.single():
                    console.print(f"[red]Repository not found: {path_obj}[/red]")
                    return
                
                # Get stats
                # Get stats using separate queries to handle depth and avoid Cartesian products.
                # `count(x)` counts *rows*, and a variable-length CONTAINS* match
                # yields one row per distinct path to the node: a method is
                # reachable as Repo->..->File->Function and again as
                # Repo->..->File->Class->Function, a nested function three ways.
                # count(DISTINCT x) is required or the totals are inflated —
                # functions were over-reported by ~55% on CGC's own repo.
                # 1. Files
                file_query = "MATCH (r:Repository {path: $path})-[:CONTAINS*]->(f:File) RETURN count(DISTINCT f) as c"
                file_count = session.run(file_query, path=repo_path_str).single()["c"]

                # 2. Functions (including methods in classes)
                func_query = "MATCH (r:Repository {path: $path})-[:CONTAINS*]->(func:Function) RETURN count(DISTINCT func) as c"
                func_count = session.run(func_query, path=repo_path_str).single()["c"]

                # 3. Classes
                class_query = "MATCH (r:Repository {path: $path})-[:CONTAINS*]->(c:Class) RETURN count(DISTINCT c) as c"
                class_count = session.run(class_query, path=repo_path_str).single()["c"]
                
                # 4. Modules (imported) - Note: Module nodes are outside the repo structure usually, connected via IMPORTS
                # We need to traverse from files to modules
                module_query = "MATCH (r:Repository {path: $path})-[:CONTAINS*]->(f:File)-[:IMPORTS]->(m:Module) RETURN count(DISTINCT m) as c"
                module_count = session.run(module_query, path=repo_path_str).single()["c"]

                table = Table(show_header=True, header_style="bold magenta")
                table.add_column("Metric", style="cyan")
                table.add_column("Count", style="green", justify="right")
                
                table.add_row("Files", str(file_count))
                table.add_row("Functions", str(func_count))
                table.add_row("Classes", str(class_count))
                table.add_row("Imported Modules", str(module_count))
                
                console.print(table)
        else:
            # Overall stats
            console.print("[cyan]📊 Overall Database Statistics[/cyan]\n")
            
            with db_manager.get_driver().session() as session:
                # Get overall counts using separate O(1) queries
                repo_count = session.run("MATCH (r:Repository) RETURN count(r) as c").single()["c"]
                
                if repo_count > 0:
                    file_count = session.run("MATCH (f:File) RETURN count(f) as c").single()["c"]
                    func_count = session.run("MATCH (f:Function) RETURN count(f) as c").single()["c"]
                    class_count = session.run("MATCH (c:Class) RETURN count(c) as c").single()["c"]
                    module_count = session.run("MATCH (m:Module) RETURN count(m) as c").single()["c"]
                    
                    # Extended node types (PHP, Rust, Go, etc.)
                    interface_count = session.run("MATCH (i:Interface) RETURN count(i) as c").single()["c"]
                    trait_count = session.run("MATCH (t:Trait) RETURN count(t) as c").single()["c"]
                    struct_count = session.run("MATCH (s:Struct) RETURN count(s) as c").single()["c"]
                    enum_count = session.run("MATCH (e:Enum) RETURN count(e) as c").single()["c"]
                    
                    table = Table(show_header=True, header_style="bold magenta")
                    table.add_column("Metric", style="cyan")
                    table.add_column("Count", style="green", justify="right")
                    
                    table.add_row("Repositories", str(repo_count))
                    table.add_row("Files", str(file_count))
                    table.add_row("Functions", str(func_count))
                    table.add_row("Classes", str(class_count))
                    if interface_count > 0:
                        table.add_row("Interfaces", str(interface_count))
                    if trait_count > 0:
                        table.add_row("Traits", str(trait_count))
                    if struct_count > 0:
                        table.add_row("Structs", str(struct_count))
                    if enum_count > 0:
                        table.add_row("Enums", str(enum_count))
                    table.add_row("Modules", str(module_count))
                    
                    console.print(table)
                else:
                    console.print("[yellow]No data indexed yet.[/yellow]")
                    
    except Exception as e:
        console.print(f"[bold red]An error occurred:[/bold red] {e}")
    finally:
        db_manager.close_driver()


def watch_helper(
    path: str,
    context: Optional[str] = None,
    use_polling: Optional[bool] = None,
    sync_on_start: bool = False,
):
    """Watch a directory for changes and auto-update the graph (blocking mode)."""
    import logging
    from ..core.watcher import CodeWatcher
    
    # Suppress verbose watchdog DEBUG logs
    logging.getLogger('watchdog').setLevel(logging.WARNING)
    logging.getLogger('watchdog.observers').setLevel(logging.WARNING)
    logging.getLogger('watchdog.observers.inotify_buffer').setLevel(logging.WARNING)
    
    path_obj = Path(path).resolve()
    services = _initialize_services(context, cwd=path_obj)
    if not all(services[:3]):
        _fail_services_init()

    db_manager, graph_builder, code_finder, ctx = services
    if not path_obj.exists():
        console.print(f"[red]Error: Path does not exist: {path_obj}[/red]")
        db_manager.close_driver()
        raise typer.Exit(code=1)
    
    if not path_obj.is_dir():
        console.print(f"[red]Error: Path must be a directory: {path_obj}[/red]")
        db_manager.close_driver()
        raise typer.Exit(code=1)

    console.print(f"[bold cyan]🔍 Watching {path_obj} for changes...[/bold cyan]")
    
    # Check if already indexed — use File node count as a robust fallback so a
    # transient empty result from list_indexed_repositories never triggers a
    # destructive full rescan of an already-populated graph.
    indexed_repos = code_finder.list_indexed_repositories()
    is_indexed = any_repo_matches_path(indexed_repos, path_obj)
    if not is_indexed:
        # Fallback: count File nodes whose path starts with this repo's path.
        # If > 100 exist, the repo is clearly already indexed — skip the scan.
        try:
            with code_finder.driver.session() as _s:
                _r = _s.run(
                    "MATCH (n:File) WHERE n.path STARTS WITH $p RETURN count(n) AS c",
                    p=path_obj.as_posix() + "/"
                )
                _count = _r.single()["c"]
            if _count > 100:
                info_logger(
                    f"[watch] list_indexed_repositories returned no match for {path_obj} "
                    f"but {_count} File nodes exist — treating as already indexed."
                )
                is_indexed = True
        except Exception as _e:
            warning_logger(f"[watch] Fallback indexed check failed: {_e}")
    
    # Create watcher instance
    job_manager = JobManager()
    watcher = CodeWatcher(graph_builder, job_manager, use_polling=use_polling)
    from .snapshot_commands import effective_snapshot_identity
    from ..core.index_generation import async_generation_update

    watch_identity, _ = effective_snapshot_identity(ctx, path_obj)
    watch_db_path = getattr(db_manager, "db_path", None)
    watch_generation_enabled = bool(
        watch_db_path
        and getattr(db_manager, "get_backend_type", lambda: "")()
        in {"falkordb", "kuzudb", "ladybugdb"}
    )
    watch_generation_kwargs = (
        {
            "generation_db_path": watch_db_path,
            "generation_identity": watch_identity,
        }
        if watch_generation_enabled
        else {}
    )

    watch_generation_update = (
        async_generation_update(watch_db_path, watch_identity)
        if watch_generation_enabled else None
    )
    
    try:
        # Start the observer thread
        watcher.start()
        
        # Add the directory to watch
        if is_indexed:
            if sync_on_start:
                console.print("[green]✓[/green] Already indexed. Synchronizing current files...")
            else:
                console.print("[green]✓[/green] Already indexed. Watching for future changes only.")
                console.print(
                    "[dim]Use 'cgc index --force' or 'cgc watch --sync-on-start' to reconcile existing changes.[/dim]"
                )
            watcher.watch_directory(
                str(path_obj),
                perform_initial_scan=False,
                sync_on_start=sync_on_start,
                cgcignore_path=ctx.cgcignore_path,
                **watch_generation_kwargs,
            )
        else:
            console.print("[yellow]⚠[/yellow]  Not indexed yet. Performing initial scan...")
            
            # Index the repository first (like MCP does)
            async def do_index():
                if watch_generation_update is None:
                    await graph_builder.build_graph_from_path_async(
                        path_obj,
                        is_dependency=False,
                        cgcignore_path=ctx.cgcignore_path,
                    )
                else:
                    async with watch_generation_update:
                        await graph_builder.build_graph_from_path_async(
                            path_obj,
                            is_dependency=False,
                            cgcignore_path=ctx.cgcignore_path,
                        )
            
            asyncio.run(do_index())
            console.print("[green]✓[/green] Initial scan complete")
            
            # Now start watching (without another scan)
            watcher.watch_directory(
                str(path_obj),
                perform_initial_scan=False,
                cgcignore_path=ctx.cgcignore_path,
                **watch_generation_kwargs,
            )
        
        console.print("[bold green]👀 Monitoring for file changes...[/bold green] (Press Ctrl+C to stop)")
        console.print("[dim]💡 Tip: Open a new terminal window to continue working[/dim]\n")
        
        # Block here and keep the watcher running
        import threading
        stop_event = threading.Event()
        
        try:
            stop_event.wait()  # Wait indefinitely until interrupted
        except KeyboardInterrupt:
            console.print("\n[yellow]🛑 Stopping watcher...[/yellow]")
            
    except KeyboardInterrupt:
        console.print("\n[yellow]🛑 Stopping watcher...[/yellow]")
    except Exception as e:
        console.print(f"[bold red]An error occurred:[/bold red] {e}")
    finally:
        watcher.stop()
        db_manager.close_driver()
        console.print("[green]✓[/green] Watcher stopped.")



def unwatch_helper(path: str):
    """Report that unwatching is not available from the CLI.

    This command touches no watcher state at all — it never has. It used to
    print a note, echo the requested path back as "Path specified:" (which
    reads like confirmation that something happened, even for a path that was
    never watched and does not exist) and exit 0.
    """
    console.print(
        "[bold red]Error:[/bold red] 'cgc unwatch' is not supported from the CLI — "
        "it cannot reach a watcher started in another process."
    )
    console.print("[dim]For CLI watch mode, press Ctrl+C in the terminal running 'cgc watch'.[/dim]")
    console.print(
        "[dim]For MCP mode, call the 'unwatch_directory' tool from your IDE.[/dim]"
    )
    raise typer.Exit(code=1)


def list_watching_helper():
    """Report that listing watched paths is not available from the CLI.

    Same as unwatch: no watcher state is consulted, so exiting 0 advertised a
    successful listing of nothing.
    """
    console.print(
        "[bold red]Error:[/bold red] 'cgc watching' is not supported from the CLI — "
        "it cannot reach a watcher started in another process."
    )
    console.print("[dim]For CLI watch mode, check the terminal where you ran 'cgc watch'.[/dim]")
    console.print("[dim]For MCP mode: start 'cgc mcp start', then call the "
                  "'list_watched_paths' tool from your IDE.[/dim]")
    raise typer.Exit(code=1)


def setup_scip_helper() -> None:
    """Diagnostic and setup helper for SCIP indexers."""
    from ..tools.scip_indexer import EXTENSION_TO_SCIP
    import shutil
    
    console.print("[bold cyan]🔍 Checking SCIP Indexer Availability...[/bold cyan]\n")
    
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Language", style="cyan")
    table.add_column("Binary", style="yellow")
    table.add_column("Status", style="green")
    table.add_column("Install Hint", style="dim")
    
    langs = {}
    for ext, (lang, binary, hint, docker) in EXTENSION_TO_SCIP.items():
        if lang not in langs:
            langs[lang] = (binary, hint, docker)
            
    for lang, (binary, hint, docker) in sorted(langs.items()):
        is_installed = shutil.which(binary) is not None
        status = "[green]✓ Installed[/green]" if is_installed else "[red]✗ Not Found[/red]"
        table.add_row(lang, binary, status, hint)
        
    console.print(table)
    
    # Check Docker
    has_docker = shutil.which("docker") is not None
    if has_docker:
        console.print("\n[green]✓ Docker is available (Auto-fallback enabled)[/green]")
    else:
        console.print("\n[yellow]⚠ Docker not found. Local binaries are required for SCIP.[/yellow]")

    console.print("\n[dim]To enable SCIP indexing, run:[/dim]")
    console.print("[bold white]cgc config set SCIP_INDEXER true[/bold white]")
