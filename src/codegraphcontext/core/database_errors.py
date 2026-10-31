"""Database exceptions that do not require a backend driver to import."""

from typing import Optional


class Neo4jConnectionError(Exception):
    """Raised when Neo4j cannot be reached or authenticated with guidance."""

    def __init__(
        self,
        message: str,
        reason: Optional[str] = None,
        source: Optional[str] = None,
        suggestions: Optional[list] = None,
    ):
        super().__init__(message)
        self.reason = reason or message
        self.source = source or "unknown"
        self.suggestions = suggestions or []

    def __str__(self) -> str:
        base = f"Neo4j connection failed (source: {self.source}). Reason: {self.reason}"
        if not self.suggestions:
            return base
        suggestion_block = "\n" + "\n".join(f"  - {suggestion}" for suggestion in self.suggestions)
        return f"{base}\nSuggested fixes:{suggestion_block}"
