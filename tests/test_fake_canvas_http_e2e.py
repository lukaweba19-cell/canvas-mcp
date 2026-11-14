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
}
COURSE_TWO = {
    "id": 226,
    "name": "Databases",
    "course_code": "CS326",
    "syllabus_body": "",
}
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
}
ASSIGNMENT_TWO = {**ASSIGNMENT, "id": 43, "name": "Second page assignment"}
FILE_INFO = {
    "id": 501,
    "display_name": "lecture.pdf",
    "filename": "lecture.pdf",
    "content-type": "application/pdf",
    "size": len(PDF),
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


class _CanvasHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # keep pytest output clean
        pass

    @property
    def state(self) -> FakeState:
        return self.server.state  # type: ignore[attr-defined]

    def _headers(self) -> dict[str, str]:
        return {key.lower(): value for key, value in self.headers.items()}

    def _record(self, method: str, path: str) -> dict[str, str]:
        headers = self._headers()
        self.state.requests.append((method, path, headers))
        return headers

    def _send(self, body: bytes, status: int, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200, headers: dict[str, str] | None = None) -> None:
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
        return not self.state.reject and cookies.get("_canvas_session") == self.state.cookie

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
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
                    f'<{self.state.canvas_base}/api/v1/courses/225/assignments'
                    f'?page=2>; rel="next"'
                )
                self._json([ASSIGNMENT], headers={"Link": link})
            else:
                self._json([ASSIGNMENT_TWO])
            return

        if path == "/api/v1/courses/225/assignments/42":
            self._json(ASSIGNMENT)
            return

        if path == "/api/v1/courses/225/pages":
            self._json(
                [
                    {
                        "title": "Week 1",
                        "published": True,
                        "front_page": True,
                        "updated_at": "2026-11-01T00:00:00Z",
                    }
                ]
            )
            return

        if path == "/api/v1/courses/225/modules":
            self._json(
                [
                    {
                        "id": 1,
                        "name": "Module 1",
                        "state": "completed",
                        "items": [
                            {
                                "type": "Page",
                                "title": "Week 1",
                                "content_id": 1,
                            }
                        ],
                    }
                ]
            )
            return

        if path == "/api/v1/courses/225/modules/1/items":
            self._json(
                [{"type": "Page", "title": "Week 1", "content_id": 1}]
            )
            return

        if path == "/api/v1/files/900/confirm":
            self._json({"id": 900, "display_name": "answer.txt"})
            return

        if path == "/api/v1/courses/225/files":
            self._json([FILE_INFO])
            return

        if path == "/api/v1/courses/225/assignments/42/submissions/self":
            self._json({"attempt": 0, "workflow_state": "unsubmitted"})
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

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler naming
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

        self._json({"error": f"unhandled POST {path}"}, status=404)


class _StorageHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    @property
    def state(self) -> FakeState:
        return self.server.state  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
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

    def do_POST(self) -> None:  # noqa: N802
        self.state.storage_cookies.append(self.headers.get("Cookie", ""))
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.state.storage_uploads += 1
        # Canvas storage normally redirects to a Canvas confirmation endpoint,
        # which is the hop that must carry the session cookie and no CSRF.
        confirm = f"{self.state.canvas_base}/api/v1/files/900/confirm"
        self.send_response(303)
        self.send_header("Location", confirm)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _start(handler, state: FakeState):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.state = state  # type: ignore[attr-defined]
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

        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # --- read + pagination -------------------------------------
                listing = text_of(
                    await session.call_tool(
                        "list_assignments", {"course_identifier": 225}
                    )
                )
                assert "Short answer" in listing, listing
                assert "Second page assignment" in listing, listing

                # --- other read shapes: list, detail, overview, content map --
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

                # --- download via an off-host redirect ---------------------
                material = text_of(
                    await session.call_tool(
                        "read_course_material",
                        {"course_identifier": 225, "file_id": 501, "full_text": True},
                    )
                )
                assert "Cookie sessions reach Canvas." in material, material
                assert "Downloads leave Canvas for storage." in material, material
                assert state.storage_cookies, "storage was never asked for the file"
                assert all(cookie == "" for cookie in state.storage_cookies), (
                    state.storage_cookies
                )

                # --- write: text submission, preview then CSRF-protected POST
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

                # --- write: file upload through storage ---------------------
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

                # Every Canvas call carried the session cookie; every write
                # carried the CSRF token (the fake 403s otherwise).
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

                # --- expired session, then recovery ------------------------
                state.reject = True
                expired = text_of(
                    await session.call_tool(
                        "list_assignments", {"course_identifier": 225}
                    )
                )
                assert "session expired" in expired.lower(), expired
                assert "Chrome login window" in expired, expired

                # The session is gone and no browser is allowed here: the next
                # call must say so, not report an empty course.
                blocked = text_of(
                    await session.call_tool(
                        "list_assignments", {"course_identifier": 225}
                    )
                )
                assert "No assignments found" not in blocked, blocked
                assert "session" in blocked.lower(), blocked

                # What a successful Chrome login writes: a fresh session file.
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

                # A file download is a separate path (redirect off Canvas). It
                # must report the same expiry, not download the login page.
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
        # `uv run` wraps the server in a child process; on POSIX, terminate the
        # whole group so no server outlives the test. Windows has no process
        # groups here, so terminating the launcher is the available cleanup.
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
