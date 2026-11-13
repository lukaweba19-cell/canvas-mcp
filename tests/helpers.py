"""Small test helpers for calling tools through the real MCP layer."""

from __future__ import annotations

import re
from typing import Any

from fastmcp import FastMCP

_TOKEN_RE = re.compile(r"confirmation_token='([^']+)'")


def text_of(result: Any) -> str:
    """Join a FastMCP ToolResult's text content."""
    content = getattr(result, "content", None)
    if isinstance(content, list):
        return "\n".join(str(getattr(part, "text", "")) for part in content)
    return str(result)


async def call(mcp: FastMCP, tool: str, **arguments: Any) -> str:
    """Invoke a registered tool exactly as a client would."""
    return text_of(await mcp.call_tool(tool, arguments))


def token_in(preview: str) -> str:
    """Extract the confirmation token a preview offered."""
    match = _TOKEN_RE.search(preview)
    assert match, f"preview contained no confirmation token:\n{preview}"
    return match.group(1)
