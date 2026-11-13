"""Study guides and practice tests, written as markdown into the student workspace.

The model composes the content; these tools enforce the shape that makes a guide
usable for an exam — a fixed skeleton, a hard page budget, a source map, and an
honest report of what is missing or could not be read.
"""

from __future__ import annotations

from pathlib import Path

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core import rubric as rubric_core
from ..core import student_store
from ..core import study as study_core
from ..core.cache import get_course_code, get_course_id
from ..core.validation import coerce_canvas_id, validate_params

_PREVIEW_CHARS = 1200


def _guide_path(
    course_identifier: str | int, course_code: str | None, title: str
) -> Path:
    return student_store.study_dir(course_identifier, course_code) / f"{student_store.slug(title)}.md"


def _placeholder(title: str, course: str, sources: str, exam_date: str) -> str:
    """The skeleton handed back when the model asks for a guide with no content."""
    return f"""# {title} — study guide

## 0. What's on it
- Course: {course}
- Sources: {sources or "not recorded yet"}
- Exam: {exam_date or "not scheduled / not stated"}

## 1. The things that carry the marks
## 2. Definitions you must be able to state
## 3. Formulas and procedures
## 4. Worked examples
## 5. Traps and distractors
## 6. Compressed tables
## 7. Self-test
## 8. Source map
"""


