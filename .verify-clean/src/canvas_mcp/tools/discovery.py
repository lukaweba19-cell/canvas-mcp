"""
Tool discovery for the Canvas student MCP server.

Lets the model search the live registry of student tools by keyword so it can
find the right one without being handed every description at once.
"""

import json
from typing import Any, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.validation import validate_params

DetailLevel = Literal["names", "signatures", "full"]

# Bumped from the unversioned (schema_version-less) shape to 2 when the
# response gained mcp_tools/code_execution_api sections and the top-level
# "tools" key was removed (issue #281). Scripted consumers should branch on
# this field rather than assume the old flat {query, detail_level, count,
# tools: [...]} shape.
_SCHEMA_VERSION = 2

# Cap on how much of an MCP tool's description is echoed back at
# detail_level="full", so a query that matches many tools can't blow up
# the response size with full multi-paragraph docstrings.
_MCP_FULL_DESCRIPTION_CHARS = 400

# Same idea for "signatures" mode: a tool's first docstring line is usually
# short, but nothing enforces that — cap it so one verbose tool can't
# dominate the response.
_MCP_SIGNATURE_DESCRIPTION_CHARS = 200

_TRUNCATION_SENTINEL = "... [truncated]"


def _cap(text: str, max_len: int) -> str:
    """Truncate text to at most max_len characters total, sentinel included."""
    if len(text) <= max_len:
        return text
    keep = max(max_len - len(_TRUNCATION_SENTINEL), 0)
    return text[:keep] + _TRUNCATION_SENTINEL


async def _search_mcp_tools(
    mcp: FastMCP, query_lower: str, detail_level: DetailLevel
) -> tuple[list[str | dict[str, Any]], int]:
    """Search the live registry of registered MCP tools by name/description.

    Queried at call time (not registration time) so results reflect
    whichever tools are actually registered for this process — feature
    flags like EXECUTE_TYPESCRIPT_ENABLED or STUDENT_WRITE_TOOLS change
    the live tool set. Skips FastMCP's middleware chain (run_middleware=False)
    since this is a metadata listing, not a tool invocation — re-running
    ~99 tools' middleware on every search call would be pure overhead.
    """
    tools = await mcp.list_tools(run_middleware=False)

    matches: list[str | dict[str, Any]] = []
    for tool in tools:
        name = tool.name
        description = tool.description or ""
        if query_lower and query_lower not in name.lower() and query_lower not in description.lower():
            continue

        if detail_level == "names":
            matches.append(name)
        else:
            first_line = description.strip().splitlines()[0] if description.strip() else ""
            first_line = _cap(first_line, _MCP_SIGNATURE_DESCRIPTION_CHARS)
            entry: dict[str, Any] = {"name": name, "description": first_line}
            if detail_level == "full" and description.strip():
                entry["description"] = _cap(description.strip(), _MCP_FULL_DESCRIPTION_CHARS)
            matches.append(entry)

    return matches, len(tools)


def register_discovery_tools(mcp: FastMCP) -> None:
    """Register tool discovery tools."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def search_canvas_tools(
        query: str = "",
        detail_level: DetailLevel = "signatures"
    ) -> str:
        """
        Search the available student Canvas tools by keyword
        (e.g. "assignment", "grades", "study", "files").

        Args:
            query: Search term to filter tools (empty = all)
            detail_level: "names" (names only), "signatures" (recommended), or "full" (full descriptions)
        """
        try:
            query_lower = query.lower()

            mcp_matches, mcp_tool_count = await _search_mcp_tools(mcp, query_lower, detail_level)

            total_count = len(mcp_matches)

            if total_count == 0:
                return json.dumps({
                    "schema_version": _SCHEMA_VERSION,
                    "message": f"No tools found matching '{query}'",
                    "suggestion": "Try a different search term or use empty string to see all tools",
                    "mcp_tools_searched": mcp_tool_count,
                }, indent=2)

            result: dict[str, Any] = {
                "schema_version": _SCHEMA_VERSION,
                "query": query,
                "detail_level": detail_level,
                "count": total_count,
                "tools": mcp_matches,
            }

            return json.dumps(result, indent=2)

        except Exception as e:
            return json.dumps({
                "error": str(e),
                "type": type(e).__name__
            }, indent=2)
