"""Every tool path over the real HTTP transport, against a real fake Canvas.

The unit suite patches ``make_canvas_request`` with a route table, so it proves
tool logic but not the wire. This test starts the installed server as a
subprocess, points it at two real HTTP servers, and drives tools through an MCP
client over streamable HTTP:

* every ``/api/v1`` call must carry the session cookie (and writes a matching
  ``X-CSRF-Token``, or the fake answers 403);
* JSON answers carry Canvas's XSSI guard, so parsing is exercised for real;
* a read paginates across two pages via ``Link: rel="next"``;
* a download leaves the Canvas host for a second origin, which must receive no
  cookie;
* a text submission and a file upload complete Canvas's write flow;
* replacing the cookie with a login redirect produces the standard expired
  session error, and a re-captured session restores the same tool.

Chrome itself is the only piece not exercised: ``CANVAS_AUTH_NO_BROWSER`` is
set, and the "recovery" writes the session file a successful login would write.
"""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fixtures import build_text_pdf
from helpers import text_of
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
PDF = build_text_pdf(
    ["Cookie sessions reach Canvas.", "Downloads leave Canvas for storage."]
)

COURSE = {
    "id": 225,
    "name": "Algorithms",
    "course_code": "CS225",
    "syllabus_body": "",
    "enrollments": [
        {
            "type": "student",
            "computed_current_score": 91.5,
            "computed_final_score": 91.5,
            "computed_current_grade": "A",
        }
    ],
}
COURSE_TWO = {
    "id": 226,
    "name": "Databases",
    "course_code": "CS326",
    "syllabus_body": "",
    "enrollments": [
        {
            "type": "student",
            "computed_current_score": 88.0,
            "computed_final_score": 88.0,
            "computed_current_grade": "B+",
        }
    ],
}
RUBRIC = [
    {
        "id": 1,
        "description": "Clarity",
        "points": 5,
        "ratings": [
            {"id": 1, "description": "Clear", "points": 5},
            {"id": 2, "description": "Unclear", "points": 0},
        ],
    }
]
ASSIGNMENT = {
    "id": 42,
    "name": "Short answer",
    "description": "Explain a cookie session.",
    "due_at": "2026-12-01T00:00:00Z",
    "points_possible": 10,
    "submission_types": ["online_text_entry", "online_upload"],
    "allowed_extensions": ["txt"],
    "allowed_attempts": -1,
    "group_category_id": None,
    "rubric": RUBRIC,
    "rubric_settings": {"title": "Writing rubric", "points_possible": 5},
    "submission": {
        "id": 1,
        "attempt": 1,
        "workflow_state": "graded",
        "submitted_at": "2026-11-01T00:00:00Z",
        "graded_at": "2026-11-02T00:00:00Z",
        "score": 8,
    },
}
ASSIGNMENT_TWO = {**ASSIGNMENT, "id": 43, "name": "Second page assignment"}
FILE_INFO = {
    "id": 501,
    "display_name": "lecture.pdf",
    "filename": "lecture.pdf",
    "content-type": "application/pdf",
    "size": len(PDF),
}
PAGE = {
    "page_id": 1,
    "url": "week-1",
    "title": "Week 1",
    "body": "<p>Welcome to algorithms.</p>",
    "published": True,
    "front_page": True,
    "updated_at": "2026-11-01T00:00:00Z",
}
MODULE = {
    "id": 1,
    "name": "Module 1",
    "state": "completed",
    "items": [
        {
            "id": 1,
            "type": "Page",
            "title": "Week 1",
            "content_id": 1,
            "page_url": "week-1",
        }
    ],
}
TOPIC = {
    "id": 1,
    "title": "Introductions",
    "message": "<p>Say hi</p>",
    "discussion_subentry_count": 1,
    "published": True,
    "posted_at": "2026-11-01T00:00:00Z",
    "author": {"id": 1, "display_name": "Student"},
}
ENTRY = {
    "id": 1,
    "user_id": 1,
    "user_name": "Student",
    "message": "<p>Hello everyone</p>",
    "created_at": "2026-11-01T00:00:00Z",
    "rating_count": 0,
}
CONVERSATION = {
    "id": 1,
    "subject": "Welcome",
    "last_message": "Hi there",
    "workflow_state": "read",
    "unread_count": 0,
    "participants": [{"id": 1, "name": "Student"}],
    "messages": [
        {
            "id": 1,
            "body": "Hi there",
            "author_id": 1,
            "created_at": "2026-11-01T00:00:00Z",
        }
    ],
}
QUIZ_CLASSIC = {
    "id": 7,
    "title": "Algebra check",
    "description": "<p>Two short questions.</p>",
    "quiz_type": "assignment",
    "published": True,
    "due_at": "2026-11-25T00:00:00Z",
    "unlock_at": "2026-11-01T00:00:00Z",
    "lock_at": "2026-12-01T00:00:00Z",
    "time_limit": 30,
    "allowed_attempts": 2,
    "points_possible": 10,
    "question_count": 2,
    "scoring_policy": "keep_highest",
    "assignment_id": 42,
    "has_access_code": False,
}
QUIZ_NEW = {
    "id": 8,
    "title": "New Quizzes check",
    "quiz_type": "quizzes.next",
    "quiz_lti": True,
    "published": True,
    "due_at": "2026-11-28T00:00:00Z",
    "time_limit": 20,
    "allowed_attempts": 1,
    "points_possible": 5,
}
QUIZ_QUESTIONS = [
    {
        "id": 1,
        "question_name": "Addition",
        "question_text": "What is 2 + 2?",
        "question_type": "multiple_choice_question",
        "points_possible": 5,
        "answers": [
            {"id": 1, "text": "4"},
            {"id": 2, "text": "5"},
        ],
    },
    {
        "id": 2,
        "question_name": "Short answer",
        "question_text": "Name one cookie attribute.",
        "question_type": "short_answer_question",
        "points_possible": 5,
        "answers": [],
    },
]
QUIZ_ATTEMPT_COMPLETE = {
    "id": 899,
    "quiz_id": 7,
    "attempt": 1,
    "workflow_state": "complete",
    "score": 8.0,
    "started_at": "2026-11-01T10:00:00Z",
    "finished_at": "2026-11-01T10:20:00Z",
}

