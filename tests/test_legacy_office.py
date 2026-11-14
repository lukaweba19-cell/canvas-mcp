"""Legacy Office formats: real .doc and .xls in process, .ppt via LibreOffice."""

from __future__ import annotations

import os
import stat

from fixtures import build_doc, build_legacy_office, build_pptx, build_xls
from helpers import call

from canvas_mcp.core import convert, extract

COURSE = {"id": 231, "course_code": "CS231", "name": "Algorithms"}


def test_legacy_doc_text_is_read_in_process():
    result = extract.extract_bytes("notes.doc", build_doc("Lecture notes on trees"))

    assert result.status.readable
    assert "Lecture notes on trees" in result.text
    assert result.sections[0].label == "Document"


def test_legacy_xls_sheets_are_read_in_process():
    result = extract.extract_bytes("grades.xls", build_xls([["Ada", "92"], ["Grace", "88"]]))

    assert result.status.readable
    assert [section.label for section in result.sections] == ["Sheet 1"]
    assert "Ada | 92" in result.text
    assert "Grace | 88" in result.text


def _fake_soffice(directory, converted: bytes):
    """A stand-in LibreOffice that writes a real .pptx, so the plumbing is real."""
    payload = directory / "converted.pptx"
    payload.write_bytes(converted)
    script = directory / "soffice"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, shutil, sys\n"
        "args = sys.argv[1:]\n"
        "outdir = args[args.index('--outdir') + 1]\n"
        "shutil.copyfile(os.environ['FAKE_PPTX'], os.path.join(outdir, 'input.pptx'))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return payload


def test_legacy_ppt_is_converted_and_read(monkeypatch, tmp_path):
    deck = build_pptx(
        [{"text": "Greedy choice property", "notes": "Proof by exchange argument"}]
    )
    payload = _fake_soffice(tmp_path, deck)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PPTX", str(payload))

    assert convert.converter_path() == str(tmp_path / "soffice")
    result = extract.extract_bytes("lecture.ppt", build_legacy_office())

    assert result.status.readable, result.warnings
    assert "Greedy choice property" in result.text
    assert "Proof by exchange argument" in result.text
    assert result.sections[0].label == "Slide 1"


def test_legacy_ppt_without_libreoffice_names_the_install(monkeypatch):
    monkeypatch.setattr(convert, "converter_path", lambda: None)

    result = extract.extract_bytes("lecture.ppt", build_legacy_office())

    status = result.status
    assert not status.readable
    assert status.reason == "legacy_office"
    assert "LibreOffice" in status.label()
    assert "empty" not in status.label().lower()


def test_legacy_ppt_without_libreoffice_is_named_before_the_download(monkeypatch):
    monkeypatch.setattr(convert, "converter_path", lambda: None)

    status = extract.status_for("Week 3 slides.ppt")

    assert not status.readable
    assert status.reason == "legacy_office"
    assert "LibreOffice" in status.label()


async def test_legacy_xls_reads_through_the_real_tool(canvas, monkeypatch, mcp):
    payload = build_xls([["Ada", "92"], ["Grace", "88"]])
    canvas.route("get", "/courses/231", COURSE)
    canvas.route(
        "get",
        "/courses/231/files",
        [{"id": 406, "display_name": "grades.xls", "filename": "grades.xls", "size": len(payload)}],
    )
    canvas.route(
        "get",
        "/courses/231/files/406",
        {
            "id": 406,
            "display_name": "grades.xls",
            "filename": "grades.xls",
            "url": "https://files.example/406",
        },
    )

    async def download(url: str):
        return payload, None

    from canvas_mcp.tools import materials as materials_module

    monkeypatch.setattr(materials_module, "_download_course_file_bytes", download)

    read = await call(mcp, "read_course_material", course_identifier=231, file_id=406)
    assert "Status: readable — " in read, read

    sheet = await call(
        mcp, "read_material_section", course_identifier=231, file_id=406, section="Sheet 1"
    )
    assert "Ada | 92" in sheet
