"""Module-related MCP tools for Canvas API.

Provides tools for creating, updating, and managing Canvas course modules
and module items. Modules are the primary content organization system in Canvas.
"""


from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import fetch_all_paginated_results
from ..core.dates import format_date
from ..core.untrusted_content import (
    fence_untrusted_inline,
)
from ..core.validation import validate_params
from ..core.write_confirmation import (
    ConfirmationGuard,
)

_DELETE_MODULE_GUARD = ConfirmationGuard(nothing_done="Nothing was deleted.")
_DELETE_MODULE_ITEM_GUARD = ConfirmationGuard(nothing_done="Nothing was deleted.")


def register_shared_module_tools(mcp: FastMCP) -> None:
    """Register module tools accessible to both students and educators."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_modules(
        course_identifier: str | int,
        include_items: bool = False,
        search_term: str | None = None
    ) -> str:
        """List all modules in a course.

        Args:
            course_identifier: Course code or Canvas ID
            include_items: Include summary of items in each module
            search_term: Filter modules by name
        """
        course_id = await get_course_id(course_identifier)

        params: dict[str, Any] = {"per_page": 100}
        if include_items:
            params["include[]"] = ["items"]
        if search_term:
            params["search_term"] = search_term

        modules = await fetch_all_paginated_results(
            f"/courses/{course_id}/modules", params
        )

        if isinstance(modules, dict) and "error" in modules:
            return f"Error fetching modules: {modules['error']}"

        if not modules:
            return "No modules found in course."

        course_display = await get_course_code(course_id) or course_identifier
        result = f"Modules in {course_display}:\n\n"

        for module in modules:
            module_id = module.get("id")
            name = module.get("name", "Unnamed")
            position = module.get("position", 0)
            state = module.get("state", "unknown")
            published = module.get("published", False)
            items_count = module.get("items_count", 0)
            unlock_at = module.get("unlock_at")
            require_sequential = module.get("require_sequential_progress", False)
            prerequisite_ids = module.get("prerequisite_module_ids", [])

            # Module names and item titles are instructor-authored (issue 239).
            result += f"**{fence_untrusted_inline(name, 'module name')}**\n"
            result += f"  ID: {module_id}\n"
            result += f"  Position: {position}\n"
            result += f"  Status: {state} | Published: {'Yes' if published else 'No'}\n"
            result += f"  Items: {items_count}\n"

            if unlock_at:
                result += f"  Unlocks: {format_date(unlock_at)}\n"
            if require_sequential:
                result += "  Sequential Progress: Required\n"
            if prerequisite_ids:
                result += f"  Prerequisites: {prerequisite_ids}\n"

            # Include item summary if requested
            if include_items and "items" in module:
                items = module.get("items", [])
                if items:
                    result += "  Items:\n"
                    for item in items[:5]:  # Show first 5 items
                        item_title = item.get("title", "Untitled")
                        item_type = item.get("type", "Unknown")
                        result += f"    - {fence_untrusted_inline(item_title, 'module item title')} ({item_type})\n"
                    if len(items) > 5:
                        result += f"    ... and {len(items) - 5} more items\n"

            result += "\n"

        return result

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_structure(
        course_identifier: str | int,
        include_unpublished: bool = True
    ) -> str:
        """Get the full module and item structure for a course in a single call.

        Args:
            course_identifier: Course code or Canvas ID
            include_unpublished: Include unpublished modules and items (default: True)
        """
        import json

        course_id = await get_course_id(course_identifier)

        params = {"per_page": 100, "include[]": ["items"]}

        modules = await fetch_all_paginated_results(
            f"/courses/{course_id}/modules", params
        )

        if isinstance(modules, dict) and "error" in modules:
            return json.dumps({"error": f"Error fetching course structure: {modules['error']}"})

        # Build structured output
        structured_modules = []
        total_items = 0
        unpublished_modules = 0
        unpublished_items = 0
        empty_modules = 0
        item_types: dict[str, int] = {}

        for module in modules:
            module_published = module.get("published", False)

            # Skip unpublished modules if not requested
            if not include_unpublished and not module_published:
                continue

            if not module_published:
                unpublished_modules += 1

            raw_items = module.get("items", [])
            filtered_items = []

            for item in raw_items:
                item_published = item.get("published", True)

                # Skip unpublished items if not requested
                if not include_unpublished and not item_published:
                    continue

                if not item_published:
                    unpublished_items += 1

                item_type = item.get("type", "Unknown")
                item_types[item_type] = item_types.get(item_type, 0) + 1
                total_items += 1

                filtered_items.append({
                    "id": item.get("id"),
                    "type": item_type,
                    # Author-controlled titles fenced even in the JSON payload
                    # (issue 239).
                    "title": fence_untrusted_inline(item.get("title", "Untitled"), "module item title"),
                    "published": item_published,
                    "position": item.get("position"),
                    "content_id": item.get("content_id"),
                    "page_url": item.get("page_url"),
                    "external_url": item.get("external_url"),
                    "indent": item.get("indent", 0),
                })

            # Count empty modules (published modules with 0 items after filtering)
            if module_published and len(filtered_items) == 0:
                empty_modules += 1

            structured_modules.append({
                "id": module.get("id"),
                "name": fence_untrusted_inline(module.get("name", "Unnamed"), "module name"),
                "position": module.get("position"),
                "published": module_published,
                "unlock_at": format_date(module.get("unlock_at")) if module.get("unlock_at") else None,
                "require_sequential_progress": module.get("require_sequential_progress", False),
                "prerequisite_module_ids": module.get("prerequisite_module_ids", []),
                "items_count": len(filtered_items),
                "items": filtered_items,
            })

        result = {
            "course_id": str(course_id),
            "modules": structured_modules,
            "summary": {
                "total_modules": len(structured_modules),
                "total_items": total_items,
                "unpublished_modules": unpublished_modules,
                "unpublished_items": unpublished_items,
                "empty_modules": empty_modules,
                "item_types": item_types,
            },
        }

        return json.dumps(result)


