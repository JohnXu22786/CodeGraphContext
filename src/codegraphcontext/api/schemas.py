# src/codegraphcontext/api/schemas.py
from pydantic import BaseModel, Field, StrictInt, field_validator
from typing import Dict, Any, List, Optional

class IndexRequest(BaseModel):
    path: str = Field(..., description="Local path to the repository or file to index")
    repo_name: Optional[str] = Field(None, description="Optional name for the repository")
    branch: str = "main"
    force: bool = False
    timeout_seconds: Optional[float] = Field(
        None, gt=0, description="Optional indexing deadline in seconds"
    )

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def timeout_seconds_must_not_be_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("timeout_seconds must be a number, not a boolean")
        return value

class LocalUpdateRequest(BaseModel):
    """Loopback-only update submitted by a CLI sharing this gateway's DB."""
    path: str
    cgcignore_path: Optional[str] = None
    timeout_seconds: Optional[float] = Field(
        None, gt=0, description="Optional indexing deadline in seconds"
    )

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def timeout_seconds_must_not_be_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("timeout_seconds must be a number, not a boolean")
        return value

class QueryRequest(BaseModel):
    query: str = Field(..., description="Cypher query to execute")
    params: Dict[str, Any] = Field(default_factory=dict, description="Parameters for the query")
    repo_path: Optional[str] = Field(
        None, description="Optional repository root for repository-scoped execution"
    )
    global_scope: bool = Field(
        False, description="Explicitly query across repositories"
    )
    offset: StrictInt = Field(0, ge=0, le=99999, description="Zero-based row offset")
    page_size: Optional[StrictInt] = Field(
        None, ge=1, description="Maximum rows to return in this page"
    )

class SearchRequest(BaseModel):
    query: str = Field(..., description="Search query")
    top_k: int = 10

class ToolCallRequest(BaseModel):
    name: str = Field(..., description="Name of the MCP tool to call")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Arguments for the tool")

class ApiResponse(BaseModel):
    status: str
    message: Optional[str] = None
    data: Optional[Any] = None
    error: Optional[str] = None
