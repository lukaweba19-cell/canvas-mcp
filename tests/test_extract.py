"""Extraction: text layers, speaker notes, corrupt parts, and OCR degradation."""

from __future__ import annotations

from fixtures import (
    build_docx,
    build_image_only_pdf,
    build_pptx,
    build_text_pdf,
    build_vtt,
    jpeg_with_text,
)

from canvas_mcp.core import extract, ocr


class FakeOcr:
    """A stand-in engine, so the OCR merge path is provable without the extra."""

    name = "fake-ocr"

    def __init__(self, text: str = "BASE CASE STOPS RECURSION") -> None:
        self.text = text
        self.pages: list[list[int]] = []
        self.recognised = 0

    def pdf_page_images(self, data: bytes, page_numbers: list[int]) -> list[bytes]:
        self.pages.append(list(page_numbers))
        return [b"\x89PNG fake" for _ in page_numbers]

    def recognize(self, image: bytes) -> str:
        self.recognised += 1
        return self.text


def _no_ocr(monkeypatch) -> None:
    """Simulate a machine without the optional extra, using the real detector."""
    ocr.reset_detection_cache()
    monkeypatch.setattr(ocr, "_module_present", lambda name: False)
    ocr.reset_detection_cache()


def test_pptx_extracts_slides_and_speaker_notes():
    data = build_pptx(
        [
            {
                "text": "Recursion needs a base case\nEach call moves toward it",
                "notes": "This will be on the exam",
            },
            {"text": "Iteration vs recursion"},
        ]
    )
    result = extract.extract_bytes("lecture.pptx", data)

    assert result.kind == "pptx"
    assert "base case" in result.text
    # Speaker notes are the professor's own emphasis, so they must survive.
    assert "(speaker notes) This will be on the exam" in result.text
    assert [section.label for section in result.sections] == ["Slide 1", "Slide 2"]
    assert result.unreadable == []


def test_pptx_corrupt_slide_does_not_lose_the_rest():
    data = build_pptx(
        [
            {"text": "Keep me"},
            {"raw": "<p:sld><unclosed>"},
            {"text": "And me"},
        ]
    )
    result = extract.extract_bytes("broken.pptx", data)

    assert "Keep me" in result.text
    assert "And me" in result.text
    assert any("Slide 2 could not be read" in warning for warning in result.warnings)


def test_docx_paragraphs_are_extracted():
    result = extract.extract_bytes(
        "reading.docx", build_docx(["First paragraph.", "Second paragraph."])
    )
    assert "First paragraph." in result.text
    assert "Second paragraph." in result.text


def test_pdf_text_layer_is_read_per_page():
    result = extract.extract_bytes(
        "slides.pdf", build_text_pdf(["Introduction to trees", "Depth first search"])
    )
    assert [section.label for section in result.sections] == ["p.1", "p.2"]
    assert "Depth first search" in result.text
    assert not any("OCR" in warning for warning in result.warnings)


def test_image_only_pdf_without_ocr_explains_the_fix(monkeypatch):
    _no_ocr(monkeypatch)
    result = extract.extract_bytes("scan.pdf", build_text_pdf(["", ""]))

    joined = " ".join(result.warnings)
    assert "2 of 2 pages have no text layer" in joined
    assert "uv sync --extra ocr" in joined
    # Empty sections are reported as unreadable, never as "the file is empty".
    assert result.unreadable == ["p.1", "p.2"]
    assert result.word_count == 0


def test_image_only_pdf_uses_ocr_when_available(monkeypatch):
    engine = FakeOcr()
    monkeypatch.setattr(ocr, "backend", lambda: engine)

    result = extract.extract_bytes("scan.pdf", build_text_pdf(["", "text", ""]))

    assert engine.pages == [[1, 3]], "only the pages without text should be rendered"
    assert "BASE CASE STOPS RECURSION" in result.text
    assert any("read with OCR" in warning for warning in result.warnings)
    # The page that already had a text layer is untouched.
    assert "text" in result.sections[1].text


def test_image_only_pdf_real_ocr_reads_rendered_text():
    """The optional extra really works: a JPEG-only PDF becomes text."""
    import importlib.util

    import pytest

    missing = [
        name
        for name in ("PIL", "pypdfium2", "rapidocr_onnxruntime")
        if importlib.util.find_spec(name) is None
    ]
    ocr.reset_detection_cache()
    if missing or not ocr.is_available():
        pytest.skip(
            "OCR extra not installed"
            + (f" (missing: {', '.join(missing)})" if missing else "")
            + f"; runtime says: {ocr.unavailable_reason()}. "
            "Run `uv sync --extra ocr` to run this test."
        )

    jpeg, width, height = jpeg_with_text("RECURSION BASE CASE")
    result = extract.extract_bytes("scan.pdf", build_image_only_pdf([(jpeg, width, height)]))

    upper = result.text.upper()
    assert "RECURSION" in upper or "BASE" in upper, result.text
    assert any("OCR" in warning for warning in result.warnings)


def test_screenshot_slide_is_ocrd_from_its_image(monkeypatch):
    engine = FakeOcr()
    monkeypatch.setattr(ocr, "backend", lambda: engine)
    data = build_pptx([{"text": "", "images": [("image1.png", b"png-bytes")]}])

    result = extract.extract_bytes("deck.pptx", data)

    assert engine.recognised == 1
    assert "BASE CASE STOPS RECURSION" in result.text
    assert result.unreadable == []


def test_screenshot_slide_without_ocr_is_reported_not_blank(monkeypatch):
    _no_ocr(monkeypatch)
    data = build_pptx([{"text": "", "images": [("image1.png", b"png-bytes")]}])

    result = extract.extract_bytes("deck.pptx", data)

    joined = " ".join(result.warnings)
    assert "1 of 1 slides contain no extractable text" in joined
    assert "uv sync --extra ocr" in joined
    assert result.unreadable == ["Slide 1"]


def test_vtt_transcript_drops_timestamps_and_duplicate_cues():
    vtt = build_vtt(["Recursion needs a base case.", "Recursion needs a base case.", "Otherwise it never stops."])

    result = extract.extract_bytes("lecture.vtt", vtt.encode())

    assert "00:00" not in result.text
    assert "-->" not in result.text
    assert result.sections[0].text.count("Recursion needs a base case.") == 1
    assert "Otherwise it never stops." in result.text


def test_transcripts_are_recognised_as_readable_captions():
    assert extract.is_transcript("week3.vtt")
    assert extract.is_readable("week3.srt")
    assert not extract.is_transcript("week3.pptx")


def test_binary_file_is_refused_with_a_next_action():
    result = extract.extract_bytes("lecture.mp4", b"\x00\x01\x02")

    assert result.sections == []
    joined = " ".join(result.warnings)
    assert "not a readable text format" in joined
    assert ".vtt" in joined, "the message should point at captions/transcripts"


def test_html_drops_script_contents():
    text = extract.html_to_text("<h1>Trees</h1><p>DFS</p><script>evil()</script>")
    assert text == "Trees\nDFS"
