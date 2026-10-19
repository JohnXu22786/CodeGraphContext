"""Utility functions for database transactions and backend detection."""

from typing import Any


def get_backend_type(driver: Any, db_manager: Any = None) -> str:
    """Determine the database backend type dynamically."""
    if db_manager is not None and hasattr(db_manager, "get_backend_type"):
        return getattr(db_manager, "get_backend_type", None)()
    return getattr(driver, "get_backend_type", lambda: "neo4j")()


def execute_write_operation(driver: Any, backend: str, work_fn: callable) -> Any:
    """Execute a database write operation with full transaction and retry support.
    
    For Neo4j/Nornic, utilizes managed transactions (execute_write or write_transaction)
    to automatically retry on transient errors and group all operations in a single block.
    For other backends (e.g. KuzuDB, FalkorDB), passes through the standard session.
    """
    def _execute_with_session(session):
        if backend in ("neo4j", "nornic"):
            # Prefer execute_write (Neo4j driver 5+), fallback to write_transaction (<5)
            if hasattr(session, "execute_write"):
                return session.execute_write(work_fn)
            elif hasattr(session, "write_transaction"):
                return session.write_transaction(work_fn)
            else:
                return work_fn(session)
        return work_fn(session)

    # CodeFinder holds this same lock from its readiness probe through the
    # query. Locking each managed write callback prevents a Neo4j READ COMMITTED
    # transaction from observing a new building marker between those reads.
    write_lock = getattr(driver, "_write_lock", None) or getattr(driver, "_query_lock", None)

    # Sessions can reserve pooled connections when opened. Acquire the session
    # before the shared lock so readers that use the same session-before-lock
    # order cannot hold every connection while a lock holder waits for one.
    with driver.session() as session:
        if write_lock is None:
            return _execute_with_session(session)
        with write_lock:
            return _execute_with_session(session)


def execute_read_operation(driver: Any, backend: str, work_fn: callable) -> Any:
    """Execute a database read operation utilizing managed read transactions where supported."""
    if backend in ("neo4j", "nornic"):
        with driver.session() as session:
            if hasattr(session, "execute_read"):
                return session.execute_read(work_fn)
            elif hasattr(session, "read_transaction"):
                return session.read_transaction(work_fn)
            else:
                return work_fn(session)
    else:
        with driver.session() as session:
            return work_fn(session)
