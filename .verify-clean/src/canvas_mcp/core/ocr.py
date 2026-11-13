"""Optional OCR for pages and slides that are images.

A scanned PDF or a deck exported as screenshots has no text layer, so the plain
extractors return empty sections. This module is the fallback: it rasterizes PDF
pages with ``pypdfium2`` and recognizes text with either ``rapidocr-onnxruntime``
(a pip-only install) or ``pytesseract`` (needs the ``tesseract`` binary).

OCR is deliberately optional. Nothing here is imported at module import time, so
a machine without the extra keeps working and gets an actionable message instead
of a crash or a silent empty result.

    uv sync --extra ocr          # or: pip install "canvas-mcp[ocr]"

``backend()`` is the single seam tests replace, so the plumbing (which pages are
OCR'd, how their text is merged, how failures are reported) can be exercised
without a 200 MB dependency.
"""

from __future__ import annotations

import functools
import importlib.util
import shutil
from dataclasses import dataclass
from typing import Protocol

# Bound the work: OCR is seconds per page, so an unbounded 400-page scan would
# look like a hang. Pages past this limit are reported, not silently skipped.
MAX_OCR_PAGES = 25

OCR_EXTRA = "ocr"

INSTALL_HINT = (
    "Install OCR support with `uv sync --extra ocr` "
    "(or `pip install 'canvas-mcp[ocr]'`), then re-run with refresh=true."
)

_TESSERACT_HINT = (
    "pytesseract is installed but the `tesseract` binary is not on PATH. "
    "Install it (e.g. `apt install tesseract-ocr`) or use the pip-only backend: "
    "`uv sync --extra ocr`."
)


class OcrBackend(Protocol):
    """What the extractors need from an OCR engine."""

    name: str

    def pdf_page_images(self, data: bytes, page_numbers: list[int]) -> list[bytes]:
        """Render 1-based PDF pages to PNG bytes, in the order requested."""
        ...

    def recognize(self, image: bytes) -> str:
        """Return the text found in one PNG image ("" when nothing is found)."""
        ...


@dataclass
class _RapidOcrBackend:
    """pip-only backend: pypdfium2 renders, rapidocr-onnxruntime reads."""

    engine: object
    name: str = "rapidocr"

    def pdf_page_images(self, data: bytes, page_numbers: list[int]) -> list[bytes]:
        import io

        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(data)
        images: list[bytes] = []
        try:
            for number in page_numbers:
                page = document[number - 1]
                bitmap = page.render(scale=2.5)
                buffer = io.BytesIO()
                bitmap.to_pil().save(buffer, format="PNG")
                images.append(buffer.getvalue())
                bitmap.close()
                page.close()
        finally:
            document.close()
        return images

    def recognize(self, image: bytes) -> str:
        import io

        import numpy as np
        from PIL import Image

        array = np.array(Image.open(io.BytesIO(image)).convert("RGB"))
        result, _ = self.engine(array)  # type: ignore[operator]
        if not result:
            return ""
        return "\n".join(str(line[1]) for line in result if len(line) > 1)


@dataclass
class _TesseractBackend:
    """pytesseract backend; requires the tesseract binary on PATH."""

    name: str = "tesseract"

    def pdf_page_images(self, data: bytes, page_numbers: list[int]) -> list[bytes]:
        import io

        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(data)
        images: list[bytes] = []
        try:
            for number in page_numbers:
                page = document[number - 1]
                bitmap = page.render(scale=2.5)
                buffer = io.BytesIO()
                bitmap.to_pil().save(buffer, format="PNG")
                images.append(buffer.getvalue())
                bitmap.close()
                page.close()
        finally:
            document.close()
        return images

    def recognize(self, image: bytes) -> str:
        import io

        import pytesseract
        from PIL import Image

        return str(pytesseract.image_to_string(Image.open(io.BytesIO(image))))


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


@functools.lru_cache(maxsize=1)
def _installed_backend() -> tuple[OcrBackend | None, str]:
    """Detect an available engine, or explain exactly what is missing."""
    if not _module_present("pypdfium2"):
        return None, (
            "No PDF rasterizer is installed, so image-only pages cannot be OCR'd. "
            + INSTALL_HINT
        )
    if not _module_present("PIL"):
        return None, "Pillow is missing, which OCR needs to read rendered pages. " + INSTALL_HINT

    if _module_present("rapidocr_onnxruntime"):
        try:
            from rapidocr_onnxruntime import RapidOCR

            return _RapidOcrBackend(engine=RapidOCR()), ""
        except Exception as exc:  # model files missing, onnxruntime broken, ...
            rapid_error = f"{type(exc).__name__}: {exc}"
    else:
        rapid_error = "rapidocr-onnxruntime is not installed"

    if _module_present("pytesseract"):
        if shutil.which("tesseract"):
            return _TesseractBackend(), ""
        return None, f"{_TESSERACT_HINT} ({rapid_error})"

    return None, (
        "No OCR engine is installed (neither rapidocr-onnxruntime nor "
        f"pytesseract; {rapid_error}). " + INSTALL_HINT
    )


def backend() -> OcrBackend | None:
    """The engine to use, or None when OCR is unavailable.

    Tests replace this function to exercise the OCR paths without installing
    the optional dependency; production callers always get the detected engine.
    """
    return _installed_backend()[0]


def unavailable_reason() -> str:
    """Why OCR cannot run here, in a sentence the user can act on."""
    return _installed_backend()[1]


def is_available() -> bool:
    return backend() is not None


def reset_detection_cache() -> None:
    """Forget a previous detection result (used after installing the extra)."""
    _installed_backend.cache_clear()
