"""Rubrics: both sources, persistence, and that they actually drive the work."""

from __future__ import annotations

import pytest
from helpers import call

from canvas_mcp.core import rubric as rubric_core

COURSE = {"id": 225, "course_code": "CS225", "name": "Algorithms"}

CANVAS_RUBRIC = {
    "id": 42,
    "name": "Essay 1",
    "description": "<p>Write about trees.</p>",
    "submission_types": ["online_text_entry"],
    "rubric": {
        "criteria": [
            {
                "description": "Thesis clarity",
                "points": 20,
                "ratings": [
                    {"points": 20, "description": "Precise and arguable"},
                    {"points": 10, "description": "Present but vague"},
                ],
            },
            {
                "description": "Use of evidence",
                "points": 30,
                "ratings": [{"points": 30, "description": "Quotes with analysis"}],
            },
            {"description": "Mechanics", "points": 10, "ratings": []},
        ]
    },
}

PROMPT_RUBRIC = {
    "id": 43,
    "name": "Lab report",
    "description": (
        "<p>Write up the experiment.</p><h2>Rubric</h2>"
        "<table><tr><th>Criterion</th><th>Points</th></tr>"
        "<tr><td>Thesis clarity</td><td>20</td></tr>"
        "<tr><td>Mechanics</td><td>10</td></tr></table>"
    ),
    "submission_types": ["online_text_entry"],
}

MERGED_RUBRIC = {
    "id": 44,
    "name": "Merged",
    "description": (
        "<h2>Rubric</h2><table><tr><th>Criterion</th><th>Points</th></tr>"
        "<tr><td>Thesis clarity</td><td>20</td></tr>"
        "<tr><td>Presentation</td><td>15</td></tr></table>"
    ),
    "rubric": {
        "criteria": [
            {"description": "Thesis clarity", "points": 20, "ratings": []},
        ]
    },
}

NO_RUBRIC = {
    "id": 45,
    "name": "Reflection",
    "description": "<p>Write two paragraphs about the term.</p>",
    "submission_types": ["online_text_entry"],
}


@pytest.fixture
def rubric_course(canvas):
    canvas.route("get", "/courses/225", COURSE)
    for assignment in (CANVAS_RUBRIC, PROMPT_RUBRIC, MERGED_RUBRIC, NO_RUBRIC):
        canvas.route(
            "get", f"/courses/225/assignments/{assignment['id']}", assignment
        )
    return canvas


async def test_rubric_read_from_canvas_includes_ratings(rubric_course, mcp):
    result = await call(mcp, "get_my_assignment_rubric", course_identifier=225, assignment_id=42)

    assert "Source: canvas (3 criteria, 60 points)" in result
    assert "Thesis clarity" in result
    assert "Use of evidence" in result
    assert "20 pts:" in result and "Precise and arguable" in result, (
        "rating levels must be shown with their point value"
    )
    assert "10 pts:" in result and "Present but vague" in result


async def test_rubric_parsed_from_the_prompt_when_canvas_has_none(rubric_course, mcp):
    result = await call(mcp, "get_my_assignment_rubric", course_identifier=225, assignment_id=43)

    assert "Source: prompt (2 criteria, 30 points)" in result
    assert "Thesis clarity" in result
    assert "Mechanics" in result


async def test_canvas_and_prompt_rubrics_merge_without_duplicates(rubric_course, mcp):
    result = await call(mcp, "get_my_assignment_rubric", course_identifier=225, assignment_id=44)

    assert "Source: canvas+prompt" in result
    assert result.count("Thesis clarity") == 1, "the same criterion must not appear twice"
    assert "Presentation" in result, "a criterion only the prompt lists must still appear"


async def test_missing_rubric_is_stated_explicitly(rubric_course, mcp):
    result = await call(mcp, "get_my_assignment_rubric", course_identifier=225, assignment_id=45)

    assert "No rubric found" in result
    assert "ask the instructor" in result
    assert "characters of assignment description" in result


async def test_rubric_is_saved_and_loads_in_a_later_session(rubric_course, mcp):
    await call(mcp, "get_my_assignment_rubric", course_identifier=225, assignment_id=42)

    stored = rubric_core.load("225", 42)
    assert stored is not None, "the rubric must survive across sessions"
    assert [criterion.plain() for criterion in stored.criteria] == [
        "Thesis clarity",
        "Use of evidence",
        "Mechanics",
    ]
    assert stored.total_points == 60


