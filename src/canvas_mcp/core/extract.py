"""Turn Canvas course files into text the model can actually read.

Canvas hands back bytes. A model cannot read a PPTX, and base64 of a slide deck
is noise. This module extracts text (and, for slides, the speaker notes, which
are usually the professor's own explanation of what matters) once, and caches
the result so repeat reads cost nothing.

Formats:
- PDF               -> one section per page, via pypdf. A PDF is read as slides
                       (Slide 1, Slide 2, ...) only when every page repeats one
                       landscape, slide-sized page box (4:3 to 16:9) and the text
                       is slide-sparse; a landscape report or handout therefore
                       stays p.1, p.2, ...
- PPTX/PPTM         -> one section per slide + its notes
- DOCX              -> one section for the document
- XLSX              -> one section per sheet
- IPYNB             -> one section per cell
- VTT/SRT           -> one section per transcript (captions, lecture transcripts)
- HTML/MD/TXT/code  -> one section
Everything else (images, audio, video, archives) is reported as unreadable
rather than silently returning garbage.

Image-only content (a scanned PDF, a deck exported as screenshots) goes through
``core.ocr``, which ships with canvas-mcp. If OCR finds nothing, or a broken
install stops it from running, the section keeps a label with no text and the
caller gets a warning that counts the affected pages and names the real reason —
never a blank result passed off as "the file is empty".
"""

from __future__ import annotations

import html as html_lib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader

from . import ocr

# OOXML namespaces
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Guard rails: Canvas permits very large files, and extraction buffers in memory.
MAX_EXTRACT_BYTES = 60 * 1024 * 1024
MAX_SECTION_CHARS = 40_000

# OCR is slow, so a slide with a dozen decorative images must not turn one read
# into minutes. Three images per slide covers the usual screenshot + diagram.
_MAX_OCR_IMAGES_PER_SLIDE = 3
_MAX_OCR_DOCX_IMAGES = 5

# Telling a slide deck from a landscape handout takes two independent clues: the
# pages must share one landscape, slide-shaped size, and the text must be sparse
# enough to be slide bodies. Page shape alone would mislabel landscape reports.
_SLIDE_ASPECT_RANGE = (1.2, 1.9)  # 4:3 = 1.33, 16:10 = 1.6, 16:9 = 1.78
_SLIDE_PAGE_SIZE_TOLERANCE = 2.0  # PDF points: an exported deck repeats one box
_SLIDE_MEDIAN_WORDS_MAX = 150
_SLIDE_MAX_WORDS = 250

_READABLE_SUFFIXES = {
    ".pdf", ".pptx", ".pptm", ".docx", ".xlsx", ".xlsm", ".ipynb",
    ".html", ".htm", ".txt", ".md", ".markdown", ".csv", ".json",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".rb", ".go", ".rs",
    # Ruby/Rails course files: views and Rake tasks are plain source too.
    ".erb", ".rake",
    ".sql", ".r", ".rmd", ".xml", ".yaml", ".yml", ".tex",
    # Caption tracks and lecture transcripts: the readable form of a video.
    ".vtt", ".srt", ".webvtt",
}

# Legacy Office binaries (Word 97-2003 and friends) are OLE compound files, not
# text; parsing them properly needs a converter. A .doc that is really a .docx
# (a common mislabel) is read as its real format, and a true legacy binary gets
# an explicit conversion instruction instead of a vague "unsupported format".
_LEGACY_OFFICE = {
    ".doc": (".docx", "Word 97-2003"),
    ".ppt": (".pptx", "PowerPoint 97-2003"),
    ".xls": (".xlsx", "Excel 97-2003"),
}
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_LEGACY_CONVERSION = (
    "open it in Word, Google Docs or LibreOffice and save it as .docx (or PDF), "
    "then read that file"
)

_TRANSCRIPT_SUFFIXES = {".vtt", ".srt", ".webvtt"}

_SLIDE_RE = re.compile(r"ppt/slides/slide(\d+)\.xml$")
_NOTES_RE = re.compile(r"ppt/notesSlides/notesSlide(\d+)\.xml$")
_SHEET_RE = re.compile(r"xl/worksheets/sheet(\d+)\.xml$")


