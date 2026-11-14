"""OCR for pages and slides that are images.

A scanned PDF or a deck exported as screenshots has no text layer, so the plain
extractors return empty sections. OCR is part of canvas-mcp — there is no extra
to install: ``pypdfium2`` renders pages and ``rapidocr-onnxruntime`` reads them,
both shipped as normal dependencies.

The engine is loaded lazily because onnxruntime costs seconds and memory at
import time, and ``backend()`` stays the single seam tests replace. If a broken
install means the engine cannot load, the extractors report that honestly
instead of passing an empty result off as "the file has no text".
"""

from __future__ import annotations

import functools
import importlib.util
import io
from dataclasses import dataclass
from typing import Protocol

MAX_OCR_PAGES = 25

INSTALL_HINT = (
    "OCR ships with canvas-mcp, so this install is incomplete; reinstall it with "
    "`uv sync --reinstall` (or `pip install --force-reinstall canvas-mcp`)."
)

_OCR_MODULES = ("pypdfium2", "PIL", "rapidocr_onnxruntime")


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
    """pypdfium2 renders pages, rapidocr-onnxruntime reads the images."""

    engine: object
    name: str = "rapidocr"

    def pdf_page_images(self, data: bytes, page_numbers: list[int]) -> list[bytes]:
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
        import numpy as np
        from PIL import Image

        array = np.array(Image.open(io.BytesIO(image)).convert("RGB"))
        result, _ = self.engine(array)
        if not result:
            return ""
        return "\n".join(str(line[1]) for line in result if len(line) > 1)


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


@functools.lru_cache(maxsize=1)
def _installed_backend() -> tuple[OcrBackend | None, str]:
    """Detect the bundled engine, or explain exactly what is broken."""
    missing = [name for name in _OCR_MODULES if not _module_present(name)]
    if missing:
        return None, (
            "OCR is bundled with canvas-mcp but missing here ("
            + ", ".join(missing)
            + "). "
            + INSTALL_HINT
        )
    try:
        from rapidocr_onnxruntime import RapidOCR

        return _RapidOcrBackend(engine=RapidOCR()), ""
    except Exception as exc:
        return None, (
            "OCR is bundled with canvas-mcp but its engine did not load "
            f"({type(exc).__name__}: {exc}). " + INSTALL_HINT
        )


def backend() -> OcrBackend | None:
    """The engine to use, or None when this install cannot run OCR."""
    return _installed_backend()[0]


def unavailable_reason() -> str:
    """Why OCR cannot run here, in a sentence the user can act on."""
    return _installed_backend()[1]


def is_available() -> bool:
    return backend() is not None


def reset_detection_cache() -> None:
    """Forget a previous detection result (used after reinstalling)."""
    _installed_backend.cache_clear()