async def test_draft_save_reports_rubric_coverage_score(rubric_course, mcp):
    await call(mcp, "get_my_assignment_rubric", course_identifier=225, assignment_id=42)

    result = await call(
        mcp, "save_assignment_draft", course_identifier=225, assignment_id=42,
        content="Evidence from the reading supports my thesis about trees.",
        title="Essay 1",
    )

    assert "Rubric (canvas): 2/3 criteria referenced" in result
    assert "Mechanics" in result
    assert "check_work_against_rubric" in result


async def test_study_guide_is_checked_against_the_rubric(rubric_course, mcp):
    await call(mcp, "get_my_assignment_rubric", course_identifier=225, assignment_id=42)

    guide = """# Essay 1 plan

## 0. What's on it
The thesis clarity requirement and the evidence requirement. [deck 1, slide 2]

## 1. Key points
- A clear thesis states the claim [deck 1, slide 3]
- Evidence must be quoted with analysis [deck 1, slide 4]

## 7. Self-test
1. Does the thesis state a claim? [deck 1, slide 5]

## 8. Source map
- deck 1
"""
    result = await call(
        mcp, "build_study_guide", course_identifier=225, assignment_id=42,
        title="Essay 1 plan", markdown=guide,
    )

    assert "Rubric (canvas): 2/3 criteria referenced by the guide." in result
    assert "Criteria not covered: Mechanics" in result


async def test_study_guide_without_a_saved_rubric_says_so(rubric_course, mcp):
    result = await call(
        mcp, "build_study_guide", course_identifier=225, assignment_id=99,
        title="Unsaved", markdown="# Guide\n\n## 0. What's on it\nEverything [deck 1, slide 1]",
    )

    assert "No rubric on file for assignment 99" in result


async def test_check_gives_a_verdict_per_criterion(rubric_course, mcp):
    await call(mcp, "get_my_assignment_rubric", course_identifier=225, assignment_id=42)

    result = await call(
        mcp, "check_work_against_rubric", course_identifier=225, assignment_id=42,
        source="text",
        text="My thesis states the claim. Evidence from the reading supports it.",
    )

    assert "Rubric check — supplied text" in result
    lines = result.splitlines()
    verdict = {
        marker: next(line for line in lines if line.startswith(f"- [{marker}]"))
        for marker in ("addressed", "not found")
    }
    assert "Thesis clarity" in verdict["addressed"]
    assert "(20 pts)" in verdict["addressed"]
    assert "Mechanics" in verdict["not found"]
    assert result.count("[addressed]") == 2
    assert result.count("[not found]") == 1
    assert "keyword check, not a grade" in result


async def test_check_reads_the_saved_draft(rubric_course, mcp):
    await call(mcp, "get_my_assignment_rubric", course_identifier=225, assignment_id=42)
    await call(
        mcp, "save_assignment_draft", course_identifier=225, assignment_id=42,
        content="My thesis and evidence are both present.", title="Essay 1",
    )

    result = await call(
        mcp, "check_work_against_rubric", course_identifier=225, assignment_id=42
    )

    assert "Rubric check — your draft" in result


async def test_check_reads_the_submission_body(rubric_course, mcp):
    rubric_course.route(
        "get", "/courses/225/assignments/42/submissions/self",
        {"body": "A thesis with evidence attached.", "attempt": 1},
    )
    await call(mcp, "get_my_assignment_rubric", course_identifier=225, assignment_id=42)

    result = await call(
        mcp, "check_work_against_rubric", course_identifier=225, assignment_id=42,
        source="submission",
    )

    assert "Rubric check — your submission" in result


async def test_check_without_any_rubric_is_explicit(rubric_course, mcp):
    result = await call(
        mcp, "check_work_against_rubric", course_identifier=225, assignment_id=45
    )

    assert "No rubric found for assignment 45" in result
    assert "nothing to check against" in result


async def test_brief_surfaces_the_full_rubric_and_saves_it(rubric_course, mcp):
    result = await call(mcp, "get_my_assignment_brief", course_identifier=225, assignment_id=42)

    assert "Rubric (canvas, 3 criteria, 60 points)" in result
    assert "Use of evidence" in result
    assert "Quotes with analysis" in result, "ratings must reach the brief too"
    assert rubric_core.load("225", 42) is not None


async def test_brief_states_when_there_is_no_rubric(rubric_course, mcp):
    result = await call(mcp, "get_my_assignment_brief", course_identifier=225, assignment_id=45)

    assert "No rubric" in result
    assert "Ask the instructor" in result