@dataclass(frozen=True)
class Section:
    """One addressable chunk of a document (a page, slide, sheet, cell)."""

    label: str
    text: str


@dataclass
class Extracted:
    """Extracted text plus what the caller needs to judge its quality."""

    filename: str
    kind: str
    sections: list[Section] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(f"[{s.label}]\n{s.text}" for s in self.sections if s.text.strip())

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def status(self) -> ContentStatus:
        """Whether this extraction produced text, and why not if it did not."""
        return status_from_parts(self.filename, self.sections, self.warnings)

    @property
    def unreadable(self) -> list[str]:
        """Labels of sections with no text: image-only or genuinely blank."""
        return [section.label for section in self.sections if not section.text.strip()]

    def toc(self, max_labels: int = 200) -> str:
        """A compact index — the right thing to show before the full text."""
        if not self.sections:
            return "(no readable text)"
        lines = []
        for section in self.sections[:max_labels]:
            words = len(section.text.split())
            if words == 0:
                lines.append(f"- {section.label}: (no text — image only)")
            else:
                lines.append(f"- {section.label}: {words} words")
        if len(self.sections) > max_labels:
            lines.append(f"- ... {len(self.sections) - max_labels} more sections")
        return "\n".join(lines)

    def section(self, selector: str) -> Section | None:
        """Find a section by exact label, number, or 1-based position."""
        selector = str(selector).strip()
        for section in self.sections:
            if section.label.lower() == selector.lower():
                return section
        number = re.search(r"\d+", selector)
        if number:
            wanted = int(number.group(0))
            for section in self.sections:
                found = re.search(r"\d+", section.label)
                if found and int(found.group(0)) == wanted:
                    return section
        if selector.isdigit() and 1 <= int(selector) <= len(self.sections):
            return self.sections[int(selector) - 1]
        return None


def is_readable(filename: str) -> bool:
    """Whether this module can pull text out of the file."""
    return Path(filename).suffix.lower() in _READABLE_SUFFIXES


def is_transcript(filename: str) -> bool:
    """Whether this file is a caption track / transcript rather than a document."""
    return Path(filename).suffix.lower() in _TRANSCRIPT_SUFFIXES


# ---------------------------------------------------------------------------
# Content status
# ---------------------------------------------------------------------------

_IMAGE_ONLY_WARNING = re.compile(
    r"no text layer|image-only|image only|no extractable text|"
    r"no readable text after OCR|scanned",
    re.IGNORECASE,
)
_UNSUPPORTED_WARNING = re.compile(
    r"not a readable text format|has no text representation", re.IGNORECASE
)
_ENCODING_WARNING = re.compile(
    r"unsupported text encoding|unsupported encoding|binary file|binary bytes",
    re.IGNORECASE,
)
_LEGACY_WARNING = re.compile(
    r"legacy (Word|PowerPoint|Excel) 97", re.IGNORECASE
)
_TOO_LARGE_WARNING = re.compile(r"over the \d+ MB extraction limit", re.IGNORECASE)
_FAILED_WARNING = re.compile(
    r"corrupt|extraction failed|could not be read|not (a )?valid", re.IGNORECASE
)
_BINARY_HINT = (
    "File is binary or in an unsupported text encoding (tried UTF-8, UTF-16 and "
    "Windows-1252), so it has no readable text."
)

# The word used in "NOT readable — <kind>: ..." for each reason.
REASON_LABELS = {
    "image_only": "image-only",
    "empty": "empty",
    "unsupported": "unsupported format",
    "encoding": "unsupported encoding",
    "legacy_office": "legacy Office file",
    "too_large": "file too large",
    "failed": "extraction failed",
    "quiz": "quiz",
    "embedded": "embedded video/LTI",
    "external": "external link",
    "unknown_item": "unknown item type",
}


@dataclass(frozen=True)
class ContentStatus:
    """Whether text can actually be produced, and if not, exactly why.

    Every surface that would call something "readable" reports one of these
    instead, so ``readable=True`` always means real text exists — never merely
    that the file's extension is one this module can open.
    """

    readable: bool
    reason: str  # text | image_only | empty | unsupported | encoding | failed | not_read
    detail: str
    words: int = 0
    kind: str = ""  # overrides the REASON_LABELS word, e.g. for module items

    def label(self) -> str:
        if self.readable:
            return f"readable — {self.words} words extracted"
        if self.reason == "not_read":
            return "unread — extractable format: call read_course_material"
        kind = self.kind or REASON_LABELS.get(self.reason, self.reason)
        return f"NOT readable — {kind}: {self.detail}"


