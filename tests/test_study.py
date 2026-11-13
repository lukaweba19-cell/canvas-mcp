"""Study guides: the skeleton, the page budget, the source map, and practice tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from helpers import call

GUIDE = """# CS 225 Midterm 2

## 0. What's on it
Trees, recursion, complexity. [deck 3, slide 2]

## 1. The things that carry the marks
- Base case stops descent [deck 3, slide 4]
- Depth is O(n) worst case [deck 4, slide 9]

## 2. Definitions you must be able to state
- Balanced tree: height is O(log n) [deck 3, slide 7]

## 3. Formulas and procedures
- T(n) = 2T(n/2) + O(n) [deck 4, slide 11]

## 4. Worked examples
- Recurrence on n=8 [deck 4, slide 12]

## 5. Traps and distractors
- Confusing height with size [deck 4, slide 14]

## 6. Compressed tables
| structure | lookup |
| --- | --- |
| BST | O(h) |

## 7. Self-test
1. When does recursion stop? [deck 3, slide 4]

## 8. Source map
- deck 3 (slides 2, 4, 7), deck 4 (slides 9, 11, 12, 14)
"""


@pytest.fixture
def course(canvas):
    canvas.route("get", "/courses/225", {"id": 225, "course_code": "CS225"})
    return canvas


def _guides() -> list[Path]:
    root = Path(os.environ["CANVAS_STUDENT_DIR"])
    return sorted(root.rglob("study/*.md"))


async def test_empty_markdown_returns_the_required_skeleton(course, mcp):
    result = await call(mcp, "build_study_guide", course_identifier=225, title="Midterm")

    assert "No content yet" in result
    assert "## 0. What's on it" in result
    assert "## 8. Source map" in result
    assert not _guides(), "an empty call must not write a file"


async def test_guide_is_saved_and_readable(course, mcp):
    result = await call(
        mcp, "build_study_guide", course_identifier=225, title="CS 225 Midterm 2",
        markdown=GUIDE, sources="deck 3, deck 4", exam_date="2026-12-10",
    )

    assert "Study guide saved" in result
    assert "pages:" in result
    saved = _guides()
    assert len(saved) == 1
    text = saved[0].read_text(encoding="utf-8")
    assert "exam_date: 2026-12-10" in text, "front matter should record the exam date"

    read = await call(mcp, "read_study_guide", course_identifier=225, title="CS 225 Midterm 2")
    assert "Base case stops descent" in read


async def test_over_budget_guide_is_flagged(course, mcp):
    bloated = GUIDE + "\n" + ("padding words that earn no marks " * 500)
    result = await call(
        mcp, "build_study_guide", course_identifier=225, title="Bloated", markdown=bloated
    )

    assert "Over budget" in result


async def test_short_guide_is_reported_without_a_budget_warning(course, mcp):
    result = await call(
        mcp, "build_study_guide", course_identifier=225, title="Short", markdown=GUIDE
    )
    assert "Over budget" not in result


async def test_missing_sections_are_named(course, mcp):
    result = await call(
        mcp, "build_study_guide", course_identifier=225, title="Thin",
        markdown="# Thin\n\n## 0. What's on it\nJust this.",
    )

    assert "Missing sections:" in result


async def test_missing_source_markers_are_flagged(course, mcp):
    unsourced = """# Guide

## 0. What's on it
Everything.

## 2. Definitions
- A tree is a graph without cycles.

## 7. Self-test
1. What is a tree?

## 8. Further reading
- Chapter 4.
"""
    result = await call(
        mcp, "build_study_guide", course_identifier=225, title="Unsourced", markdown=unsourced
    )

    assert "No source markers found" in result


async def test_existing_guide_is_not_overwritten_by_accident(course, mcp):
    await call(mcp, "build_study_guide", course_identifier=225, title="Same", markdown=GUIDE)
    again = await call(mcp, "build_study_guide", course_identifier=225, title="Same", markdown=GUIDE)

    assert "already exists" in again
    assert len(_guides()) == 1


async def test_update_appends_then_replaces(course, mcp):
    await call(mcp, "build_study_guide", course_identifier=225, title="Grow", markdown=GUIDE)

    appended = await call(
        mcp, "update_study_guide", course_identifier=225, title="Grow",
        markdown="## 9. Extra\n- One more fact [deck 5, slide 1]",
    )
    assert "append" in appended
    assert "Extra" in _guides()[0].read_text(encoding="utf-8")

    replaced = await call(
        mcp, "update_study_guide", course_identifier=225, title="Grow",
        markdown="# Only this", mode="replace",
    )
    assert "replace" in replaced
    assert _guides()[0].read_text(encoding="utf-8").strip().endswith("# Only this")


async def test_update_requires_an_existing_guide(course, mcp):
    result = await call(
        mcp, "update_study_guide", course_identifier=225, title="Nope", markdown="x"
    )
    assert "Build it first" in result


async def test_practice_test_without_answers_is_rejected_for_use(course, mcp):
    result = await call(
        mcp, "make_practice_test", course_identifier=225, title="Practice",
        markdown="# Practice\n\n1. Question one?\n2. Question two?",
    )

    assert "No answer key" in result
    assert "collapsible" in result


async def test_well_formed_practice_test_passes_shape_check(course, mcp):
    markdown = "# Practice\n\n" + "\n".join(
        f"{index}. Question {index}?" for index in range(1, 8)
    ) + "\n\n## Answers\n<details>\n1. A\n2. B\n</details>\n"

    result = await call(
        mcp, "make_practice_test", course_identifier=225, title="Practice",
        markdown=markdown, num_questions=7,
    )

    assert "Shape looks right" in result
    assert list(Path(os.environ["CANVAS_STUDENT_DIR"]).rglob("*practice.md"))


async def test_list_study_guides_finds_them(course, mcp):
    await call(mcp, "build_study_guide", course_identifier=225, title="Listed", markdown=GUIDE)

    result = await call(mcp, "list_study_guides")

    assert "listed" in result.lower()
    assert "words" in result