def register_study_tools(mcp: FastMCP) -> None:
    """Register study-guide tools."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def build_study_guide(
        course_identifier: str | int,
        title: str,
        markdown: str = "",
        sources: str = "",
        exam_date: str = "",
        max_pages: float = 3.0,
        overwrite: bool = False,
        assignment_id: str | int | None = None,
    ) -> str:
        """Save a study guide as .md in the student workspace.

        Write the content first by reading the course material
        (get_course_content_map -> read_course_material -> read_material_section),
        then pass it here. Guidance for what the content should contain:

        - Section 0: what is actually examinable, what is excluded, and anything
          the professor explicitly flagged.
        - One idea per bullet. Cut history, anecdotes and logistics.
        - Keep what is repeated across sources, bolded, in learning objectives,
          or marked as exam-relevant.
        - Every claim gets a source marker like [deck 4, slide 12].
        - Section 7 is a self-test with answers in <details>.
        - Compress hard: the default budget is 3 pages.

        Calling with empty markdown returns the required skeleton instead of
        writing a file.

        Args:
            course_identifier: Course code or Canvas ID
            title: Guide title, e.g. "CS 225 Midterm 2"
            markdown: Full guide content in markdown
            sources: What it was built from (files, pages, dates)
            exam_date: ISO date if known
            max_pages: Page budget (default 3; ~450 words per page)
            overwrite: Replace an existing guide with the same title
            assignment_id: Check the guide against this assignment's rubric
        """
        course_id = await get_course_id(course_identifier)
        course_code = await get_course_code(course_id) or str(course_identifier)

        if not markdown.strip():
            return (
                "No content yet. Read the material, then call this again with markdown. "
                "Required skeleton:\n\n"
                + _placeholder(title, course_code, sources, exam_date)
            )

        target = _guide_path(course_id, course_code, title)
        if target.exists() and not overwrite:
            return (
                f"A guide already exists at {target}. Pass overwrite=true to replace it, "
                "or use update_study_guide to add to it."
            )

        pages, within_budget = study_core.budget_report(markdown, max_pages)
        missing = study_core.missing_sections(markdown)
        has_sources = study_core.has_source_map(markdown)

        header = study_core.front_matter(
            {
                "title": title,
                "course": course_code,
                "kind": "study-guide",
                "created": study_core.word_count(markdown) and _now(),
                "sources": sources,
                "exam_date": exam_date,
                "words": study_core.word_count(markdown),
                "pages": pages,
            }
        )
        student_store.write_text(target, f"{header}\n\n{markdown.strip()}\n")

        report = [
            f"Study guide saved: {target}",
            f"Words: {study_core.word_count(markdown)} | pages: {pages} (budget {max_pages})",
        ]
        if not within_budget:
            report.append(
                f"⚠️  Over budget by {round(pages - max_pages, 1)} pages. "
                "Cut anything that is not load-bearing for the exam."
            )
        if missing:
            report.append("Missing sections: " + ", ".join(missing))
        if not has_sources:
            report.append(
                "⚠️  No source markers found. Add [deck/slide/page] references so every "
                "claim can be checked against the material."
            )
        if assignment_id is not None:
            validated = coerce_canvas_id(assignment_id)
            parsed_rubric = (
                rubric_core.load(course_id, validated) if validated else None
            )
            if parsed_rubric is None or not parsed_rubric.criteria:
                report.append(
                    f"No rubric on file for assignment {assignment_id}, so this guide was "
                    "not checked against one. Run get_my_assignment_rubric first if that "
                    "assignment has criteria."
                )
            else:
                verdicts = rubric_core.check(parsed_rubric, markdown)
                addressed = sum(
                    1 for verdict in verdicts if verdict.verdict == "addressed"
                )
                report.append(
                    f"Rubric ({parsed_rubric.source}): {addressed}/{len(verdicts)} "
                    "criteria referenced by the guide."
                )
                uncovered = [
                    verdict.criterion.plain()
                    for verdict in verdicts
                    if verdict.verdict != "addressed"
                ]
                if uncovered:
                    report.append("Criteria not covered: " + ", ".join(uncovered[:8]))

        report.append(f"\nPreview:\n{markdown.strip()[:_PREVIEW_CHARS]}")
        return "\n".join(report)

    @mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
    @validate_params
    async def update_study_guide(
        course_identifier: str | int,
        title: str,
        markdown: str,
        mode: str = "append",
    ) -> str:
        """Add to an existing study guide.

        Args:
            course_identifier: Course code or Canvas ID
            title: The guide's title (same as build_study_guide)
            markdown: Content to add
            mode: "append" (default) or "replace"
        """
        if mode not in ("append", "replace"):
            return "Error: mode must be 'append' or 'replace'."

        course_id = await get_course_id(course_identifier)
        course_code = await get_course_code(course_id) or str(course_identifier)
        target = _guide_path(course_id, course_code, title)
        if not target.is_file():
            return f"No guide at {target}. Build it first with build_study_guide."

        existing = target.read_text(encoding="utf-8", errors="replace")
        if mode == "replace":
            combined = markdown.strip()
        else:
            combined = existing.rstrip() + "\n\n" + markdown.strip()
        student_store.write_text(target, combined.rstrip() + "\n")

        pages = study_core.estimate_pages(combined)
        return (
            f"Guide updated ({mode}): {target}\n"
            f"Words: {study_core.word_count(combined)} | pages: {pages}\n"
            f"Missing sections: {', '.join(study_core.missing_sections(combined)) or 'none'}"
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_study_guides(course_identifier: str | int | None = None) -> str:
        """List saved study guides.

        Args:
            course_identifier: Course code or Canvas ID (omit for all courses)
        """
        root = student_store.data_dir() / "courses"
        guides = sorted(root.glob("*/study/*.md"), reverse=True)
        if not guides:
            return "No study guides yet. Build one with build_study_guide."

        lines = ["Saved study guides:\n"]
        for path in guides:
            course = path.parts[-3]
            if course_identifier is not None and student_store.slug(str(course_identifier)) not in course:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            lines.append(
                f"- {path.stem} [{course}] — {study_core.word_count(text)} words, "
                f"{study_core.estimate_pages(text)} pages\n  {path}"
            )
        return "\n".join(lines) if len(lines) > 1 else "No study guides for that course."

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def read_study_guide(
        course_identifier: str | int,
        title: str,
        max_chars: int = 8000,
    ) -> str:
        """Read a saved study guide.

        Args:
            course_identifier: Course code or Canvas ID
            title: The guide's title
            max_chars: Maximum characters to return (default 8000)
        """
        course_id = await get_course_id(course_identifier)
        course_code = await get_course_code(course_id) or str(course_identifier)
        target = _guide_path(course_id, course_code, title)
        if not target.is_file():
            return f"No guide '{title}' for that course. Use list_study_guides."
        text = target.read_text(encoding="utf-8", errors="replace")
        body = text[:max(2000, max_chars)]
        suffix = "" if len(text) <= len(body) else "\n...[truncated; open the file for the rest]"
        return f"{target}\n\n{body}{suffix}"

    @mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
    @validate_params
    async def make_practice_test(
        course_identifier: str | int,
        title: str,
        markdown: str,
        num_questions: int = 20,
    ) -> str:
        """Save a practice test with a collapsible answer key.

        Generate questions from the study guide (not from thin air), mix formats,
        and put every answer under a <details> block so the student can test
        honestly.

        Args:
            course_identifier: Course code or Canvas ID
            title: Test title, e.g. "CS 225 Midterm 2 practice"
            markdown: Questions plus an '## Answers' section wrapped in <details>
            num_questions: How many questions you intended (for the report)
        """
        if not markdown.strip():
            return "No content. Pass the questions and an '## Answers' section."

        course_id = await get_course_id(course_identifier)
        course_code = await get_course_code(course_id) or str(course_identifier)
        target = (
            student_store.study_dir(course_id, course_code)
            / f"{student_store.slug(title)}-practice.md"
        )
        student_store.write_text(target, markdown.strip() + "\n")

        problems = study_core.practice_test_shape(markdown)
        report = [
            f"Practice test saved: {target}",
            f"Intended questions: {num_questions} | words: {study_core.word_count(markdown)}",
        ]
        report.extend(f"⚠️  {problem}" for problem in problems)
        if not problems:
            report.append("Shape looks right: numbered questions, collapsible answers.")
        return "\n".join(report)


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")
