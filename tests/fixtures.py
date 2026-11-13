"""Byte-level builders for extraction tests: no network, no external files."""

from __future__ import annotations

import io
import io as _io
import json
import zipfile
from xml.sax.saxutils import escape

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_IMG_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
)


def _runs(paragraphs: list[str]) -> str:
    return "".join(
        f"<a:p><a:r><a:t>{escape(part)}</a:t></a:r></a:p>" for part in paragraphs if part
    ) or "<a:p/>"


def build_pptx(slides: list[dict]) -> bytes:
    """Build a minimal PPTX.

    Each slide dict accepts ``text`` (str, one line per paragraph), ``notes``
    (str), ``images`` (list of ``(filename, bytes)`` pairs), or ``raw`` (a raw
    slide XML string, used to simulate a corrupt slide).
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, slide in enumerate(slides, start=1):
            if "raw" in slide:
                xml = slide["raw"]
            else:
                body = _runs(str(slide.get("text", "")).split("\n"))
                xml = (
                    f'<p:sld xmlns:a="{_A}" xmlns:p="{_P}">'
                    f"<p:cSld><p:spTree><p:sp><p:txBody>{body}</p:txBody></p:sp>"
                    "</p:spTree></p:cSld></p:sld>"
                )
            archive.writestr(f"ppt/slides/slide{index}.xml", xml)

            if slide.get("notes"):
                notes = _runs(str(slide["notes"]).split("\n"))
                archive.writestr(
                    f"ppt/notesSlides/notesSlide{index}.xml",
                    f'<p:notes xmlns:a="{_A}" xmlns:p="{_P}">'
                    f"<p:cSld><p:spTree><p:sp><p:txBody>{notes}</p:txBody></p:sp>"
                    "</p:spTree></p:cSld></p:notes>",
                )

            images = slide.get("images") or []
            if images:
                rels = [f'<Relationships xmlns="{_REL}">']
                for number, (name, data) in enumerate(images, start=1):
                    archive.writestr(f"ppt/media/{name}", data)
                    rels.append(
                        f'<Relationship Id="rId{number}" Type="{_IMG_REL}" '
                        f'Target="../media/{name}"/>'
                    )
                rels.append("</Relationships>")
                archive.writestr(
                    f"ppt/slides/_rels/slide{index}.xml.rels", "".join(rels)
                )
    return buffer.getvalue()


def build_docx(paragraphs: list[str], images: list[tuple[str, bytes]] | None = None) -> bytes:
    """Build a minimal DOCX; images go in ``word/media`` with a rels entry."""
    buffer = io.BytesIO()
    body = "".join(
        f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>" for text in paragraphs
    )
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>',
        )
        if images:
            rels = [f'<Relationships xmlns="{_REL}">']
            for number, (name, data) in enumerate(images, start=1):
                archive.writestr(f"word/media/{name}", data)
                rels.append(
                    f'<Relationship Id="rId{number}" Type="{_IMG_REL}" '
                    f'Target="media/{name}"/>'
                )
            rels.append("</Relationships>")
            archive.writestr("word/_rels/document.xml.rels", "".join(rels))
    return buffer.getvalue()


def _pdf(objects: list[bytes]) -> bytes:
    """Assemble objects into a PDF with a correct xref table."""
    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<</Size "
        + str(len(objects) + 1).encode()
        + b"/Root 1 0 R>>\nstartxref\n"
        + str(xref).encode()
        + b"\n%%EOF"
    )
    return out


def build_text_pdf(
    pages: list[str], size: tuple[int, int] = (612, 792)
) -> bytes:
    """A PDF with a real text layer, one page per string.

    ``size`` is the page box: the default is portrait (a document). Pass a
    landscape size (see :func:`build_slide_pdf`) for a deck exported to PDF.
    """
    width, height = size
    objects: list[bytes] = [b"<</Type/Catalog/Pages 2 0 R>>"]
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(len(pages)))
    objects.append(f"<</Type/Pages/Kids[{kids}]/Count {len(pages)}>>".encode())
    for index, text in enumerate(pages):
        stream = (
            f"BT /F1 18 Tf 72 {height - 72} Td ({text}) Tj ET".encode()
            if text
            else b""
        )
        page_number = 3 + 2 * index
        objects.append(
            (
                f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {width} {height}]"
                f"/Contents {page_number + 1} 0 R"
                f"/Resources<</Font<</F1 {3 + 2 * len(pages)} 0 R>>>>>>"
            ).encode()
        )
        objects.append(
            b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream"
        )
    objects.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    return _pdf(objects)


def build_slide_pdf(slides: list[str]) -> bytes:
    """A deck exported to PDF: one landscape page per slide, with a text layer."""
    return build_text_pdf(slides, size=(960, 540))


def build_image_only_pdf(jpegs: list[tuple[bytes, int, int]]) -> bytes:
    """A PDF where every page is one JPEG and there is no text layer."""
    objects: list[bytes] = [b"<</Type/Catalog/Pages 2 0 R>>"]
    kids = " ".join(f"{3 + 3 * index} 0 R" for index in range(len(jpegs)))
    objects.append(f"<</Type/Pages/Kids[{kids}]/Count {len(jpegs)}>>".encode())
    for index, (jpeg, width, height) in enumerate(jpegs):
        page_number = 3 + 3 * index
        objects.append(
            (
                f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {width} {height}]"
                f"/Contents {page_number + 1} 0 R"
                f"/Resources<</XObject<</Im0 {page_number + 2} 0 R>>>>>>"
            ).encode()
        )
        stream = f"q {width} 0 0 {height} 0 0 cm /Im0 Do Q".encode()
        objects.append(
            b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream"
        )
        objects.append(
            (
                f"<</Type/XObject/Subtype/Image/Width {width}/Height {height}"
                f"/ColorSpace/DeviceRGB/BitsPerComponent 8/Filter/DCTDecode/Length "
            ).encode()
            + str(len(jpeg)).encode()
            + b">>stream\n"
            + jpeg
            + b"\nendstream"
        )
    return _pdf(objects)


def build_legacy_office(payload: bytes = b"Word 97 binary body") -> bytes:
    """Bytes shaped like a legacy Word .doc: the OLE compound-file magic + body."""
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16 + payload


def jpeg_with_text(text: str, width: int = 1200, height: int = 300) -> tuple[bytes, int, int]:
    """Render text into a JPEG so OCR has something real to recognise.

    Pillow is a normal dependency (OCR ships with the server), so this is
    always available.
    """
    import io as _io

    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=64)
    draw.text((40, height // 3), text, fill="black", font=font)
    buffer = _io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue(), width, height


def build_vtt(cues: list[str]) -> str:
    """A WebVTT caption track with cue numbers and timestamps."""
    lines = ["WEBVTT", ""]
    for index, cue in enumerate(cues, start=1):
        start = f"00:00:{index:02d}.000"
        end = f"00:00:{index + 1:02d}.000"
        lines.extend([str(index), f"{start} --> {end}", cue, ""])
    return "\n".join(lines)


def build_xlsx(rows: list[list[str]], sheet: int = 1) -> bytes:
    """A minimal XLSX with one worksheet of plain value cells."""
    cells = []
    for row_index, row in enumerate(rows, start=1):
        row_cells = "".join(
            f"<c r='{chr(65 + column_index)}{row_index}'><v>{value}</v></c>"
            for column_index, value in enumerate(row)
        )
        cells.append(f"<row r='{row_index}'>{row_cells}</row>")
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(cells)}</sheetData></worksheet>"
    )
    buffer = _io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"xl/worksheets/sheet{sheet}.xml", sheet_xml)
    return buffer.getvalue()


def build_ipynb(cells: list[str]) -> bytes:
    """A minimal notebook, one markdown cell per string."""
    return json.dumps(
        {"cells": [{"cell_type": "markdown", "source": cell} for cell in cells]}
    ).encode()
