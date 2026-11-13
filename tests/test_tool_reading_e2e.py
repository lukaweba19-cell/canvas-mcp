"""End to end: the real MCP tools read real generated files.

Unit extraction tests call ``extract_bytes`` directly. These go through the
registered tools (content map -> read_course_material -> read_material_section
-> build_study_guide) over the in-memory MCP client, with real PDF, scanned PDF
and PPTX bytes, so the tool wiring itself is what is proven.
"""

from __future__ import annotations

import pytest
from fixtures import (
    build_doc,
    build_image_only_pdf,
    build_legacy_office,
    build_pptx,
    build_text_pdf,
    jpeg_with_text,
)
from helpers import call

from canvas_mcp.tools import materials as materials_module

COURSE = {"id": 231, "course_code": "CS231", "name": "Algorithms"}

FILES = [
    {"id": 401, "display_name": "lecture.pdf", "filename": "lecture.pdf"},
    {"id": 402, "display_name": "scan.pdf", "filename": "scan.pdf"},
    {"id": 403, "display_name": "deck.pptx", "filename": "deck.pptx"},
    {"id": 404, "display_name": "old-notes.doc", "filename": "old-notes.doc"},
    {"id": 405, "display_name": "broken.doc", "filename": "broken.doc"},
]

_SCAN_JPEG = jpeg_with_text("RECURSION BASE CASE")
PAYLOADS = {
    401: build_text_pdf(
        [
            "Depth first search explores one branch fully.",
            "Breadth first search uses a queue.",
        ]
    ),
    402: build_image_only_pdf([_SCAN_JPEG]),
    403: build_pptx(
        [
            {
                "text": "Greedy choice property",
                "notes": "Proof by exchange argument",
            }
        ]
    ),
    404: build_doc("Legacy notes on recursion"),
    405: build_legacy_office(),
}


@pytest.fixture
def course(canvas, monkeypatch):
    canvas.route("get", "/courses/231", COURSE)
    canvas.route(
        "get",
        "/courses/231/files",
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
            f"/courses/231/files/{entry['id']}",
            {**entry, "url": f"https://files.example/{entry['id']}"},
        )

    async def download(url: str):
        return PAYLOADS[int(url.rsplit("/", 1)[1])], None

    monkeypatch.setattr(materials_module, "_download_course_file_bytes", download)
    return canvas


def _line(report: str, needle: str) -> str:
    return next(line for line in report.splitlines() if needle in line)


async def test_text_pdf_is_read_through_the_real_tools(course, mcp):
    report = await call(mcp, "get_course_content_map", course_identifier=231)
    assert "unread — extractable format" in _line(report, "file_id=401")

    read = await call(mcp, "read_course_material", course_identifier=231, file_id=401)
    assert "Status: readable — " in read

    first = await call(
        mcp, "read_material_section", course_identifier=231, file_id=401, section="p.1"
    )
    assert "Depth first search explores one branch fully." in first

    second = await call(
        mcp, "read_material_section", course_identifier=231, file_id=401, section="p.2"
    )
    assert "Breadth first search uses a queue." in second

    report = await call(mcp, "get_course_content_map", course_identifier=231)
    assert "words extracted" in _line(report, "file_id=401")


async def test_scanned_pdf_is_read_by_built_in_ocr_through_the_real_tools(course, mcp):
    read = await call(mcp, "read_course_material", course_identifier=231, file_id=402)

    # OCR (not a text layer) is what makes this readable, with a real word count.
    assert "Status: readable — " in read, read
    assert "words extracted" in read

    page = await call(
        mcp, "read_material_section", course_identifier=231, file_id=402, section="p.1"
    )
    upper = page.upper()
    assert "RECURSION" in upper or "BASE" in upper, page


async def test_pptx_text_and_speaker_notes_come_back_through_the_real_tools(course, mcp):
    read = await call(mcp, "read_course_material", course_identifier=231, file_id=403)
    assert "Status: readable — " in read

    slide = await call(
        mcp, "read_material_section", course_identifier=231, file_id=403, section="Slide 1"
    )
    assert "Greedy choice property" in slide
    assert "Proof by exchange argument" in slide, "speaker notes must survive"


async def test_legacy_doc_is_read_through_the_real_tools(course, mcp):
    before = await call(mcp, "get_course_content_map", course_identifier=231)
    assert "unread — extractable format" in _line(before, "file_id=404")

    read = await call(
        mcp, "read_course_material", course_identifier=231, file_id=404, full_text=True
    )
    assert "Status: readable — " in read, read
    assert "Legacy notes on recursion" in read

    report = await call(mcp, "get_course_content_map", course_identifier=231)
    assert "words extracted" in _line(report, "file_id=404")


async def test_broken_legacy_doc_is_an_honest_failure(course, mcp):
    read = await call(mcp, "read_course_material", course_identifier=231, file_id=405)

    assert "Status: NOT readable — extraction failed" in read
    assert ".docx" in read
    assert "words extracted" not in read


async def test_study_guide_built_from_read_material_round_trips(course, mcp):
    await call(mcp, "read_course_material", course_identifier=231, file_id=401)
    body = await call(
        mcp, "read_material_section", course_identifier=231, file_id=401, section="p.1"
    )
    assert "Depth first search explores one branch fully." in body

    built = await call(
        mcp,
        "build_study_guide",
        course_identifier=231,
        title="Graph search",
        markdown=(
            "# Graph search\n\n"
            "- DFS explores one branch fully [lecture.pdf, p.1]\n"
            "- BFS uses a queue [lecture.pdf, p.2]\n"
        ),
        sources="lecture.pdf (p.1-2)",
    )
    assert "Graph search" in built

    guide = await call(mcp, "read_study_guide", course_identifier=231, title="Graph search")
    assert "DFS explores one branch fully" in guide
    assert "BFS uses a queue" in guide
