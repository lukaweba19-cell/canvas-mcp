"""Readability claims are backed by text: no surface says readable without it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fixtures import build_pptx, build_text_pdf
from helpers import call

from canvas_mcp.core import extract, ocr
from canvas_mcp.tools import materials as materials_module

COURSE = {"id": 225, "course_code": "CS225", "name": "Algorithms"}

FILES = [
    {"id": 101, "display_name": "scan.pdf", "filename": "scan.pdf"},
    {"id": 102, "display_name": "Lecture 3.pptx", "filename": "Lecture 3.pptx"},
    {"id": 103, "display_name": "empty.txt", "filename": "empty.txt"},
    {"id": 104, "display_name": "intro.mp4", "filename": "intro.mp4"},
]

PAYLOADS = {
    # Image-only: two pages with no text layer at all.
    101: build_text_pdf(["", ""]),
    # Genuinely readable, with speaker notes.
    102: build_pptx([{"text": "Base case first", "notes": "Exam favourite"}]),
    # Valid but empty.
    103: b"   \n\n  ",
    # A format that has no text representation at all.
    104: b"\x00\x01binary",
}

_WORDS_EXTRACTED = re.compile(r"readable — \d+ words extracted")


@pytest.fixture
def no_ocr(monkeypatch):
    """A machine without the optional OCR extra, using the real detector."""
    ocr.reset_detection_cache()
    monkeypatch.setattr(ocr, "_module_present", lambda name: False)
    ocr.reset_detection_cache()
    yield
    ocr.reset_detection_cache()


@pytest.fixture
def course(canvas, monkeypatch):
    canvas.route("get", "/courses/225", COURSE)
    canvas.route(
        "get",
        "/courses/225/files",
        [
            {**entry, "size": len(PAYLOADS[entry["id"]]), "content-type": "application/octet-stream"}
            for entry in FILES
        ],
    )
    for entry in FILES:
        canvas.route(
            "get", f"/courses/225/files/{entry['id']}",
            {**entry, "url": f"https://files.example/{entry['id']}"},
        )

    async def download(url: str):
        return PAYLOADS[int(url.rsplit("/", 1)[1])], None

    monkeypatch.setattr(materials_module, "_download_course_file_bytes", download)
    return canvas


def _line(report: str, needle: str) -> str:
    return next(line for line in report.splitlines() if needle in line)


async def test_image_only_pdf_is_never_reported_readable(course, no_ocr, mcp):
    read = await call(mcp, "read_course_material", course_identifier=225, file_id=101)

    # The extraction itself must state the real reason, not an empty success.
    assert "Status: NOT readable — image-only" in read
    assert "uv sync --extra ocr" in read
    assert not _WORDS_EXTRACTED.search(read), "no readable claim without text"

    report = await call(mcp, "get_course_content_map", course_identifier=225)
    line = _line(report, "file_id=101")
    assert "NOT readable — image-only" in line
    assert not _WORDS_EXTRACTED.search(line)
    assert "needs OCR" not in line.lower() or "uv sync --extra ocr" in line


async def test_screenshot_slide_is_not_reported_readable(course, no_ocr, mcp, monkeypatch):
    async def screenshot_deck(url: str):
        return build_pptx([{"text": "", "images": [("image1.png", b"png-bytes")]}]), None

    monkeypatch.setattr(materials_module, "_download_course_file_bytes", screenshot_deck)

    read = await call(mcp, "read_course_material", course_identifier=225, file_id=102)

    assert "Status: NOT readable — image-only" in read
    assert "uv sync --extra ocr" in read
    assert not _WORDS_EXTRACTED.search(read)


async def test_readable_pptx_reports_words_and_returns_text(course, mcp):
    read = await call(mcp, "read_course_material", course_identifier=225, file_id=102)

    assert "Status: readable — " in read
    assert _WORDS_EXTRACTED.search(read)

    section = await call(
        mcp, "read_material_section", course_identifier=225, file_id=102, section="Slide 1"
    )
    assert "Base case first" in section
    assert "Exam favourite" in section

    report = await call(mcp, "get_course_content_map", course_identifier=225)
    line = _line(report, "file_id=102")
    assert _WORDS_EXTRACTED.search(line), "an extracted file must show its word count"
    assert "NOT readable" not in line


async def test_unread_file_is_not_called_readable(course, mcp):
    report = await call(mcp, "get_course_content_map", course_identifier=225)

    line = _line(report, "file_id=102")
    assert "unread — extractable format" in line
    assert not _WORDS_EXTRACTED.search(line)


async def test_empty_file_is_labelled_empty_not_readable(course, mcp):
    read = await call(mcp, "read_course_material", course_identifier=225, file_id=103)

    assert "Status: NOT readable — empty" in read
    assert not _WORDS_EXTRACTED.search(read)
    assert "Status: NOT readable — image-only" not in read

    report = await call(mcp, "get_course_content_map", course_identifier=225)
    assert "NOT readable — empty" in _line(report, "file_id=103")


async def test_unsupported_format_is_named_not_called_empty(course, mcp):
    read = await call(mcp, "read_course_material", course_identifier=225, file_id=104)

    assert "NOT readable — unsupported format" in read
    assert "'mp4'" in read
    assert "NOT readable — empty" not in read
    assert not _WORDS_EXTRACTED.search(read)


async def test_file_listing_carries_status_per_file(course, no_ocr, mcp):
    await call(mcp, "read_course_material", course_identifier=225, file_id=102)
    await call(mcp, "read_course_material", course_identifier=225, file_id=101)

    listing = await call(mcp, "list_course_files", course_identifier=225)

    extracted = _line(listing, "Lecture 3.pptx")
    assert _WORDS_EXTRACTED.search(extracted)

    never_read = _line(listing, "scan.pdf")
    assert "NOT readable — image-only" in never_read
    assert not _WORDS_EXTRACTED.search(never_read)


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("scan.pdf", build_text_pdf(["", ""])),
        ("deck.pptx", build_pptx([{"text": "", "images": [("a.png", b"x")]}])),
        ("empty.txt", b"  \n "),
        ("clip.mp4", b"\x00\x01"),
        ("blob.txt", b"\x00\x01\x02 binary \x00\xff"),
        ("broken.docx", b"not a zip"),
        ("notes.pptx", build_pptx([{"text": "Real text here"}])),
    ],
)
def test_readable_status_always_carries_words(name, data):
    status = extract.extract_bytes(name, data).status

    if status.readable:
        assert status.words > 0, "readable must mean text exists"
        assert _WORDS_EXTRACTED.search(status.label())
    else:
        assert status.reason in {
            "image_only",
            "empty",
            "unsupported",
            "encoding",
            "failed",
        }
        assert status.label().startswith("NOT readable — ")
        assert not _WORDS_EXTRACTED.search(status.label())


def test_status_for_unread_file_does_not_claim_readability():
    status = extract.status_for("Lecture 3.pptx")
    assert not status.readable
    assert status.reason == "not_read"
    assert "readable —" not in status.label()


def test_readable_labels_have_a_single_source():
    """No module may format its own readable/NOT readable wording."""
    root = Path(__file__).resolve().parents[1]
    offenders = [
        str(path.relative_to(root))
        for path in (root / "src" / "canvas_mcp").rglob("*.py")
        if path.name != "extract.py"
        and ("NOT readable — " in path.read_text() or "readable — " in path.read_text())
    ]
    assert not offenders, f"readable wording outside ContentStatus: {offenders}"