SUBMISSION = {
    "id": 1,
    "user_id": 1,
    "attempt": 1,
    "workflow_state": "graded",
    "body": "My answer",
    "submitted_at": "2026-11-01T00:00:00Z",
    "graded_at": "2026-11-02T00:00:00Z",
    "score": 8,
    "grade": "8",
    "submission_comments": [{"comment": "Nice work"}],
    "rubric_assessment": {"1": {"points": 4, "comments": "Clear"}},
}


class FakeState:
    def __init__(self) -> None:
        self.cookie = "session-a"
        self.csrf = "csrf-a"
        self.reject = False
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.storage_cookies: list[str] = []
        self.storage_uploads = 0
        self.submissions: list[dict[str, list[str]]] = []
        self.canvas_base = ""
        self.storage_base = ""
        self.item_done = False
        self.quiz_attempts = [dict(QUIZ_ATTEMPT_COMPLETE)]
        self.quiz_started = False
        self.quiz_answers: dict[str, list[str]] = {}
        self.quiz_submitted = False


class _CanvasHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    @property
    def state(self) -> FakeState:
        return self.server.state

    def _headers(self) -> dict[str, str]:
        return {key.lower(): value for key, value in self.headers.items()}

    def _record(self, method: str, path: str) -> dict[str, str]:
        headers = self._headers()
        self.state.requests.append((method, path, headers))
        return headers

    def _send(
        self,
        body: bytes,
        status: int,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(
        self, payload, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        body = b"while(1);" + json.dumps(payload).encode()
        self._send(body, status, "application/json", headers)

    def _login(self) -> None:
        self._send(b"", 302, "text/html", {"Location": "/login"})

    def _authorized(self) -> bool:
        cookies = {}
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                cookies[key] = value
        return (
            not self.state.reject
            and cookies.get("_canvas_session") == self.state.cookie
        )

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        self._record("GET", self.path)

        if not self._authorized():
            self._login()
            return

        if path == "/api/v1/courses":
            page = query.get("page", ["1"])[0]
            if page == "1":
                link = f'<{self.state.canvas_base}/api/v1/courses?page=2>; rel="next"'
                self._json([COURSE], headers={"Link": link})
            else:
                self._json([COURSE_TWO])
            return

        if path == "/api/v1/courses/225":
            self._json(COURSE)
            return

        if path == "/api/v1/courses/225/assignments":
            page = query.get("page", ["1"])[0]
            if page == "1":
                link = (
                    f"<{self.state.canvas_base}/api/v1/courses/225/assignments"
                    f'?page=2>; rel="next"'
                )
                self._json([ASSIGNMENT], headers={"Link": link})
            else:
                self._json([ASSIGNMENT_TWO])
            return

        if path == "/api/v1/courses/226/assignments":
            self._json([])
            return

        if path == "/api/v1/courses/225/assignments/42":
            self._json(ASSIGNMENT)
            return
        if path == "/api/v1/courses/225/assignments/43":
            self._json(ASSIGNMENT_TWO)
            return

        if path == "/api/v1/courses/225/quizzes":
            self._json([QUIZ_CLASSIC, QUIZ_NEW])
            return
        if path == "/api/v1/courses/226/quizzes":
            self._json([])
            return
        if path == "/api/v1/courses/225/quizzes/7":
            self._json(QUIZ_CLASSIC)
            return
        if path == "/api/v1/courses/225/quizzes/8":
            self._json(QUIZ_NEW)
            return
        if path == "/api/v1/courses/225/quizzes/7/submissions":
            self._json(self.state.quiz_attempts)
            return
        if path == "/api/v1/quiz_submissions/900/questions":
            self._json(
                {
                    "quiz_submission_questions": QUIZ_QUESTIONS,
                    "quiz_submission": {
                        "id": 900,
                        "attempt": 2,
                        "validation_token": "quiz-token",
                        "workflow_state": "untaken",
                    },
                }
            )
            return

        if path == "/api/v1/courses/225/pages":
            self._json([PAGE])
            return

        if path in ("/api/v1/courses/225/pages/week-1", "/api/v1/courses/225/pages/1"):
            self._json(PAGE)
            return

        if path == "/api/v1/courses/225/front_page":
            self._json(PAGE)
            return

        if path == "/api/v1/courses/225/modules":
            self._json([MODULE])
            return

        if path == "/api/v1/courses/225/modules/1":
            self._json(MODULE)
            return

        module_item = {
            "id": 1,
            "type": "Page",
            "title": "Week 1",
            "content_id": 1,
            "page_url": "week-1",
            "completion_requirement": {
                "type": "must_mark_done",
                "completed": self.state.item_done,
            },
        }
        if path == "/api/v1/courses/225/modules/1/items":
            self._json([module_item])
            return
        if path == "/api/v1/courses/225/modules/1/items/1":
            self._json(module_item)
            return

        if path == "/api/v1/courses/225/discussion_topics":
            self._json([TOPIC])
            return
        if path == "/api/v1/courses/225/discussion_topics/1":
            self._json(TOPIC)
            return
        if path == "/api/v1/courses/225/discussion_topics/1/entries":
            self._json([ENTRY])
            return
        if path == "/api/v1/courses/225/discussion_topics/1/entries/1/replies":
            self._json([{**ENTRY, "id": 2, "message": "<p>A reply</p>"}])
            return
        if path == "/api/v1/courses/225/discussion_topics/1/entry_list":
            self._json([ENTRY])
            return
        if path == "/api/v1/courses/225/discussion_topics/1/view":
            self._json(
                {
                    "participants": [{"id": 1, "display_name": "Student"}],
                    "unread_entries": [],
                    "entry_ratings": {},
                }
            )
            return

        if path == "/api/v1/conversations":
            self._json([CONVERSATION])
            return
        if path == "/api/v1/conversations/unread_count":
            self._json({"unread_count": 0})
            return
        if path == "/api/v1/conversations/1":
            self._json(CONVERSATION)
            return

        if path == "/api/v1/courses/225/assignments/42/submissions/1":
            self._json(SUBMISSION)
            return
        if path == "/api/v1/courses/225/rubrics/1":
            self._json(
                {
                    "id": 1,
                    "title": "Writing rubric",
                    "data": RUBRIC,
                    "criteria": RUBRIC,
                    "assessments": [],
                    "associations": [],
                }
            )
            return

        if path == "/api/v1/users/self":
            self._json({"id": 1, "name": "Student", "login_id": "student@example.edu"})
            return
        if path == "/api/v1/users/self/profile":
            self._json(
                {
                    "id": 1,
                    "name": "Student",
                    "short_name": "Student",
                    "login_id": "student@example.edu",
                    "primary_email": "student@example.edu",
                }
            )
            return

        if path == "/api/v1/planner/items":
            self._json(
                [
                    {
                        "course_id": 225,
                        "plannable_type": "assignment",
                        "plannable_id": 42,
                        "plannable_date": "2026-11-20T00:00:00Z",
                        "plannable": {
                            "id": 42,
                            "title": "Short answer",
                            "due_at": "2026-11-20T00:00:00Z",
                            "points_possible": 10,
                        },
                        "submissions": {"submitted": False},
                    },
                    {
                        "course_id": 225,
                        "plannable_type": "quiz",
                        "plannable_id": 77,
                        "plannable_date": "2026-11-30T00:00:00Z",
                        "plannable": {
                            "id": 77,
                            "title": "Planner-only quiz",
                            "due_at": "2026-11-30T00:00:00Z",
                            "points_possible": 15,
                        },
                        "submissions": {"submitted": False},
                    },
                ]
            )
            return

        if path == "/api/v1/users/self/todo":
            self._json(
                [
                    {
                        "type": "submitting",
                        "course_id": 225,
                        "assignment": {
                            "id": 42,
                            "name": "Short answer",
                            "due_at": "2026-11-20T00:00:00Z",
                        },
                    }
                ]
            )
            return

        if path == "/api/v1/courses/225/assignment_groups":
            self._json(
                [
                    {
                        "id": 1,
                        "name": "Homework",
                        "group_weight": 100,
                        "assignments": [ASSIGNMENT],
                    }
                ]
            )
            return

        if path == "/api/v1/courses/225/assignments/43/peer_reviews":
            self._json([])
            return

        if path == "/api/v1/courses/225/assignments/42/peer_reviews":
            self._json(
                [
                    {
                        "id": 1,
                        "assessor_id": 1,
                        "asset_id": 42,
                        "workflow_state": "assigned",
                        "user": {"id": 1, "name": "Student"},
                    }
                ]
            )
            return

        if path == "/api/v1/files/900/confirm":
            self._json({"id": 900, "display_name": "answer.txt"})
            return

        if path == "/api/v1/courses/225/files":
            self._json([FILE_INFO])
            return

        if path in (
            "/api/v1/courses/225/assignments/42/submissions/self",
            "/api/v1/courses/225/assignments/43/submissions/self",
        ):
            self._json(SUBMISSION)
            return

        if path == "/api/v1/courses/225/files/501":
            self._json(
                {
                    **FILE_INFO,
                    "url": f"{self.state.canvas_base}/files/501/download",
                }
            )
            return

        if path == "/files/501/download":
            self._send(
                b"",
                302,
                "text/html",
                {"Location": f"{self.state.storage_base}/blob/501?sig=abc"},
            )
            return

        self._json({"error": f"unhandled GET {path}"}, status=404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        headers = self._record("POST", self.path)

        if not self._authorized():
            self._login()
            return

        if headers.get("x-csrf-token") != self.state.csrf:
            self._json({"error": "invalid CSRF token"}, status=403)
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        content_type = headers.get("content-type", "")
        form = (
            parse_qs(raw.decode("utf-8", errors="replace"))
            if content_type.startswith("application/x-www-form-urlencoded")
            else {}
        )

        if path == "/api/v1/courses/225/assignments/42/submissions":
            self.state.submissions.append(form)
            self._json(
                {
                    "id": 1,
                    "attempt": 1,
                    "workflow_state": "submitted",
                    "submitted_at": "2026-11-14T12:00:00Z",
                }
            )
            return

        if path == "/api/v1/courses/225/assignments/42/submissions/self/files":
            self._json(
                {
                    "upload_url": f"{self.state.storage_base}/upload",
                    "upload_params": {"key": "submissions/answer.txt"},
                }
            )
            return

        if path == "/api/v1/courses/225/discussion_topics/1/entries":
            self._json({**ENTRY, "id": 3, "message": "Hello everyone"})
            return
        if path == "/api/v1/courses/225/discussion_topics/1/entries/1/replies":
            self._json({**ENTRY, "id": 4, "message": "A reply"})
            return
        if path == "/api/v1/courses/225/rubric_associations":
            self._json({"id": 1})
            return

        if path == "/api/v1/courses/225/quizzes/7/submissions":
            attempt = {
                "id": 900,
                "quiz_id": 7,
                "attempt": 2,
                "workflow_state": "untaken",
                "score": None,
                "validation_token": "quiz-token",
            }
            self.state.quiz_started = True
            self.state.quiz_attempts.append(attempt)
            self._json({"quiz_submissions": [attempt]})
            return

        if path == "/api/v1/quiz_submissions/900/complete":
            self.state.quiz_submitted = True
            for attempt in self.state.quiz_attempts:
                if attempt.get("id") == 900:
                    attempt["workflow_state"] = "complete"
                    attempt["score"] = 10.0
                    attempt["finished_at"] = "2026-11-14T13:00:00Z"
                    self._json({"quiz_submissions": [attempt]})
                    return
            self._json({"quiz_submissions": []})
            return

        self._json({"error": f"unhandled POST {path}"}, status=404)

    def do_PUT(self) -> None:
        path = urlsplit(self.path).path
        headers = self._record("PUT", self.path)

        if not self._authorized():
            self._login()
            return

        if headers.get("x-csrf-token") != self.state.csrf:
            self._json({"error": "invalid CSRF token"}, status=403)
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        content_type = headers.get("content-type", "")
        form = (
            parse_qs(raw.decode("utf-8", errors="replace"))
            if content_type.startswith("application/x-www-form-urlencoded")
            else {}
        )

        if path == "/api/v1/quiz_submissions/900/questions":
            self.state.quiz_answers = form
            self._json(
                {
                    "quiz_submissions": [
                        {
                            "id": 900,
                            "attempt": 2,
                            "validation_token": "quiz-token",
                            "workflow_state": "untaken",
                        }
                    ]
                }
            )
            return

        if path == "/api/v1/courses/225/assignments/42/submissions/self":
            self._json(
                {
                    **SUBMISSION,
                    "submission_comments": [{"comment": "A comment"}],
                }
            )
            return
        if path == "/api/v1/courses/225/modules/1/items/1/done":
            self.state.item_done = True
            self._json({})
            return
        if path == "/api/v1/conversations":
            self._json({})
            return

        self._json({"error": f"unhandled PUT {path}"}, status=404)


class _StorageHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    @property
    def state(self) -> FakeState:
        return self.server.state

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        self.state.storage_cookies.append(self.headers.get("Cookie", ""))
        if path == "/blob/501":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(PDF)))
            self.end_headers()
            self.wfile.write(PDF)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        self.state.storage_cookies.append(self.headers.get("Cookie", ""))
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.state.storage_uploads += 1
        confirm = f"{self.state.canvas_base}/api/v1/files/900/confirm"
        self.send_response(303)
        self.send_header("Location", confirm)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _start(handler, state: FakeState):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", thread


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _write_session(path: Path, base: str, cookie: str, csrf: str) -> None:
    path.write_text(
        json.dumps(
            {
                "base_url": base,
                "cookies": {"_canvas_session": cookie, "_csrf_token": csrf},
                "csrf": csrf,
                "saved_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def _token(preview: str) -> str:
    match = re.search(r"confirmation_token='([^']+)'", preview)
    assert match, preview
    return match.group(1)


async def test_every_path_over_real_http_transport(tmp_path):
    state = FakeState()
    storage_server, storage_base, storage_thread = _start(_StorageHandler, state)
    canvas_server, canvas_base, canvas_thread = _start(_CanvasHandler, state)
    state.canvas_base = canvas_base
    state.storage_base = storage_base

    session_file = tmp_path / "session.json"
    _write_session(session_file, canvas_base + "/api/v1", state.cookie, state.csrf)

    port = _free_port()
    server_log = tmp_path / "server.log"
    env = dict(os.environ)
    env.update(
        {
            "CANVAS_API_URL": canvas_base + "/api/v1",
            "CANVAS_SESSION_FILE": str(session_file),
            "CANVAS_AUTH_NO_BROWSER": "1",
            "CANVAS_ALLOW_INSECURE_HTTP": "true",
            "CANVAS_STUDENT_DIR": str(tmp_path / "student"),
            "STUDENT_WRITE_TOOLS": "submit_assignment,mark_module_item_done",
            "LOG_LEVEL": "WARNING",
        }
    )
    log_handle = server_log.open("w")
    process = subprocess.Popen(
        [
            "uv",
            "run",
            "canvas-mcp-server",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    try:
        if not _wait_for_port(port):
            raise AssertionError(
                "server never listened; log:\n" + server_log.read_text(errors="replace")
            )

        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (
            read,
            write,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()

                listing = text_of(
                    await session.call_tool(
                        "list_assignments", {"course_identifier": 225}
                    )
                )
                assert "Short answer" in listing, listing
                assert "Second page assignment" in listing, listing

                courses = text_of(await session.call_tool("list_courses", {}))
                assert "CS225" in courses and "CS326" in courses, courses

                details = text_of(
                    await session.call_tool(
                        "get_course_details", {"course_identifier": 225}
                    )
                )
                assert "Algorithms" in details, details

                overview = text_of(
                    await session.call_tool(
                        "get_course_content_overview", {"course_identifier": 225}
                    )
                )
                assert "Module 1" in overview and "Week 1" in overview, overview

                content_map = text_of(
                    await session.call_tool(
                        "get_course_content_map", {"course_identifier": 225}
                    )
                )
                assert "lecture.pdf" in content_map, content_map

                direct = text_of(
                    await session.call_tool(
                        "read_course_file",
                        {"course_identifier": 225, "file_id": 501},
                    )
                )
                assert "Encoding: base64" in direct, direct

                material = text_of(
                    await session.call_tool(
                        "read_course_material",
                        {"course_identifier": 225, "file_id": 501, "full_text": True},
                    )
                )
                assert "Cookie sessions reach Canvas." in material, material
                assert "Downloads leave Canvas for storage." in material, material
                assert state.storage_cookies, "storage was never asked for the file"
                assert all(
                    cookie == "" for cookie in state.storage_cookies
                ), state.storage_cookies

                body = "Sessions are cookies, not bearer tokens."
                preview = text_of(
                    await session.call_tool(
                        "submit_assignment",
                        {
                            "course_identifier": 225,
                            "assignment_id": 42,
                            "submission_type": "online_text_entry",
                            "body": body,
                        },
                    )
                )
                assert "NOTHING has been submitted" in preview, preview
                done = text_of(
                    await session.call_tool(
                        "submit_assignment",
                        {
                            "course_identifier": 225,
                            "assignment_id": 42,
                            "submission_type": "online_text_entry",
                            "body": body,
                            "confirmation_token": _token(preview),
                        },
                    )
                )
                assert "Submitted." in done, done
                assert state.submissions[-1]["submission[body]"] == [body]
                assert state.submissions[-1]["submission[submission_type]"] == [
                    "online_text_entry"
                ]

                encoded = base64.b64encode(b"A cookie session upload.").decode()
                preview = text_of(
                    await session.call_tool(
                        "submit_assignment",
                        {
                            "course_identifier": 225,
                            "assignment_id": 42,
                            "submission_type": "online_upload",
                            "file_contents": [
                                {"name": "answer.txt", "content_base64": encoded}
                            ],
                        },
                    )
                )
                assert "answer.txt" in preview, preview
                done = text_of(
                    await session.call_tool(
                        "submit_assignment",
                        {
                            "course_identifier": 225,
                            "assignment_id": 42,
                            "submission_type": "online_upload",
                            "file_contents": [
                                {"name": "answer.txt", "content_base64": encoded}
                            ],
                            "confirmation_token": _token(preview),
                        },
                    )
                )
                assert "Submitted." in done, done
                assert state.storage_uploads == 1
                assert state.submissions[-1]["submission[file_ids][]"] == ["900"]
                assert state.storage_cookies[-1] == ""

                assert state.requests, "no Canvas requests reached the fake"
                assert all(
                    "_canvas_session=" in headers.get("cookie", "")
                    for _, _, headers in state.requests
                )
                assert all(
                    "authorization" not in headers for _, _, headers in state.requests
                ), "a bearer header reached Canvas"
                writes = [
                    (path, headers)
                    for method, path, headers in state.requests
                    if method in ("POST", "PUT", "DELETE") and "/api/v1/" in path
                ]
                assert writes
                assert all(
                    headers.get("x-csrf-token") == state.csrf for _, headers in writes
                )

                state.reject = True
                expired = text_of(
                    await session.call_tool(
                        "list_assignments", {"course_identifier": 225}
                    )
                )
                assert "session expired" in expired.lower(), expired
                assert "Chrome login window" in expired, expired

                blocked = text_of(
                    await session.call_tool(
                        "list_assignments", {"course_identifier": 225}
                    )
                )
                assert "No assignments found" not in blocked, blocked
                assert "session" in blocked.lower(), blocked

                _write_session(
                    session_file, canvas_base + "/api/v1", "session-b", "csrf-b"
                )
                state.cookie, state.csrf, state.reject = "session-b", "csrf-b", False
                recovered = text_of(
                    await session.call_tool(
                        "list_assignments", {"course_identifier": 225}
                    )
                )
                assert "Short answer" in recovered, recovered

                state.reject = True
                download_expired = text_of(
                    await session.call_tool(
                        "read_course_material",
                        {
                            "course_identifier": 225,
                            "file_id": 501,
                            "refresh": True,
                        },
                    )
                )
                assert "session expired" in download_expired.lower(), download_expired

                _write_session(
                    session_file, canvas_base + "/api/v1", "session-c", "csrf-c"
                )
                state.cookie, state.csrf, state.reject = "session-c", "csrf-c", False
                recovered_download = text_of(
                    await session.call_tool(
                        "read_course_material",
                        {
                            "course_identifier": 225,
                            "file_id": 501,
                            "refresh": True,
                            "full_text": True,
                        },
                    )
                )
                assert "Cookie sessions reach Canvas." in recovered_download
    finally:
        if os.name == "nt":
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        log_handle.close()
        for server, thread in (
            (canvas_server, canvas_thread),
            (storage_server, storage_thread),
        ):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _tool_calls(tmp_path: Path) -> dict[str, list[dict]]:
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    return {
        "list_courses": [{}],
        "get_course_details": [{"course_identifier": 225}],
        "get_syllabus": [{"course_identifier": 225}],
        "get_course_content_overview": [{"course_identifier": 225}],
        "list_pages": [{"course_identifier": 225}],
        "get_page_content": [{"course_identifier": 225, "page_url_or_id": "week-1"}],
        "get_page_details": [{"course_identifier": 225, "page_url_or_id": "week-1"}],
        "get_front_page": [{"course_identifier": 225}],
        "list_module_items": [{"course_identifier": 225, "module_id": 1}],
        "list_assignments": [{"course_identifier": 225}],
        "get_assignment_details": [{"course_identifier": 225, "assignment_id": 42}],
        "list_discussion_topics": [{"course_identifier": 225}],
        "list_announcements": [{"course_identifier": 225}],
        "get_discussion_topic_details": [{"course_identifier": 225, "topic_id": 1}],
        "list_discussion_entries": [{"course_identifier": 225, "topic_id": 1}],
        "get_discussion_entry_details": [
            {"course_identifier": 225, "topic_id": 1, "entry_id": 1}
        ],
        "get_discussion_with_replies": [{"course_identifier": 225, "topic_id": 1}],
        "post_discussion_entry": [
            {"course_identifier": 225, "topic_id": 1, "message": "Hello everyone"}
        ],
        "reply_to_discussion_entry": [
            {
                "course_identifier": 225,
                "topic_id": 1,
                "entry_id": 1,
                "message": "A reply",
            }
        ],
        "list_modules": [{"course_identifier": 225}],
        "get_course_structure": [{"course_identifier": 225}],
        "download_course_file": [
            {
                "course_identifier": 225,
                "file_id": 501,
                "save_directory": str(download_dir),
            }
        ],
        "read_course_file": [{"course_identifier": 225, "file_id": 501}],
        "list_course_files": [{"course_identifier": 225}],
        "list_conversations": [{}],
        "get_conversation_details": [{"conversation_id": 1}],
        "get_unread_count": [{}],
        "mark_conversations_read": [{"conversation_ids": ["1"]}],
        "get_rubric": [{"course_identifier": 225, "assignment_id": 42}],
        "get_rubric_assessment": [
            {"course_identifier": 225, "assignment_id": 42, "user_id": 1}
        ],
        "get_my_assignment_rubric": [{"course_identifier": 225, "assignment_id": 42}],
        "check_work_against_rubric": [
            {
                "course_identifier": 225,
                "assignment_id": 42,
                "text": "A clear answer.",
            }
        ],
        "search_canvas_tools": [{"query": "assignment"}],
        "get_my_enrollments": [{}],
        "get_my_profile": [{}],
        "get_my_upcoming_assignments": [{"days": 14}],
        "get_my_submission_status": [{}],
        "get_my_course_grades": [{}],
        "get_my_todo_items": [{}],
        "get_my_peer_reviews_todo": [{}],
        "get_my_assignment_brief": [{"course_identifier": 225, "assignment_id": 42}],
        "get_my_grade_breakdown": [{"course_identifier": 225}],
        "get_my_week_plan": [{"days": 14}],
        "get_my_feedback": [{"course_identifier": 225}],
        "get_my_submission": [{"course_identifier": 225, "assignment_id": 42}],
        "submit_assignment": [
            {
                "course_identifier": 225,
                "assignment_id": 42,
                "submission_type": "online_text_entry",
                "body": "Matrix answer",
            }
        ],
        "comment_on_my_submission": [
            {
                "course_identifier": 225,
                "assignment_id": 42,
                "comment": "A comment",
            }
        ],
        "mark_module_item_done": [
            {"course_identifier": 225, "module_id": 1, "item_id": 1}
        ],
        "get_course_content_map": [{"course_identifier": 225}],
        "read_course_material": [
            {"course_identifier": 225, "file_id": 501, "full_text": True}
        ],
        "read_material_section": [
            {"course_identifier": 225, "file_id": 501, "section": "p.1"}
        ],
        "read_page_text": [{"course_identifier": 225, "page_url_or_id": "week-1"}],
        "build_study_guide": [
            {"course_identifier": 225, "title": "Guide", "markdown": "# Guide"}
        ],
        "update_study_guide": [
            {
                "course_identifier": 225,
                "title": "Guide",
                "markdown": "More",
                "mode": "append",
            }
        ],
        "list_study_guides": [{"course_identifier": 225}],
        "read_study_guide": [{"course_identifier": 225, "title": "Guide"}],
        "make_practice_test": [
            {"course_identifier": 225, "title": "Practice", "markdown": "Q1"}
        ],
        "start_assignment_work": [{"course_identifier": 225, "assignment_id": 42}],
        "save_assignment_draft": [
            {
                "course_identifier": 225,
                "assignment_id": 42,
                "content": "Draft body",
            }
        ],
        "read_assignment_draft": [
            {"course_identifier": 225, "assignment_id": 42, "version": 0}
        ],
        "list_assignment_drafts": [{"course_identifier": 225}],
        "save_note": [{"title": "Matrix note", "content": "Note body"}],
        "search_notes": [{"query": "Note"}],
        "list_notes": [{}],
        "read_note": [{"note_id": "Matrix"}],
        "add_work_log_entry": [{"summary": "Did the work"}],
        "get_my_handoff": [{}],
        "save_voice_sample": [
            {
                "text": "This is my own writing voice and these are my own words in a sample."
            }
        ],
        "check_draft_style": [{"text": "A clear and simple sentence about cookies."}],
        "list_quizzes": [{}, {"course_identifier": 225}],
        "get_quiz_details": [{"course_identifier": 225, "quiz_id": 7}],
        "get_my_quiz_attempts": [{"course_identifier": 225, "quiz_id": 7}],
        "start_quiz_attempt": [{"course_identifier": 225, "quiz_id": 7}],
        "answer_quiz_questions": [
            {
                "course_identifier": 225,
                "quiz_id": 7,
                "quiz_submission_id": 900,
                "validation_token": "quiz-token",
                "attempt": 2,
                "answers": [
                    {"id": 1, "answer": "4"},
                    {"id": 2, "answer": "HTTPOnly"},
                ],
            }
        ],
        "submit_quiz_attempt": [
            {
                "course_identifier": 225,
                "quiz_id": 7,
                "quiz_submission_id": 900,
                "validation_token": "quiz-token",
                "attempt": 2,
            }
        ],
    }


LOCAL_TOOLS = frozenset(
    {
        "search_canvas_tools",
        "read_material_section",
        "build_study_guide",
        "update_study_guide",
        "list_study_guides",
        "read_study_guide",
        "make_practice_test",
        "save_assignment_draft",
        "read_assignment_draft",
        "list_assignment_drafts",
        "save_note",
        "search_notes",
        "list_notes",
        "read_note",
        "add_work_log_entry",
        "get_my_handoff",
        "save_voice_sample",
        "check_draft_style",
        "check_work_against_rubric",
    }
)


async def test_every_registered_tool_over_real_http_transport(tmp_path):
    state = FakeState()
    storage_server, storage_base, storage_thread = _start(_StorageHandler, state)
    canvas_server, canvas_base, canvas_thread = _start(_CanvasHandler, state)
    state.canvas_base = canvas_base
    state.storage_base = storage_base

    session_file = tmp_path / "session.json"
    _write_session(session_file, canvas_base + "/api/v1", state.cookie, state.csrf)

    port = _free_port()
    server_log = tmp_path / "server.log"
    env = dict(os.environ)
    env.update(
        {
            "CANVAS_API_URL": canvas_base + "/api/v1",
            "CANVAS_SESSION_FILE": str(session_file),
            "CANVAS_AUTH_NO_BROWSER": "1",
            "CANVAS_ALLOW_INSECURE_HTTP": "true",
            "CANVAS_STUDENT_DIR": str(tmp_path / "student"),
            "STUDENT_WRITE_TOOLS": (
                "submit_assignment,comment_on_my_submission,mark_module_item_done"
            ),
            "CANVAS_ENABLE_QUIZ_TAKING": "true",
            "LOG_LEVEL": "WARNING",
        }
    )
    log_handle = server_log.open("w")
    process = subprocess.Popen(
        [
            "uv",
            "run",
            "canvas-mcp-server",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    try:
        if not _wait_for_port(port):
            raise AssertionError(
                "server never listened; log:\n" + server_log.read_text(errors="replace")
            )

        calls = _tool_calls(tmp_path)
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (
            read,
            write,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()

                registered = {tool.name for tool in (await session.list_tools()).tools}
                assert registered == set(calls), (
                    "tool registry and e2e call table differ; "
                    f"uninvoked={sorted(registered - set(calls))}, "
                    f"unknown={sorted(set(calls) - registered)}"
                )

                id_keys = {
                    "course_identifier",
                    "assignment_id",
                    "assignment_identifier",
                    "module_id",
                    "item_id",
                    "topic_id",
                    "entry_id",
                    "file_id",
                    "conversation_id",
                    "user_id",
                    "rubric_id",
                    "quiz_submission_id",
                }

                def wire(raw_args: dict) -> dict:
                    return {
                        key: str(value) if key in id_keys else value
                        for key, value in raw_args.items()
                    }

                outputs: dict[str, list[str]] = {}
                for name, invocations in calls.items():
                    before = len(state.requests)
                    for raw_args in invocations:
                        args = wire(raw_args)
                        result = await session.call_tool(name, args)
                        text = text_of(result)
                        outputs.setdefault(name, []).append(text)
                        assert not result.is_error, f"{name}: {text}"
                        assert "session expired" not in text.lower(), f"{name}: {text}"
                        assert not text.lstrip().startswith(
                            ("Error", "❌")
                        ), f"{name}: {text}"

                        if name == "submit_assignment":
                            token = _token(text)
                            done = text_of(
                                await session.call_tool(
                                    name, {**args, "confirmation_token": token}
                                )
                            )
                            assert "Submitted." in done, done
                    made = state.requests[before:]
                    if name not in LOCAL_TOOLS:
                        assert made, f"{name} made no Canvas request"

                all_quizzes = outputs["list_quizzes"][0]
                assert "Algebra check" in all_quizzes, all_quizzes
                assert "Planner-only quiz" in all_quizzes, all_quizzes
                assert "Time limit: 30 minutes" in all_quizzes, all_quizzes
                assert "score 8" in all_quizzes, all_quizzes
                assert "OUTSTANDING" in all_quizzes, all_quizzes
                assert "New Quizzes" in all_quizzes, all_quizzes
                course_quizzes = outputs["list_quizzes"][1]
                assert "Algebra check" in course_quizzes, course_quizzes
                details = outputs["get_quiz_details"][0]
                assert "Questions: 2" in details, details
                assert "Time limit: 30 minutes" in details, details
                attempts = outputs["get_my_quiz_attempts"][0]
                assert "attempt 1" in attempts and "score 8" in attempts, attempts

                started = outputs["start_quiz_attempt"][0]
                assert "Quiz submission ID: 900" in started, started
                assert "quiz-token" in started, started
                assert "What is 2 + 2?" in started, started
                assert state.quiz_answers["quiz_questions[][id]"] == ["1", "2"]
                assert state.quiz_answers["quiz_questions[][answer]"] == [
                    "4",
                    "HTTPOnly",
                ]
                assert "Score: 10" in outputs["submit_quiz_attempt"][0]
                assert state.quiz_submitted

                unsupported = text_of(
                    await session.call_tool(
                        "start_quiz_attempt",
                        {"course_identifier": "225", "quiz_id": "8"},
                    )
                )
                assert "New Quizzes" in unsupported, unsupported
                assert "does not expose" in unsupported, unsupported

                assert state.requests
                for method, path, headers in state.requests:
                    if "/api/v1/" not in path:
                        continue
                    assert "_canvas_session=" in headers.get("cookie", ""), path
                    assert "authorization" not in headers, path
                    if method in ("POST", "PUT", "DELETE", "PATCH"):
                        assert headers.get("x-csrf-token") == state.csrf, path

                state.reject = True
                for name in calls:
                    if name in LOCAL_TOOLS:
                        continue
                    _write_session(
                        session_file, canvas_base + "/api/v1", state.cookie, state.csrf
                    )
                    raw_args = dict(calls[name][0])
                    if name == "read_course_material":
                        raw_args["refresh"] = True
                    text = text_of(await session.call_tool(name, wire(raw_args)))
                    assert "session expired" in text.lower(), f"{name}: {text}"
                state.reject = False

                assert state.storage_cookies
                assert all(cookie == "" for cookie in state.storage_cookies)
    finally:
        if os.name == "nt":
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        log_handle.close()
        for server, thread in (
            (canvas_server, canvas_thread),
            (storage_server, storage_thread),
        ):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


async def test_quiz_taking_is_off_by_default(tmp_path):
    """Reading quizzes works without the flag; taking refuses with guidance."""
    state = FakeState()
    storage_server, storage_base, storage_thread = _start(_StorageHandler, state)
    canvas_server, canvas_base, canvas_thread = _start(_CanvasHandler, state)
    state.canvas_base = canvas_base
    state.storage_base = storage_base

    session_file = tmp_path / "session.json"
    _write_session(session_file, canvas_base + "/api/v1", state.cookie, state.csrf)

    port = _free_port()
    server_log = tmp_path / "server.log"
    env = dict(os.environ)
    env.pop("CANVAS_ENABLE_QUIZ_TAKING", None)
    env.update(
        {
            "CANVAS_API_URL": canvas_base + "/api/v1",
            "CANVAS_SESSION_FILE": str(session_file),
            "CANVAS_AUTH_NO_BROWSER": "1",
            "CANVAS_ALLOW_INSECURE_HTTP": "true",
            "CANVAS_STUDENT_DIR": str(tmp_path / "student"),
            "LOG_LEVEL": "WARNING",
        }
    )
    log_handle = server_log.open("w")
    process = subprocess.Popen(
        [
            "uv",
            "run",
            "canvas-mcp-server",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    try:
        if not _wait_for_port(port):
            raise AssertionError(
                "server never listened; log:\n" + server_log.read_text(errors="replace")
            )
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (
            read,
            write,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()

                listed = text_of(
                    await session.call_tool(
                        "list_quizzes", {"course_identifier": "225"}
                    )
                )
                assert "Algebra check" in listed, listed

                refused = text_of(
                    await session.call_tool(
                        "start_quiz_attempt",
                        {"course_identifier": "225", "quiz_id": "7"},
                    )
                )
                assert "disabled" in refused.lower(), refused
                assert "CANVAS_ENABLE_QUIZ_TAKING" in refused, refused
                assert not [
                    request
                    for request in state.requests
                    if request[0] == "POST"
                    and request[1].endswith("/quizzes/7/submissions")
                ]
    finally:
        if os.name == "nt":
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        log_handle.close()
        for server, thread in (
            (canvas_server, canvas_thread),
            (storage_server, storage_thread),
        ):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
