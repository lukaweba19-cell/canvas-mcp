"""Deeper student views: assignment brief, grade maths, week plan, feedback.

Each of these replaces several round trips the student would otherwise have to
ask for one at a time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core import extract
from ..core import rubric as rubric_core
from ..core.cache import get_course_code, get_course_id
from ..core.client import fetch_all_paginated_results, make_canvas_request
from ..core.dates import format_date, parse_date
from ..core.untrusted_content import fence_untrusted, fence_untrusted_inline
from ..core.validation import coerce_canvas_id, validate_params
from .materials import readiness_lines


def _html(value: str | None) -> str:
    return extract.html_to_text(value or "")


def register_student_insight_tools(mcp: FastMCP) -> None:
    """Register the deeper student views."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_assignment_brief(
        course_identifier: str | int,
        assignment_id: str | int,
    ) -> str:
        """Everything needed to do one assignment, in a single call.

        Prompt, rubric, submission rules, attempts, your current state and any
        feedback — instead of five separate lookups.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Canvas assignment ID
        """
        validated = coerce_canvas_id(assignment_id)
        if validated is None:
            return "Error: assignment_id must be a numeric Canvas assignment ID."
        assignment_id = validated

        course_id = await get_course_id(course_identifier)
        course_display = await get_course_code(course_id) or course_identifier

        assignment = await make_canvas_request(
            "get",
            f"/courses/{course_id}/assignments/{assignment_id}",
            params={"include[]": ["rubric"]},
        )
        if isinstance(assignment, dict) and "error" in assignment:
            return f"Error fetching assignment: {assignment['error']}"

        submission = await make_canvas_request(
            "get",
            f"/courses/{course_id}/assignments/{assignment_id}/submissions/self",
            params={"include[]": ["submission_comments", "rubric_assessment"]},
        )
        if not isinstance(submission, dict) or "error" in submission:
            submission = {}

        lines = [
            f"Assignment: {fence_untrusted_inline(assignment.get('name', 'Untitled'), 'assignment name')}",
            f"Course: {course_display} | ID: {assignment_id}",
            f"Points: {assignment.get('points_possible', 'n/a')}",
            f"Due: {format_date(assignment.get('due_at'))}",
            f"Available: {format_date(assignment.get('unlock_at'))} → {format_date(assignment.get('lock_at'))}",
            f"Submission types: {', '.join(assignment.get('submission_types') or []) or 'none'}",
            f"Allowed files: {', '.join(assignment.get('allowed_extensions') or []) or 'any'}",
            f"Attempts: {submission.get('attempt', 0)} used, "
            f"{assignment.get('allowed_attempts', 'unlimited')} allowed",
            f"Your state: {submission.get('workflow_state', 'unsubmitted')}"
            + (f" (submitted {format_date(submission.get('submitted_at'))})" if submission.get("submitted_at") else ""),
            f"Grade: {submission.get('grade', 'not graded')}",
        ]
        if assignment.get("group_category_id"):
            lines.append("⚠️  Group assignment — agent-assisted submission is refused by design.")

        lines.append("\nPrompt:\n" + fence_untrusted(_html(assignment.get("description")) or "(none)", "assignment prompt"))

        attachments = assignment.get("attachments") or []
        if attachments:
            lines.append(
                f"\nReadable material — {len(attachments)} attachment(s). Status comes "
                "from the extracted text, not from the file type:"
            )
            lines.extend(
                readiness_lines(course_id, course_display, attachments, limit=8)
            )
        else:
            lines.append(
                "\nReadable material: this assignment has no attachments of its own. "
                "get_course_content_map lists the course files and whether their text "
                "can actually be read."
            )

        parsed = rubric_core.from_assignment(assignment)
        if parsed.criteria:
            rubric_core.save(parsed, course_id, assignment_id, assignment.get("name"))
            lines.append(
                f"\nRubric ({parsed.source}, {len(parsed.criteria)} criteria, "
                f"{parsed.total_points:g} points) — also saved to the assignment workspace:"
            )
            for criterion in parsed.criteria:
                points = (
                    f" — {criterion.points:g} pts"
                    if criterion.points is not None
                    else ""
                )
                lines.append(
                    f"- {fence_untrusted_inline(criterion.plain(), 'rubric criterion')}{points}"
                )
                for rating in criterion.ratings:
                    level = (
                        f"{rating.points:g} pts: " if rating.points is not None else ""
                    )
                    lines.append(
                        f"    - {level}"
                        f"{fence_untrusted_inline(rating.description, 'rubric rating')}"
                    )
        else:
            lines.append(
                "\nNo rubric: Canvas has none attached and none could be parsed from the "
                "prompt. Ask the instructor what the marks are for, or check the syllabus."
            )

        comments = submission.get("submission_comments") or []
        if comments:
            lines.append(f"\nFeedback ({len(comments)} comment(s)):")
            for comment in comments[:5]:
                lines.append(
                    "- "
                    + fence_untrusted(
                        comment.get("comment", ""), "submission comment"
                    )
                    + (f"  [by {fence_untrusted_inline(comment.get('author_name'), 'comment author')}]"
                       if comment.get("author_name") else "")
                )

        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_grade_breakdown(
        course_identifier: str | int,
        target_grade: float | None = None,
    ) -> str:
        """Your grade by assignment group, and what a target grade would need.

        Args:
            course_identifier: Course code or Canvas ID
            target_grade: Optional target percentage (e.g. 90) to see what remaining work must average
        """
        course_id = await get_course_id(course_identifier)
        course_display = await get_course_code(course_id) or course_identifier

        groups = await fetch_all_paginated_results(
            f"/courses/{course_id}/assignment_groups",
            params={"include[]": ["assignments", "submission"], "per_page": 100},
        )
        if isinstance(groups, dict) and "error" in groups:
            return f"Error fetching grade breakdown: {groups['error']}"

        lines = [f"Grade breakdown — {course_display}\n"]
        earned_total = 0.0
        possible_total = 0.0
        remaining_total = 0.0

        for group in groups or []:
            name = group.get("name", "Unnamed group")
            weight = group.get("group_weight")
            weight_text = f" — weight {weight:.0f}%" if isinstance(weight, (int, float)) and weight else ""
            lines.append(
                f"## {fence_untrusted_inline(name, 'assignment group name')}{weight_text}"
            )

            earned = 0.0
            possible = 0.0
            remaining = 0.0
            for assignment in group.get("assignments") or []:
                points = assignment.get("points_possible") or 0
                submission = assignment.get("submission") or {}
                score = submission.get("score")
                title = fence_untrusted_inline(assignment.get("name", "Assignment"), "assignment name")
                due = format_date(assignment.get("due_at"))

                if submission.get("submitted_at") is None and score is None:
                    remaining += points
                    lines.append(f"- {title}: not submitted (due {due}, {points} pts)")
                elif score is None:
                    lines.append(f"- {title}: submitted, ungraded ({points} pts)")
                else:
                    earned += score
                    possible += points
                    lines.append(f"- {title}: {score}/{points} (due {due})")

            if possible:
                lines.append(f"  Group: {earned:.1f}/{possible:.1f} = {100 * earned / possible:.1f}%")
            if remaining:
                lines.append(f"  Still available: {remaining:.1f} pts")
            earned_total += earned
            possible_total += possible
            remaining_total += remaining

        if possible_total:
            lines.append(
                f"\nGraded so far: {earned_total:.1f}/{possible_total:.1f} "
                f"= {100 * earned_total / possible_total:.1f}%"
            )
        if remaining_total:
            lines.append(f"Remaining points: {remaining_total:.1f}")

        if target_grade is not None and remaining_total > 0:
            total = possible_total + remaining_total
            needed = (target_grade / 100 * total) - earned_total
            per_point = needed / remaining_total * 100
            lines.append(
                f"\nTo finish at {target_grade:.0f}% you need {max(needed, 0):.1f} of the "
                f"{remaining_total:.1f} remaining points ({max(per_point, 0):.1f}%)."
            )
            if per_point > 100:
                lines.append("That is above 100% — this target needs dropped/lowest-score rules or extra credit.")
            lines.append(
                "Note: this is a straight points calculation. If the course uses weighted "
                "groups or drop rules, Canvas's own total is authoritative."
            )
        elif target_grade is not None:
            lines.append("\nNo ungraded points left — the final grade is already determined.")

        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_week_plan(
        days: int = 7,
        course_identifier: str | int | None = None,
    ) -> str:
        """A prioritised plan for the next N days: overdue, today, then by date.

        Args:
            days: How many days ahead to look (default 7)
            course_identifier: Restrict to one course (optional)
        """
        if days < 1:
            return "Error: days must be at least 1."

        now = datetime.now(UTC)
        end = now + timedelta(days=days)
        params: dict = {
            "start_date": (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "per_page": 100,
        }
        if course_identifier is not None:
            course_id = await get_course_id(course_identifier)
            params["context_codes[]"] = [f"course_{course_id}"]

        items = await fetch_all_paginated_results("/planner/items", params)
        if isinstance(items, dict) and "error" in items:
            return f"Error fetching planner items: {items['error']}"

        rows = []
        for item in items if isinstance(items, list) else []:
            plannable_type = item.get("plannable_type")
            plannable = item.get("plannable") or {}
            if plannable_type in ("assignment", "quiz"):
                due_at = plannable.get("due_at") or item.get("plannable_date")
            elif plannable_type == "discussion_topic":
                due_at = plannable.get("due_at")
            else:
                continue
            due = parse_date(due_at)
            if not due:
                continue
            submissions = item.get("submissions")
            submitted = isinstance(submissions, dict) and bool(submissions.get("submitted"))
            rows.append(
                {
                    "name": plannable.get("title", "Untitled"),
                    "due": due,
                    "due_raw": due_at,
                    "course_id": item.get("course_id"),
                    "points": plannable.get("points_possible"),
                    "submitted": submitted,
                    "type": plannable_type,
                }
            )

        if not rows:
            return f"Nothing due in the next {days} days."

        rows.sort(key=lambda row: row["due"])
        overdue = [r for r in rows if r["due"] < now and not r["submitted"]]
        today = [r for r in rows if now <= r["due"] <= now + timedelta(days=1) and not r["submitted"]]
        upcoming = [r for r in rows if r["due"] > now + timedelta(days=1) and not r["submitted"]]
        done = [r for r in rows if r["submitted"]]

        lines = [f"Week plan — next {days} days\n"]
        for label, bucket in (("OVERDUE", overdue), ("DUE TODAY/TOMORROW", today), ("UPCOMING", upcoming)):
            if not bucket:
                continue
            lines.append(f"## {label} ({len(bucket)})")
            for row in bucket:
                course = await get_course_code(row["course_id"]) if row["course_id"] else "?"
                points = f", {row['points']} pts" if row["points"] is not None else ""
                lines.append(
                    f"- {fence_untrusted_inline(row['name'], 'assignment name')} "
                    f"[{course}] due {format_date(row['due_raw'])}{points}"
                )
            lines.append("")

        if done:
            lines.append(f"Submitted ({len(done)}): " + ", ".join(
                fence_untrusted_inline(row["name"], "assignment name") for row in done[:8]
            ))

        course_ids: list[int] = []
        for row in rows:
            if row["course_id"] and row["course_id"] not in course_ids:
                course_ids.append(row["course_id"])
        if course_ids:
            lines.append("## Material readiness")
            for course_id in course_ids:
                course = await get_course_code(course_id) or course_id
                files = await fetch_all_paginated_results(
                    f"/courses/{course_id}/files", {"per_page": 100}
                )
                if isinstance(files, dict) and "error" in files:
                    lines.append(f"- {course}: could not list files ({files['error']})")
                    continue
                if not files:
                    lines.append(f"- {course}: no files posted yet")
                    continue
                lines.extend(
                    readiness_lines(course_id, course, files, limit=6, header=f"- {course}:")
                )
            lines.append("")

        if overdue:
            lines.append(
                f"\nStart here: {len(overdue)} overdue item(s). "
                "Open each with get_my_assignment_brief before doing anything else."
            )
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_feedback(
        course_identifier: str | int,
        assignment_id: str | int | None = None,
    ) -> str:
        """Your feedback: rubric assessment and comments, for one assignment or a course.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Specific assignment (omit to scan the whole course)
        """
        course_id = await get_course_id(course_identifier)
        course_display = await get_course_code(course_id) or course_identifier

        target_ids: list[str] = []
        if assignment_id is not None:
            validated = coerce_canvas_id(assignment_id)
            if validated is None:
                return "Error: assignment_id must be a numeric Canvas assignment ID."
            target_ids = [validated]
        else:
            assignments = await fetch_all_paginated_results(
                f"/courses/{course_id}/assignments",
                params={"per_page": 100, "include[]": ["submission"]},
            )
            if isinstance(assignments, dict) and "error" in assignments:
                return f"Error fetching assignments: {assignments['error']}"
            for assignment in assignments or []:
                submission = assignment.get("submission") or {}
                if submission.get("graded_at") or submission.get("submission_comments"):
                    target_ids.append(str(assignment.get("id")))

        if not target_ids:
            return "No graded work with feedback in that scope yet."

        lines = [f"Feedback — {course_display}\n"]
        for aid in target_ids[:25]:
            submission = await make_canvas_request(
                "get",
                f"/courses/{course_id}/assignments/{aid}/submissions/self",
                params={"include[]": ["submission_comments", "rubric_assessment"]},
            )
            if not isinstance(submission, dict):
                return f"Error reading submission {aid}: unexpected response from Canvas"
            if "error" in submission:
                return f"Error reading submission {aid}: {submission['error']}"
            assignment = submission.get("assignment") or {}
            name = assignment.get("name", f"Assignment {aid}")
            lines.append(
                f"## {fence_untrusted_inline(name, 'assignment name')} — "
                f"{submission.get('grade', 'ungraded')}"
            )
            assessment = submission.get("rubric_assessment") or {}
            for criterion_id, detail in assessment.items():
                if not isinstance(detail, dict):
                    continue
                lines.append(
                    f"- Rubric {criterion_id}: {detail.get('points', '?')} pts"
                    + (f" — {fence_untrusted(detail.get('comments'), 'rubric comment')}"
                       if detail.get("comments") else "")
                )
            for comment in submission.get("submission_comments") or []:
                lines.append(
                    "- "
                    + fence_untrusted(comment.get("comment", ""), "submission comment")
                )
            lines.append("")

        if len(lines) == 1:
            return "No readable feedback found."
        lines.append(
            "Use this to target study time: rubric criteria that scored low are the weak spots."
        )
        return "\n".join(lines)
