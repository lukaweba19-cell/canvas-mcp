"""Quiz discovery and opt-in Classic Quizzes taking.

Reading quizzes is enabled like every other read tool. Taking them is a
separate, off-by-default operator decision (``CANVAS_ENABLE_QUIZ_TAKING``)
because starting an attempt consumes one of a student's attempts and submitting
it is graded work.

Canvas has two quiz engines with very different APIs:

* **Classic Quizzes** expose a student attempt flow under ``/api/v1``: create a
  quiz submission, read its questions with a validation token, save answers,
  then complete it.
* **New Quizzes** do not expose a student start/answer/submit API at all. Those
  are listed and opened in Canvas; the taking tools refuse rather than pretend
  to have done something.

Every Canvas request goes through ``make_canvas_request`` so the captured
session cookie (and CSRF token on writes) is applied exactly once, centrally.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import fetch_all_paginated_results, make_canvas_request
from ..core.config import get_config
from ..core.course_policy import get_course_policy
from ..core.dates import format_date, parse_date
from ..core.untrusted_content import fence_untrusted, fence_untrusted_inline
from ..core.validation import coerce_canvas_id, validate_params

_NEW_QUIZ_TYPES = frozenset({"quizzes.next", "new_quizzes"})
_COMPLETE_STATES = frozenset({"complete", "pending_review"})


def _engine(quiz: dict[str, Any]) -> str:
    """Classify the quiz engine from the fields Canvas actually returns."""
    quiz_type = str(quiz.get("quiz_type") or "").lower()
    if (
        quiz_type in _NEW_QUIZ_TYPES
        or quiz.get("engine") == "new_quizzes"
        or quiz.get("quiz_lti")
    ):
        return "new_quizzes"
    return "classic"


def _engine_label(engine: str) -> str:
    return (
        "New Quizzes (unsupported for taking)"
        if engine == "new_quizzes"
        else "Classic Quizzes"
    )


def _is_outstanding(quiz: dict[str, Any], attempts: list[dict[str, Any]]) -> bool:
    """Whether the student still has to do this quiz.

    A completed attempt means it is done. Otherwise it is outstanding while the
    quiz is published, not past its lock date, and attempts remain.
    """
    if not quiz.get("published", True):
        return False
    lock_at = parse_date(quiz.get("lock_at"))
    if lock_at is not None and lock_at < datetime.now(UTC):
        return False
    if any(
        str(a.get("workflow_state", "")).lower() in _COMPLETE_STATES for a in attempts
    ):
        return False
    allowed = quiz.get("allowed_attempts")
    if isinstance(allowed, int) and allowed > 0 and len(attempts) >= allowed:
        return False
    return True


def _attempt_line(attempt: dict[str, Any]) -> str:
    state = str(attempt.get("workflow_state") or "unknown")
    score = attempt.get("score")
    score_text = "not graded" if score is None else f"{score:g}"
    parts = [
        f"attempt {attempt.get('attempt', '?')}",
        state,
        f"score {score_text}",
    ]
    if attempt.get("started_at"):
        parts.append(f"started {format_date(attempt.get('started_at'))}")
    if attempt.get("finished_at"):
        parts.append(f"finished {format_date(attempt.get('finished_at'))}")
    return "  - " + "; ".join(parts)


def _submission_attempts(response: Any) -> list[dict[str, Any]] | None:
    """Normalize the several shapes Canvas uses for quiz-submission payloads."""
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict):
        nested = response.get("quiz_submissions")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if response.get("id") is not None:
            return [response]
    return None


def _first_submission(response: Any) -> dict[str, Any] | None:
    submissions = _submission_attempts(response)
    return submissions[0] if submissions else None


async def _quiz_attempts(
    course_id: str, quiz: dict[str, Any]
) -> tuple[list[dict[str, Any]], str | None]:
    """The student's own attempts for one quiz, or an explanatory error.

    Classic Quizzes has a quiz-submission listing, but some institutions scope
    it to teachers. The assignment's ``submissions/self`` endpoint is the
    student-scoped fallback when that listing is refused.
    """
    if _engine(quiz) == "new_quizzes":
        return [], "New Quizzes does not expose attempt history through this API."

    quiz_id = quiz.get("id")
    if quiz_id is None:
        return [], "This item has no Canvas quiz id."
    response = await make_canvas_request(
        "get",
        f"/courses/{course_id}/quizzes/{quiz_id}/submissions",
        params={"per_page": 100},
    )
    attempts = _submission_attempts(response)
    if attempts is not None:
        return attempts, None
    error = str(
        response.get("error") if isinstance(response, dict) else "unexpected response"
    )
    if "session expired" in error.lower():
        return [], error

    assignment_id = quiz.get("assignment_id") or quiz_id
    fallback = await make_canvas_request(
        "get", f"/courses/{course_id}/assignments/{assignment_id}/submissions/self"
    )
    if isinstance(fallback, dict) and "error" not in fallback:
        return (
            [
                {
                    "attempt": fallback.get("attempt"),
                    "workflow_state": (
                        "complete" if fallback.get("submitted_at") else "untaken"
                    ),
                    "score": fallback.get("score"),
                    "submitted_at": fallback.get("submitted_at"),
                    "graded_at": fallback.get("graded_at"),
                }
            ],
            f"Quiz-submission listing unavailable ({error}); using the assignment submission.",
        )
    return [], error


def _quiz_heading(quiz: dict[str, Any], course_display: str) -> str:
    title = str(quiz.get("title") or f"Quiz {quiz.get('id', '?')}")
    return (
        f"{fence_untrusted_inline(title, 'quiz title')} "
        f"(id {quiz.get('id', '?')}) — {course_display}"
    )


def _quiz_body(quiz: dict[str, Any], attempts: list[dict[str, Any]]) -> str:
    engine = _engine(quiz)
    allowed = quiz.get("allowed_attempts")
    attempts_text = (
        "unlimited"
        if allowed is None or (isinstance(allowed, int) and allowed < 0)
        else str(allowed)
    )
    used = len(attempts)
    quiz_type = quiz.get("quiz_type") or "unknown"
    published = "yes" if quiz.get("published", True) else "no"
    time_limit = quiz.get("time_limit")
    lines = [
        f"  Engine: {_engine_label(engine)}",
        f"  Type: {fence_untrusted_inline(str(quiz_type), 'quiz type')} | Published: {published}",
        f"  Due: {format_date(quiz.get('due_at'))}",
        "  Available: "
        f"{format_date(quiz.get('unlock_at'))} → {format_date(quiz.get('lock_at'))}",
        f"  Time limit: {f'{time_limit} minutes' if time_limit else 'none'}",
        f"  Attempts: {used}/{attempts_text} used",
        f"  Points: {quiz.get('points_possible', 'N/A')}",
    ]
    if attempts:
        lines.extend(_attempt_line(attempt) for attempt in attempts)
    else:
        lines.append("  Attempts: none yet")
    if engine == "new_quizzes":
        lines.append("  Taking: unavailable through Canvas's API — open it in Canvas.")
    return "\n".join(lines)


async def _load_quiz(
    course_id: str, quiz_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    response = await make_canvas_request(
        "get", f"/courses/{course_id}/quizzes/{quiz_id}"
    )
    if isinstance(response, dict) and "error" in response:
        return None, str(response["error"])
    if not isinstance(response, dict):
        return None, "Canvas returned an unexpected quiz payload."
    return response, None


def _quiz_taking_disabled_message() -> str:
    return (
        "❌ Quiz taking is disabled on this server. Reading quizzes is always "
        "available, but starting, answering and submitting an attempt are a "
        "separate opt-in. Set CANVAS_ENABLE_QUIZ_TAKING=true in .env (or the "
        "server environment), restart the server, then retry. This does not "
        "affect list_quizzes, get_quiz_details, or get_my_quiz_attempts."
    )


async def _write_blocked(course_id: str, tool_name: str) -> str | None:
    """The flag and the course's instructor policy, checked before any write."""
    if not get_config().quiz_taking:
        return _quiz_taking_disabled_message()
    policy = await get_course_policy(course_id)
    if not policy.allow_writes:
        return f"❌ Quiz taking is blocked in this course. {policy.note}".strip()
    if policy.allow_tools is not None and tool_name not in policy.allow_tools:
        return (
            f"❌ This course permits agent writes but not '{tool_name}'. "
            f"{policy.note}"
        ).strip()
    return None


