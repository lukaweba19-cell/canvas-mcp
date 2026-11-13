"""Extraction: text layers, speaker notes, corrupt parts, and OCR degradation."""

from __future__ import annotations

from fixtures import (
    build_docx,
    build_image_only_pdf,
    build_ipynb,
    build_landscape_prose_pdf,
    build_legacy_office,
    build_pptx,
    build_slide_pdf,
    build_text_pdf,
    build_vtt,
    build_xlsx,
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


def _broken_ocr(monkeypatch) -> None:
    """Simulate an incomplete install whose bundled OCR cannot load."""
    message = (
        "OCR is bundled with canvas-mcp but its engine did not load "
        "(simulated broken install). Reinstall it with `uv sync --reinstall`."
    )
    monkeypatch.setattr(ocr, "backend", lambda: None)
    monkeypatch.setattr(ocr, "unavailable_reason", lambda: message)


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


def test_broken_legacy_doc_is_named_failed_not_empty():
    """A corrupt OLE container must say it could not be parsed, never "empty"."""
    result = extract.extract_bytes("notes.doc", build_legacy_office())

    status = result.status
    assert not status.readable
    assert status.reason == "failed"
    assert ".docx" in status.label()
    assert "empty" not in status.label().lower()


def test_a_doc_that_is_really_a_docx_is_read():
    result = extract.extract_bytes("mislabeled.doc", build_docx(["Real paragraph."]))

    assert "Real paragraph." in result.text
    assert result.status.readable


def test_a_doc_with_no_office_magic_is_a_failed_parse():
    result = extract.extract_bytes("junk.doc", b"definitely not a word file")

    assert not result.status.readable
    assert result.status.reason == "failed"


def test_xlsx_rows_are_read_per_sheet():
    result = extract.extract_bytes(
        "grades.xlsx", build_xlsx([["Student", "Grade"], ["Ada", "92"]])
    )
    assert result.sections[0].label == "Sheet 1"
    assert "Ada | 92" in result.text
    assert result.status.readable


def test_ipynb_cells_are_read():
    result = extract.extract_bytes(
        "hw.ipynb", build_ipynb(["# Imports", "print('hello')"])
    )
    assert result.sections[0].label == "Cell 1"
    assert "print('hello')" in result.text
    assert result.status.readable


def test_pdf_text_layer_is_read_per_page():
    result = extract.extract_bytes(
        "slides.pdf", build_text_pdf(["Introduction to trees", "Depth first search"])
    )
    assert [section.label for section in result.sections] == ["p.1", "p.2"]
    assert "Depth first search" in result.text
    assert not any("OCR" in warning for warning in result.warnings)


def test_landscape_pdf_deck_is_labeled_and_read_by_slide():
    """A lecture deck exported to PDF must read as slides, not as anonymous pages."""
    result = extract.extract_bytes(
        "lecture.pdf",
        build_slide_pdf(["Introduction to trees", "Depth first search", "Breadth first"]),
    )

    assert [section.label for section in result.sections] == [
        "Slide 1",
        "Slide 2",
        "Slide 3",
    ]
    assert result.section("Slide 2") is not None
    assert result.section("Slide 2").text == "Depth first search"
    assert "Breadth first" in result.text
    assert "p.1" not in result.toc(), "a deck must not be indexed as document pages"


PROSE_SENTENCE = (
    "The quarterly report reviews staffing, budgets, timelines and risks for "
    "each regional office, and records the decisions the committee made."
)


def test_landscape_prose_document_is_read_as_pages_not_slides():
    """A wide handout has slide-shaped pages but paragraphs, so it is not a deck."""
    pages = [f"Page {n}. " + PROSE_SENTENCE * 14 for n in range(1, 7)]
    result = extract.extract_bytes("handout.pdf", build_landscape_prose_pdf(pages))

    assert [section.label for section in result.sections] == [
        "p.1", "p.2", "p.3", "p.4", "p.5", "p.6"
    ]
    assert "Slide" not in result.toc(), "prose must never be labelled as slides"
    assert "Page 3." in result.section("p.3").text


def test_ruby_and_rails_source_files_are_read_as_text():
    """Ruby/Rails course files are source text, not an unreadable format."""
    files = {
        "routes.rb": "Rails.application.routes.draw do\n  get '/courses', to: 'courses#index'\nend",
        "index.html.erb": "<h1>Courses</h1>\n<% @courses.each do |course| %>\n  <li><%= course.name %></li>\n<% end %>",
        "report.rake": "task :report do\n  puts 'weekly report'\nend",
        "course.gemspec": "Gem::Specification.new do |spec|\n  spec.name = 'course'\nend",
        "config.ru": "run Rails.application\n",
        # Rails-era project files have no extension to key off, so they are
        # recognised by name instead of being refused as an unknown format.
        "Gemfile": "source 'https://rubygems.org'\ngem 'rails', '~> 7.1'\n",
        "Rakefile": "require_relative 'config/application'\n",
    }

    for name, source in files.items():
        result = extract.extract_bytes(name, source.encode())
        assert result.status.readable, name
        assert source.splitlines()[0] in result.text, name


def test_image_only_pdf_with_a_broken_ocr_install_explains_the_fix(monkeypatch):
    _broken_ocr(monkeypatch)
    result = extract.extract_bytes("scan.pdf", build_text_pdf(["", ""]))

    joined = " ".join(result.warnings)
    assert "2 of 2 pages have no text layer" in joined
    assert "OCR is bundled with canvas-mcp" in joined
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
    """Built-in OCR really works: a JPEG-only PDF becomes text."""
    ocr.reset_detection_cache()
    assert ocr.is_available(), ocr.unavailable_reason()

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


def test_screenshot_slide_with_a_broken_ocr_install_is_reported_not_blank(monkeypatch):
    _broken_ocr(monkeypatch)
    data = build_pptx([{"text": "", "images": [("image1.png", b"png-bytes")]}])

    result = extract.extract_bytes("deck.pptx", data)

    joined = " ".join(result.warnings)
    assert "1 of 1 slides contain no extractable text" in joined
    assert "OCR is bundled with canvas-mcp" in joined
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
