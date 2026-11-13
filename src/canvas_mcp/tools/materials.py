"""Reading course material: slides, PDFs, Word docs, pages.

Canvas returns file bytes. These tools turn them into text the model can study
from, cache the text next to the student's workspace, and return an index first
so a 60-slide deck never floods the conversation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core import extract, student_store
from ..core.cache import get_course_code, get_course_id
from ..core.client import (
    canvas_authenticated_client,
    fetch_all_paginated_results,
    make_canvas_request,
)
from ..core.untrusted_content import fence_untrusted, fence_untrusted_inline
from ..core.validation import coerce_canvas_id, validate_params

_MAX_SECTION_RETURN = 20_000
_MAX_TOC_LINES = 60


def _cache_file(
    course_identifier: str | int, course_code: str | None, file_id: str | int, filename: str
) -> Path:
    directory = student_store.materials_dir(course_identifier, course_code)
    return directory / f"{file_id}-{student_store.slug(filename, 50)}.json"


def _find_cache(
    course_identifier: str | int, course_code: str | None, file_id: str | int
) -> Path | None:
    directory = student_store.materials_dir(course_identifier, course_code)
    matches = sorted(directory.glob(f"{file_id}-*.json"))
    return matches[0] if matches else None


def _load_cache(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _download_course_file_bytes(download_url: str) -> tuple[bytes | None, str | None]:
    """Fetch a Canvas file's bytes using the caller's own credentials."""
    try:
        buffer = bytearray()
        async with canvas_authenticated_client() as client:
            async with client.stream("GET", download_url, follow_redirects=True) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    buffer.extend(chunk)
        return bytes(buffer), None
    except Exception as exc:  # network, auth, storage redirect
        return None, f"{type(exc).__name__}: {exc}"


def register_material_tools(mcp: FastMCP) -> None:
    """Register course-material reading tools."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_content_map(course_identifier: str | int) -> str:
        """Map everything readable in a course, so you know what you can study.

        Lists each module item with its type and the tool that reads it, then
        lists course files with whether their text can be extracted. Use this
        before building a study guide.

        Args:
            course_identifier: Course code or Canvas ID
        """
        course_id = await get_course_id(course_identifier)
        course_display = await get_course_code(course_id) or course_identifier

        modules = await fetch_all_paginated_results(
            f"/courses/{course_id}/modules", {"per_page": 100, "include[]": ["items"]}
        )
        lines = [f"Content map for {course_display}:\n"]

        if isinstance(modules, dict) and "error" in modules:
            lines.append(f"Could not list modules: {modules['error']}")
        elif not modules:
            lines.append("No modules in this course.")
        else:
            readable_by_type = {
                "Page": "read_page_text",
                "File": "read_course_material",
                "Assignment": "get_assignment_details",
                "Discussion": "list_discussion_entries",
                "Quiz": "not supported (quizzes cannot be read or taken)",
                "ExternalUrl": "not readable (opens outside Canvas)",
                "ExternalTool": "not readable (LTI tool, e.g. Panopto/Gradescope)",
                "SubHeader": "heading only",
            }
            for module in modules:
                lines.append(
                    f"\n## {fence_untrusted_inline(module.get('name', 'Untitled'), 'module name')}"
                )
                for item in module.get("items", []):
                    item_type = item.get("type", "Unknown")
                    title = item.get("title", "Untitled")
                    ids = []
                    if item.get("content_id"):
                        ids.append(f"content_id={item['content_id']}")
                    if item.get("page_url"):
                        ids.append(f"page_url={item['page_url']}")
                    if item.get("external_url"):
                        ids.append("external link")
                    hint = readable_by_type.get(item_type, "unknown")
                    lines.append(
                        f"- [{item_type}] {fence_untrusted_inline(title, 'module item title')}"
                        f" ({', '.join(ids) or 'no content id'}) -> {hint}"
                    )

        files = await fetch_all_paginated_results(
            f"/courses/{course_id}/files", {"per_page": 100}
        )
        lines.append("\n## Course files")
        if isinstance(files, dict) and "error" in files:
            lines.append(f"Could not list files: {files['error']}")
        elif not files:
            lines.append("No files.")
        else:
            for file in files:
                name = file.get("display_name") or file.get("filename", "unknown")
                readable = "readable" if extract.is_readable(name) else "NOT readable as text"
                lines.append(
                    f"- file_id={file.get('id')} {fence_untrusted_inline(name, 'file name')}"
                    f" ({readable})"
                )

        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def read_course_material(
        course_identifier: str | int,
        file_id: str | int,
        refresh: bool = False,
    ) -> str:
        """Extract the text of a course file (PDF, slides, Word, Excel, notebook).

        Downloads the file once, extracts text, caches it in the student
        workspace, and returns a section index plus the cache path — NOT the
        whole text. Read individual sections with read_material_section. Slide
        speaker notes are included, since they usually say what matters.

        Args:
            course_identifier: Course code or Canvas ID
            file_id: Canvas file ID (see get_course_content_map or list_course_files)
            refresh: Re-download and re-extract even if a cache exists
        """
        validated = coerce_canvas_id(file_id)
        if validated is None:
            return "Error: file_id must be a numeric Canvas file ID."
        file_id = validated

        course_id = await get_course_id(course_identifier)
        course_code = await get_course_code(course_id)
        cache = _find_cache(course_id, course_code, file_id)

        if cache and not refresh:
            cached = _load_cache(cache)
            if cached:
                return _describe_cached(cached, str(cache))

        info = await make_canvas_request("get", f"/courses/{course_id}/files/{file_id}")
        if isinstance(info, dict) and "error" in info:
            return f"Error getting file info: {info['error']}"

        filename = info.get("display_name") or info.get("filename") or f"file-{file_id}"
        download_url = info.get("url")
        if not download_url:
            return "Error: Canvas returned no download URL for this file (check file permissions)."

        data, error = await _download_course_file_bytes(download_url)
        if data is None:
            return f"Error downloading '{filename}': {error}"

        extracted = extract.extract_bytes(filename, data)
        record = {
            "file_id": file_id,
            "course_id": course_id,
            "filename": filename,
            "kind": extracted.kind,
            "bytes": len(data),
            "warnings": extracted.warnings,
            "sections": [
                {"label": section.label, "text": section.text} for section in extracted.sections
            ],
        }

        cache = _cache_file(course_id, course_code, file_id, filename)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(record), encoding="utf-8")
        (cache.with_suffix(".orig")).write_bytes(data)

        return _describe_cached(record, str(cache))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def read_material_section(
        course_identifier: str | int,
        file_id: str | int,
        section: str,
    ) -> str:
        """Read one page/slide/sheet of a file already extracted.

        Args:
            course_identifier: Course code or Canvas ID
            file_id: Canvas file ID
            section: Section label ("Slide 4", "p.12"), its number ("4"), or its 1-based position
        """
        course_id = await get_course_id(course_identifier)
        course_code = await get_course_code(course_id)
        cache = _find_cache(course_id, course_code, file_id)
        if cache is None:
            return (
                "That file has not been extracted yet. Call read_course_material "
                "with this file_id first."
            )
        record = _load_cache(cache)
        if not record:
            return "The cached extraction is unreadable. Re-run read_course_material with refresh=true."

        extracted = extract.Extracted(
            filename=record.get("filename", "file"),
            kind=record.get("kind", "unknown"),
            sections=[extract.Section(item["label"], item["text"]) for item in record.get("sections", [])],
        )
        found = extracted.section(section)
        if found is None:
            available = ", ".join(item["label"] for item in record.get("sections", [])[:15])
            return f"No section '{section}'. Available: {available}"

        body = found.text[:_MAX_SECTION_RETURN]
        suffix = "" if len(found.text) <= _MAX_SECTION_RETURN else "\n...[section truncated]"
        return (
            f"{fence_untrusted_inline(record.get('filename', 'file'), 'file name')} "
            f"— {found.label}\n\n"
            + fence_untrusted(body or "(no text on this section)", "course material")
            + suffix
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def read_page_text(
        course_identifier: str | int,
        page_url_or_id: str,
        max_chars: int = 20000,
    ) -> str:
        """Read a course page as plain text, and cache it for study guides.

        Unlike get_page_content (which returns raw HTML), this returns readable
        prose with no markup.

        Args:
            course_identifier: Course code or Canvas ID
            page_url_or_id: Page URL slug or page ID
            max_chars: Maximum characters to return (default 20000)
        """
        course_id = await get_course_id(course_identifier)
        response = await make_canvas_request(
            "get", f"/courses/{course_id}/pages/{page_url_or_id}"
        )
        if isinstance(response, dict) and "error" in response:
            return f"Error fetching page: {response['error']}"

        title = response.get("title", "Untitled page")
        text = extract.html_to_text(response.get("body") or "")

        course_code = await get_course_code(course_id)
        directory = student_store.materials_dir(course_id, course_code)
        target = directory / f"page-{student_store.slug(title, 50)}.txt"
        student_store.write_text(target, text)

        if not text:
            return f"Page '{title}' has no text content. Cached at {target}"

        body = text[:max(1000, max_chars)]
        suffix = "" if len(text) <= len(body) else "\n...[truncated; cached file has the full text]"
        return (
            f"{fence_untrusted_inline(title, 'page title')}\n"
            f"Cached: {target} ({len(text)} chars)\n\n"
            + fence_untrusted(body, "page body")
            + suffix
        )


def _describe_cached(record: dict[str, Any], cache_path: str) -> str:
    """Render the index a caller sees after extraction."""
    sections = record.get("sections", [])
    extracted = extract.Extracted(
        filename=record.get("filename", "file"),
        kind=record.get("kind", "unknown"),
        sections=[extract.Section(item["label"], item["text"]) for item in sections],
    )
    lines = [
        f"Extracted: {fence_untrusted_inline(record.get('filename', 'file'), 'file name')}",
        f"Type: {record.get('kind')} | sections: {len(sections)} | words: {extracted.word_count}",
        f"Cache: {cache_path}",
    ]
    if record.get("warnings"):
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in record["warnings"])
    if not extracted.word_count:
        lines.append("\nNo readable text was extracted from this file.")
        return "\n".join(lines)

    toc_lines = extracted.toc(max_labels=_MAX_TOC_LINES).splitlines()
    lines.append("\nSections:")
    lines.extend(toc_lines)
    lines.append(
        "\nNext: call read_material_section(file_id=..., section='Slide 3') to read "
        "the parts you need for notes or a study guide."
    )
    return "\n".join(lines)