def _ocr_hint() -> str:
    reason = ocr.unavailable_reason()
    if reason:
        return f"OCR could not run: {reason}"
    return "OCR ran but found no text in it"


def _unsupported_detail(filename: str) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    return (
        f"'{suffix or 'no extension'}' has no text representation "
        "(audio, video, image or archive)"
    )


def status_from_parts(
    filename: str,
    sections: list[Section],
    warnings: list[str],
) -> ContentStatus:
    """Derive a content status from extracted sections and their warnings."""
    words = sum(len(section.text.split()) for section in sections)
    if words:
        return ContentStatus(True, "text", "", words)

    joined = " ".join(warnings)
    if _UNSUPPORTED_WARNING.search(joined):
        return ContentStatus(False, "unsupported", _unsupported_detail(filename))
    if _TOO_LARGE_WARNING.search(joined):
        return ContentStatus(
            False,
            "too_large",
            f"it is over the {MAX_EXTRACT_BYTES // (1024 * 1024)} MB extraction limit",
            kind="file too large",
        )
    if _LEGACY_WARNING.search(joined):
        suffix = Path(filename).suffix.lower()
        return ContentStatus(
            False, "legacy_office", _LEGACY_CONVERSION, kind=f"legacy {suffix}"
        )
    if _ENCODING_WARNING.search(joined):
        return ContentStatus(
            False,
            "encoding",
            "its bytes are binary or not UTF-8/UTF-16/Windows-1252 text "
            "(convert it to PDF or plain text)",
        )
    if _FAILED_WARNING.search(joined):
        return ContentStatus(False, "failed", warnings[0] if warnings else "unknown error")
    if _IMAGE_ONLY_WARNING.search(joined):
        return ContentStatus(
            False,
            "image_only",
            "every page/slide is an image with no text layer; " + _ocr_hint(),
        )
    return ContentStatus(False, "empty", "the file contains no text")


def status_for(filename: str, record: dict | None = None) -> ContentStatus:
    """Status for a file, using its cached extraction when one exists."""
    if record is not None:
        sections = [
            Section(str(item.get("label", "")), str(item.get("text", "")))
            for item in record.get("sections") or []
        ]
        return status_from_parts(filename, sections, list(record.get("warnings") or []))

    suffix = Path(filename).suffix.lower()
    if suffix in _LEGACY_OFFICE:
        return ContentStatus(
            False, "legacy_office", _LEGACY_CONVERSION, kind=f"legacy {suffix}"
        )
    if not is_readable(filename):
        return ContentStatus(False, "unsupported", _unsupported_detail(filename))
    return ContentStatus(
        False, "not_read", "extractable format; call read_course_material to read it"
    )



# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_TAG_BLOCKS = re.compile(
    r"</\s*(p|div|li|tr|h[1-6]|section|article|blockquote|pre)\s*>", re.IGNORECASE
)
_BR = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_SCRIPT = re.compile(r"<\s*(script|style)\b.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL)
_ANY_TAG = re.compile(r"<[^>]+>")
_MANY_BLANK = re.compile(r"\n{3,}")


def html_to_text(body: str) -> str:
    """Reduce Canvas rich text / page HTML to readable plain text.

    Drops script and style CONTENTS (not just their tags), so a page's
    JavaScript never reads as prose, then turns block boundaries into newlines.
    """
    if not body:
        return ""
    text = _SCRIPT.sub(" ", body)
    text = _BR.sub("\n", text)
    text = _TAG_BLOCKS.sub("\n", text)
    text = _ANY_TAG.sub(" ", text)
    text = html_lib.unescape(text)
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines()]
    return _MANY_BLANK.sub("\n\n", "\n".join(lines)).strip()


# ---------------------------------------------------------------------------
# OOXML helpers
# ---------------------------------------------------------------------------


