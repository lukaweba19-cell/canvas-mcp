"""Turn Canvas course files into text the model can actually read.

Canvas hands back bytes. A model cannot read a PPTX, and base64 of a slide deck
is noise. This module extracts text (and, for slides, the speaker notes, which
are usually the professor's own explanation of what matters) once, and caches
the result so repeat reads cost nothing.

Formats:
- PDF          -> one section per page, via pypdf
- PPTX/PPTM    -> one section per slide + its notes
- DOCX         -> one section for the document
- XLSX         -> one section per sheet
- IPYNB        -> one section per cell
- HTML/MD/TXT  -> one section
Everything else (images, audio, video, archives) is reported as unreadable
rather than silently returning garbage.
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

# OOXML namespaces
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Guard rails: Canvas permits very large files, and extraction buffers in memory.
MAX_EXTRACT_BYTES = 60 * 1024 * 1024
MAX_SECTION_CHARS = 40_000

_READABLE_SUFFIXES = {
    ".pdf", ".pptx", ".pptm", ".docx", ".xlsx", ".xlsm", ".ipynb",
    ".html", ".htm", ".txt", ".md", ".markdown", ".csv", ".json",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".rb", ".go", ".rs",
    ".sql", ".r", ".rmd", ".xml", ".yaml", ".yml", ".tex",
}

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

    def toc(self, max_labels: int = 200) -> str:
        """A compact index — the right thing to show before the full text."""
        if not self.sections:
            return "(no readable text)"
        lines = []
        for section in self.sections[:max_labels]:
            words = len(section.text.split())
            if words == 0:
                lines.append(f"- {section.label}: (empty / image only)")
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


def _xml_text(elements, tag: str) -> str:
    return "".join(node.text or "" for node in elements.iter(tag))


def _paragraphs_text(root, namespace: str) -> str:
    """One line per paragraph, so bullets stay separate instead of running together."""
    paragraphs = []
    for paragraph in root.iter(namespace + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(namespace + "t")).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _extract_pptx(data: bytes) -> tuple[list[Section], list[str]]:
    sections: list[Section] = []
    warnings: list[str] = []
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

            parts = [body]
            if note_text:
                parts.append(f"(speaker notes) {note_text}")
            merged = "\n".join(part for part in parts if part.strip())
            sections.append(Section(f"Slide {index}", merged))
    if not any(section.text.strip() for section in sections):
        warnings.append(
            "No text found in the slides — they may be images of slides, which "
            "this server cannot read without OCR."
        )
    return sections, warnings


def _extract_docx(data: bytes) -> tuple[list[Section], list[str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs = []
    for paragraph in root.iter(_W + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(_W + "t")).strip()
        if text:
            paragraphs.append(text)
    return [Section("Document", "\n".join(paragraphs))], []


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


def _extract_pdf(data: bytes) -> tuple[list[Section], list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return [], [
            "PDF support requires the 'pypdf' package. Install it with "
            "`uv sync` (or `pip install pypdf`) and try again."
        ]

    warnings: list[str] = []
    reader = PdfReader(io.BytesIO(data))
    sections: list[Section] = []
    empty_pages = 0
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # a broken page must not lose the whole file
            text = ""
            warnings.append(f"Page {index} could not be read: {type(exc).__name__}")
        cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not cleaned:
            empty_pages += 1
        sections.append(Section(f"p.{index}", cleaned))
    if sections and empty_pages == len(sections):
        warnings.append(
            "Every page is image-only (a scan or a deck exported as pictures). "
            "Text extraction needs OCR for this file."
        )
    elif empty_pages:
        warnings.append(
            f"{empty_pages} of {len(sections)} pages had no extractable text "
            "(likely scanned images)."
        )
    return sections, warnings


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_bytes(filename: str, data: bytes) -> Extracted:
    """Extract readable text from a file's bytes, choosing by extension."""
    name = filename or "file"
    suffix = Path(name).suffix.lower()
    kind = suffix.lstrip(".") or "unknown"

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
        elif suffix in (".html", ".htm"):
            text = html_to_text(_decode_text(data))
            sections, warnings = [Section("Page", text)], []
        elif suffix in _READABLE_SUFFIXES:
            sections, warnings = [Section("Text", _decode_text(data))], []
        else:
            return Extracted(
                name, kind,
                warnings=[
                    f"'{suffix or 'no extension'}' is not a readable text format. "
                    "Images, audio and video cannot be read as text."
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
