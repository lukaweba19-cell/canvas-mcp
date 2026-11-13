"""The brief and the week plan report the same content status as the map."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fixtures import build_pptx, build_text_pdf
from helpers import call

from canvas_mcp.tools import materials as materials_module

COURSE = {"id": 225, "course_code": "CS225", "name": "Algorithms"}

FILES = [
    {"id": 201, "display_name": "scan.pdf", "filename": "scan.pdf"},
    {"id": 202, "display_name": "chapter.pdf", "filename": "chapter.pdf"},
    {"id": 203, "display_name": "empty.txt", "filename": "empty.txt"},
    {"id": 204, "display_name": "notes.txt", "filename": "notes.txt"},
    {"id": 205, "display_name": "Lecture 2.pptx", "filename": "Lecture 2.pptx"},
]

PAYLOADS = {
    201: build_text_pdf(["", ""]),  # image-only
    202: build_text_pdf(["Dijkstra's algorithm finds shortest paths in weighted graphs"]),
    203: b"   \n\n ",
    204: b"\x00\x01\x02 binary bytes \x00\xff",  # not text at all
    205: build_pptx([{"text": "Greedy choice property", "notes": "Proof sketch"}]),
}

_WORDS = re.compile(r"readable — \d+ words extracted")


@pytest.fixture
def course(canvas, monkeypatch):
    canvas.route("get", "/courses/225", COURSE)
    canvas.route(
        "get",
        "/courses/225/files",
        [
            {
                **entry,
                "size": len(PAYLOADS[entry["id"]]),
                "content-type": "application/octet-stream",
            }
            for entry in FILES
        ],
    )
    for entry in FILES:
        canvas.route(
            "get",
            f"/courses/225/files/{entry['id']}",
            {**entry, "url": f"https://files.example/{entry['id']}"},
        )

    # An assignment whose attachments are one image-only PDF and one real PDF.
    canvas.route(
        "get",
        "/courses/225/assignments/900",
        {
            "id": 900,
            "name": "Problem Set 3",
            "description": "<p>Prove the greedy choice property.</p>",
            "points_possible": 10,
            "submission_types": ["online_upload"],
            "attachments": [
                {**FILES[0], "url": "https://files.example/201"},
                {**FILES[1], "url": "https://files.example/202"},
            ],
        },
    )
    canvas.route(
        "get",
        "/courses/225/assignments/900/submissions/self",
        {"attempt": 0, "workflow_state": "unsubmitted"},
    )

    due = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    canvas.route(
        "get",
        "/planner/items",
        [
            {
                "plannable_type": "assignment",
                "plannable": {
                    "title": "Problem Set 3",
                    "due_at": due,
                    "points_possible": 10,
                },
                "plannable_date": due,
                "course_id": 225,
                "submissions": {"submitted": False},
            }
        ],
    )

    async def download(url: str):
        return PAYLOADS[int(url.rsplit("/", 1)[1])], None

    monkeypatch.setattr(materials_module, "_download_course_file_bytes", download)
    return canvas


def _line(report: str, needle: str) -> str:
    return next(line for line in report.splitlines() if needle in line)


async def test_assignment_brief_reports_attachment_status(course, blank_ocr, mcp):
    brief = await call(
        mcp, "get_my_assignment_brief", course_identifier=225, assignment_id=900
    )

    assert "Readable material — 2 attachment(s)" in brief
    assert "unread — extractable format" in _line(brief, "scan.pdf")
    assert "unread — extractable format" in _line(brief, "chapter.pdf")
    assert not _WORDS.search(brief), "nothing was extracted yet, so nothing is readable"

    await call(mcp, "read_course_material", course_identifier=225, file_id=201)
    await call(mcp, "read_course_material", course_identifier=225, file_id=202)
    brief = await call(
        mcp, "get_my_assignment_brief", course_identifier=225, assignment_id=900
    )

    scan = _line(brief, "scan.pdf")
    assert "NOT readable — image-only" in scan
    assert "OCR ran but found no text" in scan
    assert not _WORDS.search(scan)

    chapter = _line(brief, "file_id=202")
    assert _WORDS.search(chapter), "a read attachment must show its word count"
    assert "NOT readable" not in chapter
    header = _line(brief, "- ")
    assert "1 readable (" in header
    assert "1 NOT readable (image-only)" in header


async def test_assignment_brief_without_attachments_says_so(course, mcp):
    course.route(
        "get",
        "/courses/225/assignments/901",
        {"id": 901, "name": "Reading response", "points_possible": 5},
    )
    course.route(
        "get",
        "/courses/225/assignments/901/submissions/self",
        {"attempt": 0, "workflow_state": "unsubmitted"},
    )

    brief = await call(
        mcp, "get_my_assignment_brief", course_identifier=225, assignment_id=901
    )

    assert "this assignment has no attachments of its own" in brief
    assert "get_course_content_map" in brief
    assert not _WORDS.search(brief)


async def test_week_plan_reports_material_readiness(course, blank_ocr, mcp):
    await call(mcp, "read_course_material", course_identifier=225, file_id=201)
    await call(mcp, "read_course_material", course_identifier=225, file_id=203)
    await call(mcp, "read_course_material", course_identifier=225, file_id=204)

    plan = await call(mcp, "get_my_week_plan", days=7)

    assert "## Material readiness" in plan
    assert "NOT readable — image-only" in _line(plan, "scan.pdf")
    assert "NOT readable — empty" in _line(plan, "empty.txt")
    assert "NOT readable — unsupported encoding" in _line(plan, "notes.txt")
    assert "unread — extractable format" in _line(plan, "Lecture 2.pptx")
    header = _line(plan, "- CS225:")
    assert "2 unread" in header
    assert "1 NOT readable (image-only)" in header
    assert "1 NOT readable (empty)" in header
    assert "1 NOT readable (unsupported encoding)" in header
    assert not _WORDS.search(plan)

    await call(mcp, "read_course_material", course_identifier=225, file_id=205)
    plan = await call(mcp, "get_my_week_plan", days=7)

    lecture = _line(plan, "Lecture 2.pptx")
    assert _WORDS.search(lecture)
    assert "1 readable (" in _line(plan, "- CS225:")


async def test_unsupported_encoding_is_named_everywhere(course, mcp):
    read = await call(mcp, "read_course_material", course_identifier=225, file_id=204)

    assert "Status: NOT readable — unsupported encoding" in read
    assert "NOT readable — empty" not in read, "binary is not the same as empty"
    assert not _WORDS.search(read)

    report = await call(mcp, "get_course_content_map", course_identifier=225)
    assert "NOT readable — unsupported encoding" in _line(report, "file_id=204")

    listing = await call(mcp, "list_course_files", course_identifier=225)
    assert "NOT readable — unsupported encoding" in _line(listing, "notes.txt")


async def test_section_read_reports_the_file_status(course, blank_ocr, mcp):
    await call(mcp, "read_course_material", course_identifier=225, file_id=201)
    await call(mcp, "read_course_material", course_identifier=225, file_id=203)

    page = await call(
        mcp, "read_material_section", course_identifier=225, file_id=201, section="Page 1"
    )
    assert "This section has no text layer" in page
    assert "File status: NOT readable — image-only" in page

    whole = await call(
        mcp, "read_material_section", course_identifier=225, file_id=203, section="Text"
    )
    assert "This file contains no text at all (it is empty)." in whole
    assert "File status: NOT readable — empty" in whole
    assert "image" not in whole, "an empty file must not be blamed on images"
