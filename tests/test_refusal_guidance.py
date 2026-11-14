"""Every refusal names the concrete reason and what to do, on every surface."""

from __future__ import annotations

import pytest
from helpers import call

from canvas_mcp.core import convert
from canvas_mcp.tools import materials as materials_module

COURSE = {"id": 231, "course_code": "CS231", "name": "Algorithms"}


def _line(report: str, needle: str) -> str:
    return next(line for line in report.splitlines() if needle in line)


@pytest.fixture
def files(canvas):
    canvas.route("get", "/courses/231", COURSE)
    canvas.route(
        "get",
        "/courses/231/files",
        [
            {"id": 411, "display_name": "scan.pdf", "filename": "scan.pdf", "size": 900},
            {"id": 412, "display_name": "week3.ppt", "filename": "week3.ppt", "size": 900},
        ],
    )
    return canvas


def _route_file(canvas, monkeypatch, file_id: int, name: str, payload: bytes):
    canvas.route(
        "get",
        f"/courses/231/files/{file_id}",
        {"id": file_id, "display_name": name, "filename": name, "url": f"https://files.example/{file_id}"},
    )

    async def download(url: str):
        return payload, None

    monkeypatch.setattr(materials_module, "_download_course_file_bytes", download)


async def test_image_only_pdf_names_reason_and_fix_everywhere(
    files, blank_ocr, monkeypatch, mcp
):
    from fixtures import build_image_only_pdf, jpeg_with_text

    jpeg, width, height = jpeg_with_text("BLANK SCAN")
    payload = build_image_only_pdf([(jpeg, width, height)])
    _route_file(files, monkeypatch, 411, "scan.pdf", payload)

    # The map can only name the reason once the file has been looked at; after
    # the read it must carry the same reason and fix as the tool result.
    read = await call(mcp, "read_course_material", course_identifier=231, file_id=411)
    assert "Status: NOT readable — image-only" in read
    assert "ask for a clearer scan" in read
    assert "transcript" in read
    assert "empty" not in read.lower()

    report = await call(mcp, "get_course_content_map", course_identifier=231)
    line = _line(report, "file_id=411")
    assert "NOT readable — image-only" in line
    assert "ask for a clearer scan" in line


async def test_ppt_without_libreoffice_names_the_install_in_the_map(files, monkeypatch, mcp):
    monkeypatch.setattr(convert, "converter_path", lambda: None)
    _route_file(
        files,
        monkeypatch,
        412,
        "week3.ppt",
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64,
    )

    report = await call(mcp, "get_course_content_map", course_identifier=231)
    line = _line(report, "file_id=412")
    assert "NOT readable — legacy .ppt" in line
    assert "LibreOffice" in line

    read = await call(mcp, "read_course_material", course_identifier=231, file_id=412)
    assert "Status: NOT readable — legacy .ppt" in read
    assert "LibreOffice" in read
    assert "empty" not in read.lower()
