"""Reading a course file for real: HTTP download, text out, honest refusals.

Unlike the other material tests, nothing here stubs the download: the bytes come
from a local HTTP server over a real socket, so the whole tool path is exercised.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fixtures import build_pptx, build_slide_pdf, build_text_pdf
from helpers import call

from canvas_mcp.core import extract

COURSE = {"id": 225, "course_code": "CS225", "name": "Algorithms"}

PDF = build_text_pdf(
    [
        "Depth first search explores one branch fully.",
        "Breadth first search uses a queue.",
    ]
)
DECK = build_pptx(
    [
        {"text": "Greedy choice property", "notes": "Proof by exchange argument"},
        {"text": "Dijkstra relaxes edges"},
    ]
)
# A lecture deck exported to PDF: every page landscape, labeled per slide.
SLIDE_DECK = build_slide_pdf(
    [
        "Slide body 1 " + "alpha " * 60,
        "Slide body 2 " + "beta " * 140,
        "Slide body 3 " + "gamma " * 5,
        "Slide body 4 " + "delta " * 140,
        "Slide body 5 " + "epsilon " * 5,
    ]
)
BLANK_SLIDE_DECK = build_slide_pdf(["Deck intro", "", "Deck conclusion"])

# Alternating long/short pages: p.2 is the first that cannot fit in 1000 chars,
# and p.3/p.5 would fit afterwards, so a skip-and-resume cut would show them.
LONG = build_text_pdf(
    [
        "Page 1 " + "alpha " * 60,
        "Page 2 " + "beta " * 140,
        "Page 3 " + "gamma " * 5,
        "Page 4 " + "delta " * 140,
        "Page 5 " + "epsilon " * 5,
    ]
)


class _FileHandler(BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}

    def do_GET(self):  # noqa: N802 - stdlib handler naming
        data = self.files.get(self.path)
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def file_server():
    _FileHandler.files = {
        "/lecture.pdf": PDF,
        "/deck.pptx": DECK,
        "/long.pdf": LONG,
        "/slides.pdf": SLIDE_DECK,
        "/deck-blank.pdf": BLANK_SLIDE_DECK,
        "/scan.pdf": build_text_pdf(["", ""]),
        "/big.pdf": b"%PDF-1.4\n" + b"x" * (2 * 1024 * 1024),
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FileHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _route_file(canvas, base: str, file_id: int, name: str, path: str) -> None:
    canvas.route("get", "/courses/225", COURSE)
    canvas.route(
        "get",
        f"/courses/225/files/{file_id}",
        {"id": file_id, "display_name": name, "filename": name, "url": base + path},
    )


async def test_full_text_pdf_is_downloaded_and_read(canvas, file_server, mcp):
    _route_file(canvas, file_server, 501, "lecture.pdf", "/lecture.pdf")

    text = await call(
        mcp,
        "read_course_material",
        course_identifier=225,
        file_id=501,
        full_text=True,
    )

    assert "Status: readable — " in text
    assert "Depth first search explores one branch fully." in text
    assert "Breadth first search uses a queue." in text
    assert "## p.1" in text
    # The bytes really came over HTTP from the local server.
    assert canvas.calls_to("get", "/courses/225/files/501")


async def test_full_text_pptx_keeps_slide_text_and_speaker_notes(canvas, file_server, mcp):
    _route_file(canvas, file_server, 502, "deck.pptx", "/deck.pptx")

    text = await call(
        mcp, "read_course_material", course_identifier=225, file_id=502, full_text=True
    )

    assert "Greedy choice property" in text
    assert "Proof by exchange argument" in text, "speaker notes must come through"
    assert "Dijkstra relaxes edges" in text
    assert "## Slide 1" in text and "## Slide 2" in text


async def test_index_mode_still_returns_the_map_not_the_body(canvas, file_server, mcp):
    _route_file(canvas, file_server, 503, "lecture.pdf", "/lecture.pdf")

    index = await call(mcp, "read_course_material", course_identifier=225, file_id=503)
    assert "Status: readable — " in index
    assert "Sections:" in index
    assert "Depth first search explores one branch fully." not in index

    body = await call(
        mcp, "read_course_material", course_identifier=225, file_id=503, full_text=True
    )
    assert "Depth first search explores one branch fully." in body


async def test_full_text_stops_at_the_budget_and_names_the_next_call(
    canvas, file_server, mcp
):
    _route_file(canvas, file_server, 504, "long.pdf", "/long.pdf")

    text = await call(
        mcp,
        "read_course_material",
        course_identifier=225,
        file_id=504,
        full_text=True,
        max_chars=1000,
    )

    shown = [n for n in range(1, 6) if f"Page {n} " in text]
    hidden = [n for n in range(1, 6) if f"Page {n} " not in text]
    assert shown == [1], text
    # The cut is where reading stops: later pages that happen to be short must
    # not be pulled in behind the first page that did not fit.
    assert hidden == [2, 3, 4, 5], text
    assert "## p.1" in text and "## p.2" not in text

    # The trailing note must state the exact number of sections left unread.
    assert "[stopped after " in text
    assert "4 more section(s) not shown" in text, text
    assert "starting at 'p.2'" in text
    assert "read_material_section" in text
    assert "or raise max_chars" in text


async def test_pdf_deck_is_read_slide_by_slide_with_the_budget_cut(canvas, file_server, mcp):
    _route_file(canvas, file_server, 507, "deck.pdf", "/slides.pdf")

    text = await call(
        mcp,
        "read_course_material",
        course_identifier=225,
        file_id=507,
        full_text=True,
        max_chars=1000,
    )

    shown = [n for n in range(1, 6) if f"Slide body {n} " in text]
    hidden = [n for n in range(1, 6) if f"Slide body {n} " not in text]
    assert shown == [1], text
    assert hidden == [2, 3, 4, 5], text
    assert "## Slide 1" in text
    assert "p.1" not in text, "a deck must be labeled by slide, not by page"
    # The cut note names the unread slides and how many there are.
    assert "4 more section(s) not shown" in text
    assert "starting at 'Slide 2'" in text


async def test_pdf_deck_slides_are_addressable_by_label(canvas, file_server, mcp):
    _route_file(canvas, file_server, 508, "deck.pdf", "/slides.pdf")

    index = await call(mcp, "read_course_material", course_identifier=225, file_id=508)
    assert "- Slide 1: " in index
    assert "- Slide 3: " in index
    assert "p.1" not in index

    slide = await call(
        mcp,
        "read_material_section",
        course_identifier=225,
        file_id=508,
        section="Slide 3",
    )
    assert "Slide body 3" in slide
    assert "Slide body 1" not in slide
    assert "— Slide 3" in slide


async def test_slide_with_no_text_is_named_not_silently_dropped(
    canvas, file_server, blank_ocr, mcp
):
    _route_file(canvas, file_server, 509, "deck-blank.pdf", "/deck-blank.pdf")

    text = await call(
        mcp, "read_course_material", course_identifier=225, file_id=509, full_text=True
    )

    assert "Deck intro" in text and "Deck conclusion" in text
    assert "## Slide 2" not in text
    assert (
        "also not shown: 1 section(s) with no text (image-only or blank): Slide 2."
        in text
    ), text


async def test_scanned_pdf_full_text_names_image_only_not_empty(
    canvas, file_server, blank_ocr, mcp
):
    _route_file(canvas, file_server, 505, "scan.pdf", "/scan.pdf")

    text = await call(
        mcp, "read_course_material", course_identifier=225, file_id=505, full_text=True
    )

    assert "Status: NOT readable — image-only" in text
    assert "This file is empty" not in text
    assert "## p.1" not in text, "no text means no fabricated sections"


async def test_oversize_file_is_named_too_large(canvas, file_server, monkeypatch, mcp):
    monkeypatch.setattr(extract, "MAX_EXTRACT_BYTES", 1024 * 1024)
    _route_file(canvas, file_server, 506, "big.pdf", "/big.pdf")

    text = await call(
        mcp, "read_course_material", course_identifier=225, file_id=506, full_text=True
    )

    assert "Status: NOT readable — file too large" in text
    assert "extraction limit" in text
    assert "This file is empty" not in text