def _paragraphs_text(root, namespace: str) -> str:
    """One line per paragraph, so bullets stay separate instead of running together."""
    paragraphs = []
    for paragraph in root.iter(namespace + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(namespace + "t")).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _image_parts(archive: zipfile.ZipFile, rels_name: str, base: str) -> list[bytes]:
    """Read the images a slide (or document) part points at, via its rels file."""
    try:
        root = ElementTree.fromstring(archive.read(rels_name))
    except (KeyError, ElementTree.ParseError):
        return []

    images: list[bytes] = []
    for relationship in root.iter(_R + "Relationship"):
        if not str(relationship.get("Type", "")).endswith("/image"):
            continue
        target = str(relationship.get("Target", ""))
        if not target:
            continue
        # Targets are relative ("../media/image1.png"); resolve against the part.
        candidate = target.lstrip("/")
        if target.startswith("../"):
            candidate = f"{base}/{target[3:]}"
        try:
            images.append(archive.read(candidate))
        except KeyError:
            continue
    return images


def _ocr_images(images: list[bytes], label: str, engine) -> str:
    """OCR up to the per-slide cap and return the recognised text."""
    chunks: list[str] = []
    for image in images[:_MAX_OCR_IMAGES_PER_SLIDE]:
        try:
            text = engine.recognize(image).strip()
        except Exception:
            continue
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def _extract_pptx(data: bytes) -> tuple[list[Section], list[str]]:
    sections: list[Section] = []
    warnings: list[str] = []
    engine = ocr.backend()
    ocr_slides = 0

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        slides: dict[int, str] = {}
        for name in names:
            match = _SLIDE_RE.match(name)
            if match:
                slides[int(match.group(1))] = name
        notes: dict[int, str] = {}
        for name in names:
            match = _NOTES_RE.match(name)
            if match:
                notes[int(match.group(1))] = name

        for index in sorted(slides):
            # One unreadable slide must not cost the caller the other forty.
            try:
                root = ElementTree.fromstring(archive.read(slides[index]))
                body = _paragraphs_text(root, _A)
            except Exception as exc:
                warnings.append(f"Slide {index} could not be read: {type(exc).__name__}")
                sections.append(Section(f"Slide {index}", ""))
                continue

            note_text = ""
            if index in notes:
                try:
                    note_root = ElementTree.fromstring(archive.read(notes[index]))
                    note_text = _paragraphs_text(note_root, _A).strip()
                except Exception:
                    pass

            # A slide with no text is usually a screenshot or a diagram. OCR it
            # rather than reporting an empty slide as if the deck said nothing.
            if not body.strip() and engine is not None:
                slide_part = slides[index]
                rels = slide_part.replace("ppt/slides/", "ppt/slides/_rels/") + ".rels"
                images = _image_parts(archive, rels, "ppt")
                if images:
                    body = _ocr_images(images, f"Slide {index}", engine)
                    if body.strip():
                        ocr_slides += 1

            parts = [body]
            if note_text:
                parts.append(f"(speaker notes) {note_text}")
            merged = "\n".join(part for part in parts if part.strip())
            sections.append(Section(f"Slide {index}", merged))

    empty = [section.label for section in sections if not section.text.strip()]
    if ocr_slides:
        warnings.append(
            f"{ocr_slides} slide(s) had no text and were read with OCR "
            f"({getattr(engine, 'name', 'ocr')}); OCR can misread symbols and formulas."
        )
    if empty:
        if engine is None:
            warnings.append(
                f"{len(empty)} of {len(sections)} slides contain no extractable text "
                f"(screenshots or images). {ocr.unavailable_reason()}"
            )
        else:
            warnings.append(
                f"{len(empty)} of {len(sections)} slides still had no readable text "
                "after OCR — they may be decorative images or a scan too poor to read."
            )
    return sections, warnings


