"""Shared tool dispatch and result formatting for MCP transports."""
from dataclasses import dataclass
import json
import sys
from typing import Any

from .utils.gcf_encoder import encode_response

MCP_SERVER_VERSION = "0.1.0"
WORKSPACE_PREFIX = "/workspace/"
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class MCPToolResult:
    """Transport-neutral outcome of a single MCP tool invocation."""

    payload: Any
    error_kind: str | None = None

    @property
    def is_error(self) -> bool:
        return self.error_kind is not None

    @property
    def error_message(self) -> str:
        if isinstance(self.payload, dict) and "error" in self.payload:
            return str(self.payload["error"])
        return str(self.payload)


async def dispatch_tool_call(server, tool_name: Any, arguments: Any) -> MCPToolResult:
    """Validate the call shape and run the same server dispatcher for every transport.

    Handler exceptions intentionally propagate so each transport can keep its
    established JSON-RPC internal-error handling. Returned tool errors share
    one payload and are wrapped in the transport-specific tool-error envelope.
    """
    if not isinstance(tool_name, str):
        return MCPToolResult(
            payload={"error": "Tool name must be a string."},
            error_kind="invalid_params",
        )
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return MCPToolResult(
            payload={"error": "Tool arguments must be an object."},
            error_kind="invalid_params",
        )

    payload = await server.handle_tool_call(tool_name, arguments)
    payload = strip_workspace_prefix(payload)
    if isinstance(payload, dict) and "error" in payload:
        return MCPToolResult(payload=payload, error_kind="tool")
    return MCPToolResult(payload=payload)


def _is_path_key(key: str) -> bool:
    """Check whether a dict key represents a file path field."""
    bare = key.rsplit(".", 1)[-1] if "." in key else key
    return bare == "path" or bare.endswith("_path")


def _strip_path_value(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(WORKSPACE_PREFIX):
        return value[len(WORKSPACE_PREFIX):]
    return value


def strip_workspace_prefix(obj: Any) -> Any:
    """Strip the MCP workspace prefix recursively from path-valued fields."""
    if isinstance(obj, dict):
        return {
            key: _strip_path_value(value) if _is_path_key(key) else strip_workspace_prefix(value)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [strip_workspace_prefix(item) for item in obj]
    return obj


def _effective_char_limit(max_tokens: int, max_chars_direct: int) -> int:
    """Return the stricter active character limit, or zero when unlimited."""
    candidates = [value for value in (max_tokens * _CHARS_PER_TOKEN, max_chars_direct) if value > 0]
    return min(candidates) if candidates else 0


def _limit_source_label(max_tokens: int, max_chars_direct: int, effective: int) -> str:
    token_chars = max_tokens * _CHARS_PER_TOKEN
    both_active = token_chars > 0 and max_chars_direct > 0
    if both_active:
        winner = "MAX_TOOL_RESPONSE_TOKENS" if token_chars <= max_chars_direct else "MAX_PROMPT_CHARS"
        return f"{winner} ({effective} chars)"
    if token_chars > 0:
        return f"MAX_TOOL_RESPONSE_TOKENS ({max_tokens} tokens → {effective} chars)"
    return f"MAX_PROMPT_CHARS ({effective} chars)"


def apply_response_token_limit(tool_name: str, text: str) -> str:
    """Apply configured MCP response limits after the shared serialization."""
    from .cli.config_manager import get_config_value

    raw_tokens = get_config_value("MAX_TOOL_RESPONSE_TOKENS") or "0"
    try:
        max_tokens = int(raw_tokens)
    except ValueError:
        max_tokens = 0

    raw_chars = get_config_value("MAX_PROMPT_CHARS") or "0"
    try:
        max_chars_direct = int(raw_chars)
    except ValueError:
        max_chars_direct = 0

    max_chars = _effective_char_limit(max_tokens, max_chars_direct)
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    label = _limit_source_label(max_tokens, max_chars_direct, max_chars)
    notice = (
        f"[CGC] Response truncated: output exceeded {label} "
        f"for tool '{tool_name}'. "
        "Increase the limit or narrow your query for full results."
    )
    print(
        f"[CGC WARNING] Truncation fired | tool={tool_name} | "
        f"original_chars={len(text)} | truncated_chars={max_chars} | "
        f"limit={label}",
        file=sys.stderr,
        flush=True,
    )

    budget = max(0, max_chars - 200)
    try:
        payload = json.loads(text)
        preview = json.dumps(payload, indent=2)
        if len(preview) <= budget:
            return preview
        return json.dumps(
            {"truncated": True, "preview": preview[:budget], "notice": notice},
            indent=2,
        )
    except json.JSONDecodeError:
        return json.dumps(
            {"truncated": True, "preview": text[:budget], "notice": notice},
            indent=2,
        )


def encode_tool_payload(tool_name: str, payload: Any) -> str:
    """Encode a tool payload once for both stdio and Streamable HTTP."""
    return apply_response_token_limit(tool_name, encode_response(payload))
