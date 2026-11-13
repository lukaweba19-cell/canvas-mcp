"""Submitting an approved draft: preview, token binding, and the actual POST."""

from __future__ import annotations

import pytest
from helpers import call, token_in

from canvas_mcp.core import student_store

ASSIGNMENT = {
    "id": 42,
    "name": "Essay 1",
    "submission_types": ["online_text_entry"],
    "allowed_attempts": -1,
    "due_at": None,
    "lock_at": None,
    "group_category_id": None,
}

SUBMISSION = {"attempt": 0, "submitted_at": None, "workflow_state": "unsubmitted"}


@pytest.fixture
def canvas_routes(canvas, policy_allow):
    canvas.route("get", "/courses/225/assignments/42/submissions/self", SUBMISSION)
    canvas.route("get", "/courses/225/assignments/42", ASSIGNMENT)
    canvas.route(
        "post",
        "/courses/225/assignments/42/submissions",
        {"submitted_at": "2026-11-13T10:00:00Z", "attempt": 1},
    )
    return canvas


async def test_preview_returns_a_token_and_submits_nothing(canvas_routes, mcp):
    student_store.save_draft("225", "42", "My reviewed essay body", title="Essay 1")

    preview = await call(
        mcp, "submit_assignment", course_identifier=225, assignment_id=42,
        submission_type="online_text_entry", from_draft=True,
    )

    assert "NOTHING has been submitted" in preview
    assert "My reviewed essay body" in preview
    assert "Attempts: 0 used, unlimited allowed" in preview
    token_in(preview)
    assert not canvas_routes.calls_to("post"), "a preview must never POST"


async def test_confirming_with_the_token_submits_the_draft_body(canvas_routes, mcp):
    student_store.save_draft("225", "42", "My reviewed essay body", title="Essay 1")

    preview = await call(
        mcp, "submit_assignment", course_identifier=225, assignment_id=42,
        submission_type="online_text_entry", from_draft=True,
    )
    result = await call(
        mcp, "submit_assignment", course_identifier=225, assignment_id=42,
        submission_type="online_text_entry", from_draft=True,
        confirmation_token=token_in(preview),
    )

    assert "✅ Submitted." in result
    posts = [
        payload
        for method, endpoint, payload in canvas_routes.payloads
        if method == "POST" and endpoint.endswith("/submissions")
    ]
    assert len(posts) == 1
    assert posts[0]["submission[body]"].strip() == "My reviewed essay body"
    assert posts[0]["submission[submission_type]"] == "online_text_entry"


async def test_changing_the_draft_voids_the_token(canvas_routes, mcp):
    student_store.save_draft("225", "42", "First approved text", title="Essay 1")
    preview = await call(
        mcp, "submit_assignment", course_identifier=225, assignment_id=42,
        submission_type="online_text_entry", from_draft=True,
    )

    # The student edits the file between preview and confirmation.
    student_store.save_draft("225", "42", "Different text they never saw", title="Essay 1")
    result = await call(
        mcp, "submit_assignment", course_identifier=225, assignment_id=42,
        submission_type="online_text_entry", from_draft=True,
        confirmation_token=token_in(preview),
    )

    assert "does not match" in result
    assert not canvas_routes.calls_to("post")


async def test_token_cannot_be_reused(canvas_routes, mcp):
    student_store.save_draft("225", "42", "Body", title="Essay 1")
    preview = await call(
        mcp, "submit_assignment", course_identifier=225, assignment_id=42,
        submission_type="online_text_entry", from_draft=True,
    )
    token = token_in(preview)
    first = await call(
        mcp, "submit_assignment", course_identifier=225, assignment_id=42,
        submission_type="online_text_entry", from_draft=True, confirmation_token=token,
    )
    second = await call(
        mcp, "submit_assignment", course_identifier=225, assignment_id=42,
        submission_type="online_text_entry", from_draft=True, confirmation_token=token,
    )

    assert "✅ Submitted." in first
    assert "already used" in second
    posts = [call for call in canvas_routes.calls if call[0] == "POST"]
    assert len(posts) == 1, "a reused token must not send a second submission"


async def test_from_draft_without_a_draft_explains_what_to_do(canvas_routes, mcp):
    result = await call(
        mcp, "submit_assignment", course_identifier=225, assignment_id=42,
        submission_type="online_text_entry", from_draft=True,
    )

    assert "no saved draft" in result
    assert "save_assignment_draft" in result


async def test_from_draft_is_rejected_for_uploads(canvas_routes, mcp):
    student_store.save_draft("225", "42", "Body", title="Essay 1")
    result = await call(
        mcp, "submit_assignment", course_identifier=225, assignment_id=42,
        submission_type="online_upload", from_draft=True, file_contents=[{"name": "a.txt", "content_base64": "aGk="}],
    )

    assert "only applies to online_text_entry" in result


async def test_group_assignment_is_refused(canvas, policy_allow, mcp):
    canvas.route("get", "/courses/225/assignments/7/submissions/self", SUBMISSION)
    canvas.route(
        "get", "/courses/225/assignments/7",
        {**ASSIGNMENT, "id": 7, "group_category_id": 3},
    )

    result = await call(
        mcp, "submit_assignment", course_identifier=225, assignment_id=7,
        submission_type="online_text_entry", body="group work",
    )

    assert "group assignment" in result
    assert not canvas.calls_to("post")