def _extract_docx(data: bytes) -> tuple[list[Section], list[str]]:
    warnings: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
        paragraphs = []
        for paragraph in root.iter(_W + "p"):
            text = "".join(node.text or "" for node in paragraph.iter(_W + "t")).strip()
            if text:
                paragraphs.append(text)
        body = "\n".join(paragraphs)

        if not body.strip():
            engine = ocr.backend()
            if engine is None:
                warnings.append(
                    "This document has no text (it may be scanned images). "
                    f"{ocr.unavailable_reason()}"
                )
            else:
                images = _image_parts(
                    archive, "word/_rels/document.xml.rels", "word"
                )[:_MAX_OCR_DOCX_IMAGES]
                recognised = _ocr_images(images, "Document", engine)
                if recognised.strip():
                    body = recognised
                    warnings.append(
                        f"Document had no text layer and was read with OCR "
                        f"({getattr(engine, 'name', 'ocr')}); check formulas by eye."
                    )
                else:
                    warnings.append(
                        "Document has no text and OCR found none in its images."
                    )
    return [Section("Document", body)], warnings


def _extract_xlsx(data: bytes) -> tuple[list[Section], list[str]]:
    sections: list[Section] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in shared_root.iter(_X + "si"):
                shared.append("".join(node.text or "" for node in item.iter(_X + "t")))
        sheets = sorted(
            (int(_SHEET_RE.match(name).group(1)), name)  # type: ignore[union-attr]
            for name in names
            if _SHEET_RE.match(name)
        )
        for index, name in sheets:
            root = ElementTree.fromstring(archive.read(name))
            rows = []
            for row in root.iter(_X + "row"):
                values = []
                for cell in row.iter(_X + "c"):
                    value_node = cell.find(_X + "v")
                    if value_node is None or value_node.text is None:
                        continue
                    raw = value_node.text
                    if cell.get("t") == "s":
                        try:
                            raw = shared[int(raw)]
                        except (ValueError, IndexError):
                            pass
                    values.append(raw)
                if values:
                    rows.append(" | ".join(values))
            sections.append(Section(f"Sheet {index}", "\n".join(rows)))
    return sections, []


def _extract_ipynb(data: bytes) -> tuple[list[Section], list[str]]:
    try:
        notebook = json.loads(data.decode("utf-8", errors="replace"))
    except ValueError:
        return [], ["Notebook is not valid JSON."]
    sections = []
    for index, cell in enumerate(notebook.get("cells", []), start=1):
        source = cell.get("source", "")
        text = "".join(source) if isinstance(source, list) else str(source)
        if text.strip():
            sections.append(Section(f"Cell {index}", text.strip()))
    return sections, []


_TIMESTAMP_LINE = re.compile(r"-->|^\d+$")


def _extract_transcript(text: str) -> tuple[list[Section], list[str]]:
    """Flatten a WebVTT/SRT caption track into prose.

    Cue numbers and timestamps carry no meaning for studying, so they are
    dropped; consecutive duplicate cues (common in auto-captions) are collapsed.
    """
    lines: list[str] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        keep = []
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or _TIMESTAMP_LINE.search(stripped):
                continue
            if stripped.upper().startswith(("WEBVTT", "NOTE ", "KIND:", "LANGUAGE:")):
                continue
            keep.append(re.sub(r"<[^>]+>", "", stripped))
        cue = " ".join(keep).strip()
        if cue and (not lines or lines[-1] != cue):
            lines.append(cue)
    body = "\n".join(lines)
    if not body:
        return [], ["Caption file contained no text cues."]
    return [Section("Transcript", body)], []


def _page_box(page) -> tuple[float, float] | None:
    """Render-aware page size, or None when the PDF's page box cannot be read."""
    try:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
    except Exception:
        return None
    try:
        rotation = int(page.get("/Rotate", 0) or 0)
    except Exception:
        rotation = 0
    if rotation % 180 == 90:
        width, height = height, width
    return width, height


def _pages_share_slide_shape(boxes: list[tuple[float, float]]) -> bool:
    """One repeated landscape page size at a slide aspect ratio (4:3 ... 16:9)."""
    if len(boxes) < 2:
        return False
    width, height = boxes[0]
    if height <= 0 or width <= height:
        return False
    if not (_SLIDE_ASPECT_RANGE[0] <= width / height <= _SLIDE_ASPECT_RANGE[1]):
        return False
    return all(
        abs(other_width - width) <= _SLIDE_PAGE_SIZE_TOLERANCE
        and abs(other_height - height) <= _SLIDE_PAGE_SIZE_TOLERANCE
        for other_width, other_height in boxes
    )


