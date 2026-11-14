"""The local workspace: brief, drafts, notes, journal/handoff, and style checks."""

from __future__ import annotations

import pytest
from helpers import call

from canvas_mcp.core import student_store

ASSIGNMENT = {
    "id": 42,
    "name": "Essay 1",
    "description": "<p>Write about trees and recursion.</p>",
    "submission_types": ["online_text_entry"],
    "points_possible": 50,
    "allowed_attempts": -1,
    "due_at": "2026-11-20T23:59:00Z",
    "group_category_id": None,
    "rubric": {
        "criteria": [
            {
                "description": "Thesis clarity",
                "points": 20,
                "ratings": [{"points": 20, "description": "Precise"}],
            }
        ]
    },
}


@pytest.fixture
def course(canvas):
    canvas.route(
        "get", "/courses/225/assignments/42/submissions/self",
        {"attempt": 0, "submitted_at": None, "workflow_state": "unsubmitted"},
    )
    canvas.route("get", "/courses/225/assignments/42", ASSIGNMENT)
    canvas.route("get", "/courses/225", {"id": 225, "course_code": "CS225"})
    return canvas


async def test_start_assignment_work_writes_brief_and_rubric(course, mcp):
    result = await call(
        mcp, "start_assignment_work", course_identifier=225, assignment_id=42
    )

    assert "brief.md" in result
    assert "rubric.md" in result
    assert "Write about trees and recursion." in result

    workspace = student_store.find_assignment_dir(225, 42)
    assert workspace is not None
    brief = (workspace / "brief.md").read_text(encoding="utf-8")
    assert "Essay 1" in brief
    assert "Due: 2026-11-20" in brief
    assert "Write about trees and recursion." in brief

    rubric = (workspace / "rubric.md").read_text(encoding="utf-8")
    assert "Thesis clarity" in rubric
    assert "20 pts" in rubric


async def test_drafts_are_versioned_and_latest_wins(course, mcp):
    first = await call(
        mcp, "save_assignment_draft", course_identifier=225, assignment_id=42,
        content="first version", title="Essay 1",
    )
    second = await call(
        mcp, "save_assignment_draft", course_identifier=225, assignment_id=42,
        content="second version", title="Essay 1", kind="final",
    )

    assert "v1" in first
    assert "v2" in second
    latest = await call(
        mcp, "read_assignment_draft", course_identifier=225, assignment_id=42
    )
    assert "second version" in latest

    older = await call(
        mcp, "read_assignment_draft", course_identifier=225, assignment_id=42, version=1
    )
    assert "first version" in older

    listed = await call(mcp, "list_assignment_drafts")
    assert "2 versions" in listed


def test_draft_is_found_by_assignment_id_without_a_title():
    """Pins the bug fixed by hand: lookup by id alone must not invent a folder."""
    student_store.save_draft("225", "42", "body from disk", title="Essay 1")

    found = student_store.read_draft("225", "42")
    assert found is not None
    assert found["content"].strip() == "body from disk"

    assert student_store.read_draft("225", "999") is None
    folders = [path.name for path in student_store.course_dir("225").iterdir()]
    assert folders == ["42-essay-1"]


def test_draft_resolves_when_the_caller_passes_a_course_code():
    student_store.save_draft("CS225", "7", "code lookup", title="Lab 7")

    found = student_store.read_draft("CS225", "7")
    assert found is not None
    assert found["content"].strip() == "code lookup"


async def test_notes_round_trip_with_search_and_kinds(mcp):
    await call(
        mcp, "save_note", title="Base cases", content="Every recursion needs one.",
        tags="exam", course_identifier=225,
    )
    await call(
        mcp, "save_note", title="Ask TA", content="Is the rubric weighted?",
        course_identifier=225, kind="question",
    )

    found = await call(mcp, "search_notes", query="recursion", course_identifier=225)
    assert "Base cases" in found

    questions = await call(mcp, "list_notes", course_identifier=225, kind="question")
    assert "Ask TA" in questions
    assert "Base cases" not in questions

    listed = await call(mcp, "list_notes", course_identifier=225)
    assert "Base cases" in listed

    note_id = student_store.list_notes(225, "question")[0]["id"]
    read = await call(mcp, "read_note", note_id=note_id)
    assert "Is the rubric weighted?" in read


async def test_work_log_and_handoff_report_what_needs_the_student(mcp):
    await call(
        mcp, "add_work_log_entry", summary="Drafted Essay 1 intro",
        course_identifier=225, assignment_id=42,
        needs_you="Choose which tree example to use", next_step="Draft the body",
    )

    handoff = await call(mcp, "get_my_handoff", days=7)

    assert "Drafted Essay 1 intro" in handoff
    assert "NEEDS YOU: Choose which tree example to use" in handoff
    assert "next: Draft the body" in handoff
    assert "waiting on you" in handoff


async def test_handoff_is_clean_when_nothing_is_pending(mcp):
    handoff = await call(mcp, "get_my_handoff")
    assert "Nothing is blocked on you" in handoff


async def test_style_check_names_the_stock_phrases(mcp):
    result = await call(
        mcp, "check_draft_style",
        text=(
            "It is important to note that trees matter. Moreover, they are very fast. "
            "Furthermore, they are used everywhere. In conclusion, trees are great."
        ),
    )

    assert "moreover" in result.lower()
    assert "furthermore" in result.lower()
    assert "words/sentence" in result


async def test_style_check_can_read_the_saved_draft(course, mcp):
    await call(
        mcp, "save_assignment_draft", course_identifier=225, assignment_id=42,
        content="Moreover, this draft exists.", title="Essay 1",
    )

    result = await call(
        mcp, "check_draft_style", course_identifier=225, assignment_id=42
    )

    assert "moreover" in result.lower()


async def test_voice_sample_creates_a_profile_used_by_the_style_check(mcp):
    saved = await call(
        mcp, "save_voice_sample",
        text=(
            "I think trees matter. They show up everywhere. "
            "The recursion part is what gets me."
        ),
    )

    assert "Voice profile saved" in saved
    profile = student_store.read_voice()
    assert "Average sentence" in profile
    assert "Do not add stock connectives" in profile

    checked = await call(mcp, "check_draft_style", text="A plain sentence.")
    assert "voice.md" in checked


async def test_brief_and_draft_share_one_folder_across_identifier_forms(course, mcp):
    """The brief must not land in a different course folder than the drafts.

    Workspace folders are named by course code when one is known and by course
    id otherwise, so an id-only lookup after a code-based write used to create a
    second folder and lose the brief.
    """
    import os
    from pathlib import Path

    await call(mcp, "start_assignment_work", course_identifier=225, assignment_id=42)
    after_brief = student_store.find_assignment_dir(225, 42)

    await call(
        mcp, "save_assignment_draft", course_identifier=225, assignment_id=42,
        content="draft body", title="Essay 1",
    )
    after_draft = student_store.find_assignment_dir(225, 42)

    assert after_brief == after_draft
    assert after_draft is not None
    assert (after_draft / "brief.md").is_file()

    root = Path(os.environ["CANVAS_STUDENT_DIR"]) / "courses"
    folders = [path for path in root.rglob("42-*") if path.is_dir()]
    assert len(folders) == 1, f"one assignment must have one folder: {folders}"


def test_id_lookup_finds_a_folder_created_under_the_course_code():
    student_store.assignment_dir("225", 42, "Essay 1", "CS225")
    student_store.save_draft("225", 42, "body written later")

    found = student_store.read_draft("225", 42)
    assert found is not None
    assert found["content"].strip() == "body written later"