def _new_quiz_refusal(quiz: dict[str, Any]) -> str:
    title = fence_untrusted_inline(str(quiz.get("title") or "this quiz"), "quiz title")
    return (
        f"❌ {title} is a New Quizzes assessment. Canvas does not expose a "
        "student start/answer/submit API for New Quizzes, so this server "
        "cannot take it — open it in Canvas. Quiz reading still works; only "
        "Classic Quizzes can be taken here."
    )


def register_quiz_tools(mcp: FastMCP) -> None:
    """Register quiz discovery and opt-in Classic Quizzes taking."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_quizzes(course_identifier: str | int | None = None) -> str:
        """List your quizzes, with due dates, limits, attempts and scores.

        Covers Classic Quizzes and New Quizzes items that appear in the
        Planner, including quizzes that exist only as due items. With no course
        argument it scans all of your active courses.

        Args:
            course_identifier: Course code or Canvas ID. Omit for all courses.
        """
        courses: list[tuple[str, str]] = []
        if course_identifier is not None:
            course_id = await get_course_id(course_identifier)
            courses.append(
                (course_id, await get_course_code(course_id) or str(course_identifier))
            )
        else:
            enrolled = await fetch_all_paginated_results(
                "/courses",
                params={
                    "enrollment_state": "active",
                    "per_page": 100,
                },
            )
            if isinstance(enrolled, dict) and "error" in enrolled:
                return f"Error fetching your courses: {enrolled['error']}"
            for course in enrolled or []:
                if isinstance(course, dict) and course.get("id") is not None:
                    courses.append(
                        (
                            str(course["id"]),
                            str(
                                course.get("course_code")
                                or course.get("name")
                                or course["id"]
                            ),
                        )
                    )

        if not courses:
            return "No active courses found."

        planner_items: list[dict[str, Any]] = []
        planner_response = await fetch_all_paginated_results(
            "/planner/items", params={"filter": "incomplete_items", "per_page": 100}
        )
        planner_error: str | None = None
        if isinstance(planner_response, dict) and "error" in planner_response:
            planner_error = str(planner_response["error"])
            if "session expired" in planner_error.lower():
                return f"Error fetching your quizzes: {planner_error}"
        elif isinstance(planner_response, list):
            planner_items = [
                item for item in planner_response if isinstance(item, dict)
            ]

        lines: list[str] = ["Your quizzes:\n"]
        found = 0
        for course_id, course_display in courses:
            quizzes = await fetch_all_paginated_results(
                f"/courses/{course_id}/quizzes", params={"per_page": 100}
            )
            if isinstance(quizzes, dict) and "error" in quizzes:
                return (
                    f"Error fetching quizzes for {course_display}: {quizzes['error']}"
                )
            course_quizzes = [
                quiz
                for quiz in (quizzes or [])
                if isinstance(quiz, dict) and quiz.get("id") is not None
            ]

            seen = {str(quiz.get("id")) for quiz in course_quizzes}
            for item in planner_items:
                if str(item.get("course_id")) != str(course_id):
                    continue
                if item.get("plannable_type") != "quiz":
                    continue
                plannable = item.get("plannable") or {}
                quiz_id = item.get("plannable_id") or plannable.get("id")
                if quiz_id is None or str(quiz_id) in seen:
                    continue
                course_quizzes.append(
                    {
                        "id": quiz_id,
                        "title": plannable.get("title") or f"Quiz {quiz_id}",
                        "due_at": plannable.get("due_at") or item.get("plannable_date"),
                        "points_possible": plannable.get("points_possible"),
                        "quiz_type": plannable.get("quiz_type") or "planner item",
                        "published": True,
                        "_planner_only": True,
                    }
                )
                seen.add(str(quiz_id))

            if not course_quizzes:
                continue

            for quiz in course_quizzes:
                found += 1
                attempts: list[dict[str, Any]] = []
                attempt_note: str | None = None
                if not quiz.get("_planner_only"):
                    attempts, attempt_note = await _quiz_attempts(course_id, quiz)
                    if attempt_note and "session expired" in attempt_note.lower():
                        return f"Error fetching your quiz attempts: {attempt_note}"
                outstanding = _is_outstanding(quiz, attempts)
                marker = "OUTSTANDING — " if outstanding else ""
                lines.append(f"- {marker}{_quiz_heading(quiz, course_display)}")
                if quiz.get("_planner_only"):
                    lines.append(
                        "  Engine: not exposed by the Classic Quizzes listing "
                        "(often New Quizzes) — open it in Canvas"
                    )
                    lines.append(f"  Due: {format_date(quiz.get('due_at'))}")
                    lines.append(
                        f"  Points: {quiz.get('points_possible', 'N/A')} | "
                        "Details: open in Canvas"
                    )
                else:
                    lines.append(_quiz_body(quiz, attempts))
                    if attempt_note:
                        lines.append(f"  Note: {attempt_note}")
                lines.append("")

        if planner_error:
            lines.append(f"Planner items unavailable: {planner_error}\n")
        if found == 0:
            return "No quizzes found in your active courses."
        return "\n".join(lines).rstrip() + "\n"

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_quiz_details(course_identifier: str | int, quiz_id: str | int) -> str:
        """Get one quiz's rules, availability and your attempt history.

        Args:
            course_identifier: Course code or Canvas ID
            quiz_id: Canvas quiz ID
        """
        validated = coerce_canvas_id(quiz_id)
        if validated is None:
            return "Error: quiz_id must be a numeric Canvas quiz ID."
        course_id = await get_course_id(course_identifier)
        quiz, error = await _load_quiz(course_id, str(validated))
        if quiz is None:
            return f"Error fetching quiz: {error}"
        attempts, attempt_note = await _quiz_attempts(course_id, quiz)
        if attempt_note and "session expired" in attempt_note.lower():
            return f"Error fetching quiz attempts: {attempt_note}"

        course_display = await get_course_code(course_id) or str(course_identifier)
        lines = [
            f"Quiz: {_quiz_heading(quiz, course_display)}",
            _quiz_body(quiz, attempts),
            f"  Questions: {quiz.get('question_count', 'N/A')}",
            f"  Scoring policy: {quiz.get('scoring_policy', 'N/A')}",
            f"  Access code required: {'yes' if quiz.get('has_access_code') else 'no'}",
            f"  One question at a time: {'yes' if quiz.get('one_question_at_a_time') else 'no'}",
            f"  Can go back: {'yes' if quiz.get('cant_go_back') is not True else 'no'}",
        ]
        if quiz.get("description"):
            lines.append(
                "Description:\n"
                + fence_untrusted(str(quiz["description"]), "quiz description")
            )
        if attempt_note:
            lines.append(f"Note: {attempt_note}")
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_quiz_attempts(
        course_identifier: str | int, quiz_id: str | int
    ) -> str:
        """Show your own attempts for one quiz, with scores and what is left.

        Args:
            course_identifier: Course code or Canvas ID
            quiz_id: Canvas quiz ID
        """
        validated = coerce_canvas_id(quiz_id)
        if validated is None:
            return "Error: quiz_id must be a numeric Canvas quiz ID."
        course_id = await get_course_id(course_identifier)
        quiz, error = await _load_quiz(course_id, str(validated))
        if quiz is None:
            return f"Error fetching quiz: {error}"
        attempts, note = await _quiz_attempts(course_id, quiz)
        if note and "session expired" in note.lower():
            return f"Error fetching quiz attempts: {note}"

        allowed = quiz.get("allowed_attempts")
        remaining = (
            "unlimited"
            if allowed is None or (isinstance(allowed, int) and allowed < 0)
            else max(0, int(allowed) - len(attempts))
        )
        title = fence_untrusted_inline(
            str(quiz.get("title") or f"Quiz {validated}"), "quiz title"
        )
        lines = [
            f"Attempts for {title} (quiz {validated}):",
            f"  Remaining: {remaining}",
        ]
        if attempts:
            lines.extend(_attempt_line(attempt) for attempt in attempts)
        else:
            lines.append("  No attempts yet.")
        if _is_outstanding(quiz, attempts):
            lines.append("  Status: OUTSTANDING — you still have to do this quiz.")
        else:
            lines.append("  Status: complete (or no longer available).")
        if note:
            lines.append(f"  Note: {note}")
        if _engine(quiz) == "new_quizzes":
            lines.append(
                "  Taking: unavailable through Canvas's API — open it in Canvas."
            )
        return "\n".join(lines)

    @mcp.tool()
    @validate_params
    async def start_quiz_attempt(
        course_identifier: str | int,
        quiz_id: str | int,
        access_code: str = "",
    ) -> str:
        """Start a Classic Quizzes attempt (opt-in; consumes an attempt).

        Requires CANVAS_ENABLE_QUIZ_TAKING=true. New Quizzes is refused because
        Canvas exposes no student take API for it.

        Args:
            course_identifier: Course code or Canvas ID
            quiz_id: Canvas quiz ID
            access_code: Access code, when the quiz requires one
        """
        validated = coerce_canvas_id(quiz_id)
        if validated is None:
            return "Error: quiz_id must be a numeric Canvas quiz ID."
        course_id = await get_course_id(course_identifier)
        quiz, error = await _load_quiz(course_id, str(validated))
        if quiz is None:
            return (
                f"Error starting quiz: {error}. If this is a Planner-only quiz, "
                "it may be a New Quiz or an external tool; open it in Canvas."
            )
        if _engine(quiz) == "new_quizzes":
            return _new_quiz_refusal(quiz)
        blocked = await _write_blocked(course_id, "start_quiz_attempt")
        if blocked:
            return blocked
        if quiz.get("has_access_code") and not access_code:
            return (
                "❌ This quiz requires an access code. Call start_quiz_attempt "
                "again with access_code set to the code your instructor gave you."
            )

        data: dict[str, Any] = {}
        if access_code:
            data["access_code"] = access_code
        response = await make_canvas_request(
            "post",
            f"/courses/{course_id}/quizzes/{validated}/submissions",
            data=data,
            use_form_data=True,
        )
        if isinstance(response, dict) and "error" in response:
            return f"Error starting quiz: {response['error']}"

        submission = _first_submission(response)
        if submission is None or submission.get("id") is None:
            return "Error starting quiz: Canvas did not return a quiz submission."

        submission_id = submission["id"]
        attempt = submission.get("attempt", 1)
        token = submission.get("validation_token")
        questions_response = await make_canvas_request(
            "get",
            f"/quiz_submissions/{submission_id}/questions",
            params={"attempt": attempt},
        )
        if isinstance(questions_response, dict) and "error" in questions_response:
            return f"Error loading quiz questions: {questions_response['error']}"

        questions = (
            questions_response.get("quiz_submission_questions")
            if isinstance(questions_response, dict)
            else questions_response
        )
        if not isinstance(questions, list):
            questions = []
        if isinstance(questions_response, dict):
            nested_submission = questions_response.get("quiz_submission") or {}
            token = nested_submission.get("validation_token") or token
            attempt = nested_submission.get("attempt", attempt)

        lines = [
            "✅ Quiz attempt started.",
            f"Quiz: {fence_untrusted_inline(str(quiz.get('title') or validated), 'quiz title')}",
            f"Quiz submission ID: {submission_id}",
            f"Attempt: {attempt}",
            f"Validation token: {token or '(none returned)'}",
            "",
            "Questions:",
        ]
        for index, question in enumerate(questions, 1):
            if not isinstance(question, dict):
                continue
            lines.append(
                f"{index}. [id {question.get('id', '?')}] "
                f"{fence_untrusted_inline(str(question.get('question_text') or question.get('question_name') or ''), 'quiz question')}"
            )
            if question.get("points_possible") is not None:
                lines.append(f"   Points: {question.get('points_possible')}")
            for answer in question.get("answers") or []:
                if isinstance(answer, dict):
                    lines.append(
                        f"   - id {answer.get('id', '?')}: "
                        f"{fence_untrusted_inline(str(answer.get('text') or ''), 'quiz answer option')}"
                    )
        lines.append("")
        lines.append(
            "Next: call answer_quiz_questions with quiz_submission_id, "
            "validation_token, attempt and your answers; then submit_quiz_attempt."
        )
        return "\n".join(lines)

    @mcp.tool()
    @validate_params
    async def answer_quiz_questions(
        course_identifier: str | int,
        quiz_id: str | int,
        quiz_submission_id: str | int,
        validation_token: str,
        attempt: int,
        answers: list[dict[str, Any]],
    ) -> str:
        """Save answers on a started Classic Quizzes attempt (opt-in).

        ``answers`` is a list of ``{"id": <question id>, "answer": <value>}``.
        This saves progress; it does not submit the attempt.

        Args:
            course_identifier: Course code or Canvas ID
            quiz_id: Canvas quiz ID
            quiz_submission_id: ID returned by start_quiz_attempt
            validation_token: Token returned by start_quiz_attempt
            attempt: Attempt number returned by start_quiz_attempt
            answers: Per-question answers
        """
        validated = coerce_canvas_id(quiz_id)
        submission_id = coerce_canvas_id(quiz_submission_id)
        if validated is None or submission_id is None:
            return "Error: quiz_id and quiz_submission_id must be numeric Canvas IDs."
        if not answers:
            return "Error: answers must contain at least one question id and answer."
        course_id = await get_course_id(course_identifier)
        quiz, error = await _load_quiz(course_id, str(validated))
        if quiz is None:
            return f"Error saving answers: {error}"
        if _engine(quiz) == "new_quizzes":
            return _new_quiz_refusal(quiz)
        blocked = await _write_blocked(course_id, "answer_quiz_questions")
        if blocked:
            return blocked

        data: list[tuple[str, Any]] = [
            ("attempt", str(attempt)),
            ("validation_token", validation_token),
        ]
        for entry in answers:
            if not isinstance(entry, dict) or entry.get("id") is None:
                return "Error: each answer needs an 'id' and an 'answer'."
            data.append(("quiz_questions[][id]", str(entry["id"])))
            data.append(("quiz_questions[][answer]", str(entry.get("answer", ""))))

        response = await make_canvas_request(
            "put",
            f"/quiz_submissions/{submission_id}/questions",
            data=data,
            use_form_data=True,
        )
        if isinstance(response, dict) and "error" in response:
            return f"Error saving answers: {response['error']}"
        updated = _first_submission(response)
        new_token = (updated or {}).get("validation_token") or validation_token
        return (
            f"✅ Saved {len(answers)} answer(s) for attempt {attempt}.\n"
            f"Validation token: {new_token}\n"
            "Nothing is submitted yet. Call submit_quiz_attempt when ready."
        )

    @mcp.tool()
    @validate_params
    async def submit_quiz_attempt(
        course_identifier: str | int,
        quiz_id: str | int,
        quiz_submission_id: str | int,
        validation_token: str,
        attempt: int,
    ) -> str:
        """Submit a started Classic Quizzes attempt (opt-in; graded).

        Requires CANVAS_ENABLE_QUIZ_TAKING=true. This finishes the attempt and
        cannot be undone.

        Args:
            course_identifier: Course code or Canvas ID
            quiz_id: Canvas quiz ID
            quiz_submission_id: ID returned by start_quiz_attempt
            validation_token: Current validation token
            attempt: Attempt number
        """
        validated = coerce_canvas_id(quiz_id)
        submission_id = coerce_canvas_id(quiz_submission_id)
        if validated is None or submission_id is None:
            return "Error: quiz_id and quiz_submission_id must be numeric Canvas IDs."
        course_id = await get_course_id(course_identifier)
        quiz, error = await _load_quiz(course_id, str(validated))
        if quiz is None:
            return f"Error submitting quiz: {error}"
        if _engine(quiz) == "new_quizzes":
            return _new_quiz_refusal(quiz)
        blocked = await _write_blocked(course_id, "submit_quiz_attempt")
        if blocked:
            return blocked

        response = await make_canvas_request(
            "post",
            f"/quiz_submissions/{submission_id}/complete",
            data={"attempt": str(attempt), "validation_token": validation_token},
            use_form_data=True,
        )
        if isinstance(response, dict) and "error" in response:
            return f"Error submitting quiz: {response['error']}"
        submission = _first_submission(response) or {}
        state = submission.get("workflow_state") or "submitted"
        score = submission.get("score")
        lines = ["✅ Quiz attempt submitted.", f"Quiz submission ID: {submission_id}"]
        lines.append(f"State: {state}")
        if score is not None:
            lines.append(f"Score: {score}")
        if submission.get("finished_at"):
            lines.append(f"Finished: {format_date(submission['finished_at'])}")
        return "\n".join(lines)
