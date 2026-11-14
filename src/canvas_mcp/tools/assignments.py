"""Assignment-related MCP tools for Canvas API."""


from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import fetch_all_paginated_results, make_canvas_request
from ..core.dates import format_date
from ..core.untrusted_content import (
    fence_untrusted,
    fence_untrusted_inline,
)
from ..core.validation import validate_params
from ..core.write_confirmation import (
    ConfirmationGuard,
)

_DELETE_ASSIGNMENT_GUARD = ConfirmationGuard(nothing_done="Nothing was deleted.")


def register_shared_assignment_tools(mcp: FastMCP) -> None:
    """Register assignment tools accessible to both students and educators."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_assignments(course_identifier: str | int) -> str:
        """List assignments for a specific course.

        Args:
            course_identifier: Course code or Canvas ID
        """
        course_id = await get_course_id(course_identifier)

        params = {
            "per_page": 100,
            "include[]": ["all_dates", "submission"]
        }

        all_assignments = await fetch_all_paginated_results(f"/courses/{course_id}/assignments", params)

        if isinstance(all_assignments, dict) and "error" in all_assignments:
            return f"Error fetching assignments: {all_assignments['error']}"

        if not all_assignments:
            return f"No assignments found for course {course_identifier}."

        assignments_info = []
        for assignment in all_assignments:
            assignment_id = assignment.get("id")
            name = assignment.get("name", "Unnamed assignment")
            due_at = assignment.get("due_at", "No due date")
            points = assignment.get("points_possible", 0)

            assignments_info.append(
                f"ID: {assignment_id}\n"
                f"Name: {fence_untrusted_inline(name, 'assignment name')}\n"
                f"Due: {due_at}\nPoints: {points}\n"
            )

        course_display = await get_course_code(course_id) or course_identifier
        return f"Assignments for Course {course_display}:\n\n" + "\n".join(assignments_info)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_assignment_details(course_identifier: str | int, assignment_id: str | int) -> str:
        """Get detailed information about a specific assignment.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Canvas assignment ID
        """
        course_id = await get_course_id(course_identifier)

        assignment_id_str = str(assignment_id)

        response = await make_canvas_request(
            "get", f"/courses/{course_id}/assignments/{assignment_id_str}"
        )

        if "error" in response:
            return f"Error fetching assignment details: {response['error']}"

        details = [
            f"Name: {fence_untrusted_inline(response.get('name', 'N/A'), 'assignment name')}",
            "Description:\n"
            + fence_untrusted(response.get('description') or 'N/A', 'assignment description'),
            f"Due Date: {format_date(response.get('due_at'))}",
            f"Points Possible: {response.get('points_possible', 'N/A')}",
            f"Submission Types: {', '.join(response.get('submission_types', ['N/A']))}",
            f"Published: {response.get('published', False)}",
            f"Locked: {response.get('locked_for_user', False)}"
        ]

        course_display = await get_course_code(course_id) or course_identifier
        return f"Assignment Details for ID {assignment_id} in course {course_display}:\n\n" + "\n".join(details)