def _text_is_slide_shaped(texts: list[str]) -> bool:
    """Slides hold a title and bullets; a prose page is a block of paragraphs."""
    counts = sorted(len(text.split()) for text in texts)
    if not counts:
        return False
    median = counts[len(counts) // 2]
    return median <= _SLIDE_MEDIAN_WORDS_MAX and counts[-1] <= _SLIDE_MAX_WORDS


def _pdf_looks_like_a_deck(pages, texts: list[str]) -> bool:
    """Both clues must agree: repeated slide-shaped pages *and* slide-sparse text.

    Canvas hands over lecture slides as PDFs constantly, and labeling those pages
    ``Slide N`` (instead of ``p.N``) keeps section reads, the budget cut and the
    unread list talking about the same things the professor's deck does. The text
    check is what stops a landscape report, table or wide handout — same page
    shape, paragraphs of prose — from being mislabelled as a slide deck.
    """
    pages = list(pages)
    boxes = [box for box in (_page_box(page) for page in pages) if box is not None]
    if len(boxes) != len(pages):
        return False
    return _pages_share_slide_shape(boxes) and _text_is_slide_shaped(texts)


def _extract_pdf(data: bytes) -> tuple[list[Section], list[str]]:
    warnings: list[str] = []
    reader = PdfReader(io.BytesIO(data))
    pages = list(reader.pages)

    # Text comes first: whether this is a deck depends on how the pages read, not
    # only on their shape, so labels are chosen after the text is in hand.
    texts: list[str] = []
    unreadable: list[tuple[int, str]] = []
    for index, page in enumerate(pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # a broken page must not lose the whole file
            text = ""
            unreadable.append((index, type(exc).__name__))
        texts.append(re.sub(r"\n{3,}", "\n\n", text).strip())

    deck = _pdf_looks_like_a_deck(pages, texts)
    noun, nouns = ("slide", "slides") if deck else ("page", "pages")

    def label_for(number: int) -> str:
        return f"Slide {number}" if deck else f"p.{number}"

    for number, error in unreadable:
        warnings.append(f"{label_for(number)} could not be read: {error}")

    sections = [
        Section(label_for(index), text) for index, text in enumerate(texts, start=1)
    ]

    empty = [index for index, section in enumerate(sections) if not section.text.strip()]
    if not empty:
        return sections, warnings

    engine = ocr.backend()
    if engine is None:
        warnings.append(
            f"{len(empty)} of {len(sections)} {nouns} have no text layer (scanned or "
            f"image-only) and were not read. {ocr.unavailable_reason()}"
        )
        return sections, warnings

    # OCR only the pages that need it, oldest-first, capped so a 300-page scan
    # does not look like a hang. Overflow pages are reported, not dropped.
    targets = empty[: ocr.MAX_OCR_PAGES]
    numbers = [index + 1 for index in targets]
    try:
        images = engine.pdf_page_images(data, numbers)
    except Exception as exc:
        warnings.append(
            f"Could not render {len(targets)} image-only {noun}(s) for OCR "
            f"({type(exc).__name__}: {exc})."
        )
        images = []

    ocr_pages = 0
    for page_index, image in zip(targets, images, strict=False):
        try:
            recognised = engine.recognize(image)
        except Exception:
            continue
        cleaned = re.sub(r"\n{3,}", "\n\n", recognised or "").strip()
        if cleaned:
            sections[page_index] = Section(sections[page_index].label, cleaned)
            ocr_pages += 1

    if ocr_pages:
        warnings.append(
            f"{ocr_pages} page(s) had no text layer and were read with OCR "
            f"({getattr(engine, 'name', 'ocr')}); OCR can misread symbols and formulas."
        )
    still_empty = [index for index in empty if not sections[index].text.strip()]
    if still_empty:
        warnings.append(
            f"{len(still_empty)} of {len(sections)} {nouns} still have no readable "
            "text after OCR — they may be blank scans or diagrams."
        )
    if len(empty) > len(targets):
        warnings.append(
            f"{len(empty) - len(targets)} further image-only {nouns} were not OCR'd "
            f"(limit {ocr.MAX_OCR_PAGES} per file). Read them individually if needed."
        )
    return sections, warnings


def _looks_binary(data: bytes) -> bool:
    """Control bytes (other than tab/newline/return) mean this is not text."""
    sample = data[:4096]
    if not sample:
        return False
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False  # UTF-16 with a BOM is text
    control = sum(1 for byte in sample if byte < 9 or 13 < byte < 32)
    return control / len(sample) > 0.05


def _decode_text(data: bytes) -> str | None:
    """Decode text, or None when the bytes are binary / undecodable."""
    if _looks_binary(data):
        return None
    encodings = ["utf-8-sig"]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append("utf-16")
    encodings.append("cp1252")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_bytes(filename: str, data: bytes) -> Extracted:
    """Extract readable text from a file's bytes, choosing by extension."""
    name = filename or "file"
    suffix = Path(name).suffix.lower()
    kind = suffix.lstrip(".") or "unknown"

    if suffix in _LEGACY_OFFICE:
        modern_suffix, family = _LEGACY_OFFICE[suffix]
        if data.startswith(_ZIP_MAGIC):
            # Mislabeled modern file: read it as what it really is.
            return extract_bytes(Path(name).with_suffix(modern_suffix).name, data)
        if data.startswith(_OLE_MAGIC):
            return Extracted(
                name,
                kind,
                warnings=[
                    f"'{suffix}' is a legacy {family} binary document, not text. "
                    f"To read it, {_LEGACY_CONVERSION}."
                ],
            )
        return Extracted(
            name,
            kind,
            warnings=[
                f"'{suffix}' is not a valid {family} document (neither a modern "
                "Office file nor a legacy OLE binary)."
            ],
        )

    if len(data) > MAX_EXTRACT_BYTES:
        return Extracted(
            name, kind,
            warnings=[
                f"File is {len(data) / (1024 * 1024):.1f} MB, over the "
                f"{MAX_EXTRACT_BYTES // (1024 * 1024)} MB extraction limit."
            ],
        )

    try:
        if suffix == ".pdf":
            sections, warnings = _extract_pdf(data)
        elif suffix in (".pptx", ".pptm"):
            sections, warnings = _extract_pptx(data)
        elif suffix == ".docx":
            sections, warnings = _extract_docx(data)
        elif suffix in (".xlsx", ".xlsm"):
            sections, warnings = _extract_xlsx(data)
        elif suffix == ".ipynb":
            sections, warnings = _extract_ipynb(data)
        elif suffix in _TRANSCRIPT_SUFFIXES:
            decoded = _decode_text(data)
            if decoded is None:
                sections, warnings = [], [_BINARY_HINT]
            else:
                sections, warnings = _extract_transcript(decoded)
        elif suffix in (".html", ".htm"):
            decoded = _decode_text(data)
            if decoded is None:
                sections, warnings = [], [_BINARY_HINT]
            else:
                sections, warnings = [Section("Page", html_to_text(decoded))], []
        elif suffix in _READABLE_SUFFIXES:
            decoded = _decode_text(data)
            if decoded is None:
                sections, warnings = [], [_BINARY_HINT]
            else:
                sections, warnings = [Section("Text", decoded)], []
        else:
            return Extracted(
                name, kind,
                warnings=[
                    f"'{suffix or 'no extension'}' is not a readable text format. "
                    "Images, audio and video cannot be read as text. If this file "
                    "belongs to a Panopto/Kaltura/YouTube item, look for a "
                    "transcript or captions file (.vtt/.srt) in the course files."
                ],
            )
    except zipfile.BadZipFile:
        return Extracted(name, kind, warnings=["File is corrupt or not a valid Office document."])
    except Exception as exc:
        return Extracted(
            name, kind,
            warnings=[f"Extraction failed ({type(exc).__name__}): {exc}"],
        )

    trimmed = [
        Section(section.label, section.text[:MAX_SECTION_CHARS]) for section in sections
    ]
    return Extracted(name, kind, trimmed, warnings)


def extract_path(path: str | Path) -> Extracted:
    """Extract text from a local file."""
    file_path = Path(path)
    if not file_path.is_file():
        return Extracted(file_path.name, "unknown", warnings=[f"No such file: {file_path}"])
    return extract_bytes(file_path.name, file_path.read_bytes())
