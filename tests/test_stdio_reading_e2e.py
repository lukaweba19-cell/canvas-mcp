"""A real stdio session against the installed server, reading real fixtures.

Everything else runs in-process. This starts the actual ``canvas-mcp-server``
entry point as a subprocess, points it at a local Canvas stub over a real
socket, and asks it to read a PDF and a PPTX the way a model would. No network,
no token beyond a dummy one.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fixtures import build_pptx, build_slide_pdf, build_text_pdf
from helpers import text_of
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]

COURSE = {"id": 225, "name": "Algorithms", "course_code": "CS225"}

PDF = build_text_pdf(
    [
        "Depth first search explores one branch fully.",
        "Breadth first search uses a queue.",
    ]
)
DECK = build_pptx(
    [{"text": "Greedy choice property", "notes": "Proof by exchange argument"}]
)

# A lecture deck Canvas stored as PDF: landscape pages, so it reads per slide.
SLIDE_PDF = build_slide_pdf(
    ["Graphs and trees", "Dijkstra relaxes edges", "Proof by exchange argument"]
)

FILE_META = [
    {"id": 501, "display_name": "lecture.pdf", "filename": "lecture.pdf", "size": len(PDF)},
    {"id": 502, "display_name": "deck.pptx", "filename": "deck.pptx", "size": len(DECK)},
    {"id": 503, "display_name": "slides.pdf", "filename": "slides.pdf", "size": len(SLIDE_PDF)},
]


class _CanvasStub(BaseHTTPRequestHandler):
    def _json(self, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _blob(self, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - stdlib handler naming
        path = self.path.split("?", 1)[0]
        base = self.server.blob_base  # type: ignore[attr-defined]
        blobs = {"/blob/501": PDF, "/blob/502": DECK, "/blob/503": SLIDE_PDF}
        if path in blobs:
            self._blob(blobs[path])
            return
        routes = {
            "/api/v1/users/self": {"id": 1, "name": "Student"},
            "/api/v1/courses": [COURSE],
            "/api/v1/courses/225": COURSE,
            "/api/v1/courses/225/files": FILE_META,
            "/api/v1/courses/225/modules": [],
            "/api/v1/courses/225/files/501": {
                **FILE_META[0],
                "url": base + "/blob/501",
            },
            "/api/v1/courses/225/files/502": {
                **FILE_META[1],
                "url": base + "/blob/502",
            },
            "/api/v1/courses/225/files/503": {
                **FILE_META[2],
                "url": base + "/blob/503",
            },
        }
        if path in routes:
            self._json(routes[path])
            return
        self._json({})

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def canvas_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CanvasStub)
    port = server.server_address[1]
    server.blob_base = f"http://127.0.0.1:{port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/api/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_real_stdio_server_reads_pdf_and_pptx(tmp_path, canvas_stub):
    session_file = tmp_path / "session.json"
    session_file.write_text(
        json.dumps(
            {
                "base_url": canvas_stub,
                "cookies": {
                    "_canvas_session": "e2e-session",
                    "_csrf_token": "e2e-csrf",
                },
                "csrf": "e2e-csrf",
            }
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.update(
        {
            "CANVAS_API_URL": canvas_stub,
            "CANVAS_SESSION_FILE": str(session_file),
            "CANVAS_AUTH_NO_BROWSER": "1",
            "CANVAS_ALLOW_INSECURE_HTTP": "true",
            "CANVAS_STUDENT_DIR": str(tmp_path / "student"),
            "MCP_SERVER_NAME": "canvas-api",
        }
    )
    params = StdioServerParameters(
        command="uv",
        # HTTP is the default transport now; this test is specifically the
        # stdio path, so it must ask for it explicitly.
        args=["run", "canvas-mcp-server", "--transport", "stdio"],
        env=env,
        cwd=str(ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            pdf = text_of(
                await session.call_tool(
                    "read_course_material",
                    {
                        "course_identifier": 225,
                        "file_id": 501,
                        "full_text": True,
                    },
                )
            )
            assert "Status: readable — " in pdf, pdf
            assert "Depth first search explores one branch fully." in pdf
            assert "Breadth first search uses a queue." in pdf

            deck = text_of(
                await session.call_tool(
                    "read_course_material",
                    {"course_identifier": 225, "file_id": 502, "full_text": True},
                )
            )
            assert "Greedy choice property" in deck
            assert "Proof by exchange argument" in deck

            index = text_of(
                await session.call_tool(
                    "read_course_material", {"course_identifier": 225, "file_id": 501}
                )
            )
            assert "Sections:" in index
            assert "Depth first search explores one branch fully." not in index

            # The same server reads a PDF slide deck slide by slide.
            deck = text_of(
                await session.call_tool(
                    "read_course_material",
                    {"course_identifier": 225, "file_id": 503, "full_text": True},
                )
            )
            assert "## Slide 1" in deck and "## Slide 3" in deck, deck
            assert "Graphs and trees" in deck
            assert "Proof by exchange argument" in deck

            slide = text_of(
                await session.call_tool(
                    "read_material_section",
                    {"course_identifier": 225, "file_id": 503, "section": "Slide 2"},
                )
            )
            assert "Dijkstra relaxes edges" in slide
            assert "Graphs and trees" not in slide
