"""Convert legacy Office binaries with a locally installed LibreOffice.

Word 97-2003 ``.doc`` and Excel ``.xls`` are parsed in process (``legacy-doc``
and ``xlrd``). PowerPoint 97-2003 ``.ppt`` has no pure-Python reader, so the
only honest option is the converter every machine that opens such a file already
has: LibreOffice. When it is not installed the caller gets the exact install
step instead of a vague "unsupported format".
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

CONVERTER_NAMES = ("soffice", "libreoffice")
CONVERT_TIMEOUT_SECONDS = 180

INSTALL_HINT = (
    "install LibreOffice (which provides `soffice`) or save the file as a modern "
    "Office format"
)


def converter_path() -> str | None:
    """The LibreOffice executable on this machine, if there is one."""
    for name in CONVERTER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def convert_legacy_binary(
    data: bytes,
    source_suffix: str,
    target_suffix: str,
) -> tuple[bytes | None, str | None]:
    """Convert ``data`` from a legacy suffix to a modern one. ``(bytes, error)``.

    Returns an error string instead of raising, because every caller turns this
    into student-facing guidance ("install LibreOffice") rather than a crash.
    """
    executable = converter_path()
    if executable is None:
        return None, f"Converting {source_suffix} files needs LibreOffice: {INSTALL_HINT}."

    with tempfile.TemporaryDirectory(prefix="canvas-mcp-convert-") as directory:
        source = Path(directory) / f"input{source_suffix}"
        source.write_bytes(data)
        try:
            completed = subprocess.run(
                [
                    executable,
                    "--headless",
                    "--norestore",
                    "--convert-to",
                    target_suffix.lstrip("."),
                    "--outdir",
                    directory,
                    str(source),
                ],
                capture_output=True,
                timeout=CONVERT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"LibreOffice could not run to convert {source_suffix}: {exc}"

        converted = Path(directory) / f"input{target_suffix}"
        if not converted.is_file():
            detail = (completed.stderr or completed.stdout or b"").decode(
                "utf-8", errors="replace"
            ).strip()
            detail = f": {detail[:300]}" if detail else ""
            return None, (
                f"LibreOffice could not convert this {source_suffix} file{detail}. "
                f"Try opening it locally, or {INSTALL_HINT}."
            )
        return converted.read_bytes(), None
