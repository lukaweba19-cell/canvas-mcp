"""Reading course material: slides, PDFs, Word docs, pages.

Canvas returns file bytes. These tools turn them into text the model can study
from, cache the text next to the student's workspace, and return an index first
so a 60-slide deck never floods the conversation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core import extract, ocr, student_store
from ..core.cache import get_course_code, get_course_id
from ..core.client import (
    fetch_all_paginated_results,
    iter_canvas_file_bytes,
    make_canvas_request,
)
from ..core.untrusted_content import fence_untrusted, fence_untrusted_inline
from ..core.validation import coerce_canvas_id, validate_params

_MAX_SECTION_RETURN = 20_000
_MAX_TOC_LINES = 60


def _cache_file(
    course_identifier: str | int, course_code: str | None, file_id: str | int, filename: str
) -> Path:
    return student_store.material_cache_path(
        course_identifier, file_id, filename, course_code
    )


def _find_cache(
    course_identifier: str | int, course_code: str | None, file_id: str | int
) -> Path | None:
    return student_store.find_material_cache(course_identifier, file_id, course_code)


_STATUS_ORDER = {
    "failed": 0,
    "encoding": 0,
    "unsupported": 0,
    "image_only": 0,
    "empty": 0,
    "quiz": 0,
    "embedded": 0,
    "external": 0,
    "unknown_item": 0,
    "legacy_office": 0,
    "too_large": 0,
    "not_read": 1,
    "text": 2,
}


def file_statuses(
    course_identifier: str | int,
    course_code: str | None,
    file_list: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], extract.ContentStatus]]:
    """Each file paired with the status of its cached extraction."""
    pairs = [
        (
            entry,
            extract.status_for(
                _file_name(entry),
                student_store.load_material_record(
                    course_identifier, entry.get("id"), course_code
                ),
            ),
        )
        for entry in file_list
    ]
    pairs.sort(key=lambda pair: _STATUS_ORDER.get(pair[1].reason, 1))
    return pairs


def readiness_lines(
    course_identifier: str | int,
    course_code: str | None,
    file_list: list[dict[str, Any]],
    *,
    limit: int = 10,
    header: str | None = None,
) -> list[str]:
    """Readable/not-readable lines for a set of files, blockers first.

    Used by the content map, the assignment brief and the week plan, so all
    three describe material with the same words and the same reason.
    """
    pairs = file_statuses(course_identifier, course_code, file_list)
    readable = [status for _, status in pairs if status.readable]
    unread = sum(1 for _, status in pairs if status.reason == "not_read")
    reasons: dict[str, int] = {}
    for _, status in pairs:
        if not status.readable and status.reason != "not_read":
            reasons[status.reason] = reasons.get(status.reason, 0) + 1

    summary: list[str] = []
    if readable:
        summary.append(
            f"{len(readable)} readable ({sum(s.words for s in readable)} words)"
        )
    if unread:
        summary.append(f"{unread} unread")
    for reason in (
        "image_only",
        "empty",
        "unsupported",
        "encoding",
        "legacy_office",
        "too_large",
        "failed",
    ):
        if reasons.get(reason):
            summary.append(
                f"{reasons[reason]} NOT readable ({extract.REASON_LABELS[reason]})"
            )

    lines: list[str] = []
    if header is not None:
        lines.append(f"{header} " + (" | ".join(summary) or "no files"))
    elif summary:
        lines.append("- " + " | ".join(summary))
    for entry, status in pairs[:limit]:
        lines.append(
            f"  - file_id={entry.get('id')} "
            f"{fence_untrusted_inline(_file_name(entry), 'file name')} — "
            f"{status.label()}"
        )
    if len(pairs) > limit:
        lines.append(
            f"  - … and {len(pairs) - limit} more file(s); "
            "get_course_content_map lists the rest"
        )
    return lines


def _statuses_for(
    course_identifier: str | int,
    course_code: str | None,
    file_list: list[dict[str, Any]],
) -> dict[str, extract.ContentStatus]:
    """Content status for every course file that has been extracted already."""
    return {
        str(entry.get("id")): extract.status_for(
            _file_name(entry),
            student_store.load_material_record(
                course_identifier, entry.get("id"), course_code
            ),
        )
        for entry in file_list
    }


def _load_cache(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _download_course_file_bytes(download_url: str) -> tuple[bytes | None, str | None]:
    """Fetch a Canvas file's bytes using the caller's own credentials."""
    try:
        buffer = bytearray()
        # The token stays on Canvas's origin; the storage redirect is followed
        # without it (see iter_canvas_file_bytes).
        async for chunk in iter_canvas_file_bytes(download_url):
            buffer.extend(chunk)
        return bytes(buffer), None
    except Exception as exc:  # network, auth, storage redirect
        return None, f"{type(exc).__name__}: {exc}"



_WORD = re.compile(r"[a-z0-9]{4,}")


def _file_name(entry: dict[str, Any]) -> str:
    return str(entry.get("display_name") or entry.get("filename") or "unknown")


def _matching_transcript(
    title: str, transcripts: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Best-effort match of a video/LTI item to a caption or transcript file.

    Canvas does not link a Panopto item to its .vtt file, but instructors almost
    always name them alike ("Week 3 Lecture" / "week3-lecture.vtt"), so a word
    overlap is the only signal available. One shared word is enough; more wins.
    """
    wanted = set(_WORD.findall(title.lower()))
    if not wanted:
        return None
    best: tuple[int, dict[str, Any]] | None = None
    for entry in transcripts:
        shared = wanted & set(_WORD.findall(_file_name(entry).lower()))
        if shared and (best is None or len(shared) > best[0]):
            best = (len(shared), entry)
    return best[1] if best else None


def _item_guidance(
    item: dict[str, Any],
    files_by_id: dict[str, dict[str, Any]],
    transcripts: list[dict[str, Any]],
    statuses: dict[str, extract.ContentStatus],
) -> str:
    """How readable one module item is, and what to call for it.

    Nothing is labelled readable unless text can actually be produced: a File is
    checked against its extension, and an LTI/external item is never readable.
    """
    item_type = item.get("type", "Unknown")
    title = str(item.get("title") or "Untitled")

    if item_type == "SubHeader":
        return "heading only"
    if item_type == "Page":
        return "read_page_text"
    if item_type == "Assignment":
        return "get_assignment_details / start_assignment_work"
    if item_type == "Discussion":
        return "list_discussion_entries"
    if item_type == "Quiz":
        return extract.ContentStatus(
            False,
            "quiz",
            "quizzes cannot be read or taken here; open it in Canvas",
            kind="quiz",
        ).label()
    if item_type == "File":
        entry = files_by_id.get(str(item.get("content_id")))
        if entry is None:
            return (
                "unread — file is not in the course file list; call "
                "read_course_material with this content_id to check"
            )
        status = statuses.get(str(item.get("content_id"))) or extract.status_for(
            _file_name(entry)
        )
        if status.reason == "not_read":
            return f"{status.label()}"
        return status.label()
    if item_type in ("ExternalTool", "ExternalUrl"):
        transcript = _matching_transcript(title, transcripts)
        url = item.get("external_url") or item.get("url")
        if transcript is not None:
            return extract.ContentStatus(
                False,
                "embedded",
                "the item itself has no text, but a transcript exists: "
                f"read_course_material(file_id={transcript.get('id')}) "
                f"for {_file_name(transcript)}",
            ).label()
        if item_type == "ExternalTool" and transcripts:
            # No name match, but the course has transcripts: name them, so a
            # differently-titled recording is still reachable.
            listed = "; ".join(
                f"file_id={entry.get('id')} ({_file_name(entry)})"
                for entry in transcripts[:6]
            )
            return extract.ContentStatus(
                False,
                "embedded",
                "no transcript matched its title. Transcripts in this course: "
                f"{listed}. Pass one to read_course_material to read what was said."
                + (f" To watch it: {url}" if url else ""),
            ).label()
        if transcripts:
            return extract.ContentStatus(
                False,
                "external",
                "open "
                + (str(url) if url else "it in Canvas")
                + ". The course also has transcripts: "
                + "; ".join(
                    f"file_id={entry.get('id')} ({_file_name(entry)})"
                    for entry in transcripts[:4]
                ),
            ).label()
        return extract.ContentStatus(
            False,
            "embedded",
            "no transcript in course files; open "
            + (str(url) if url else "it in Canvas")
            + " and read the slides or notes posted alongside it",
        ).label()
    return extract.ContentStatus(
        False, "unknown_item", "open it in Canvas"
    ).label()


def register_material_tools(mcp: FastMCP) -> None:
    """Register course-material reading tools."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_content_map(course_identifier: str | int) -> str:
        """Map everything readable in a course, so you know what you can study.

        Lists each module item with its type and exactly what reads it, then the
        course files with whether their text can be extracted. Items that cannot
        produce text (quizzes, Panopto/Kaltura videos, external links) are marked
        NOT readable, with the concrete next action. Use this before building a
        study guide.

        Args:
            course_identifier: Course code or Canvas ID
        """
        course_id = await get_course_id(course_identifier)
        course_display = await get_course_code(course_id) or course_identifier

        files = await fetch_all_paginated_results(
            f"/courses/{course_id}/files", {"per_page": 100}
        )
        file_list = files if isinstance(files, list) else []
        files_by_id = {str(entry.get("id")): entry for entry in file_list}
        transcripts = [
            entry for entry in file_list if extract.is_transcript(_file_name(entry))
        ]
        statuses = _statuses_for(course_id, course_display, file_list)

        modules = await fetch_all_paginated_results(
            f"/courses/{course_id}/modules", {"per_page": 100, "include[]": ["items"]}
        )
        lines = [f"Content map for {course_display}:\n"]

        if isinstance(modules, dict) and "error" in modules:
            lines.append(f"Could not list modules: {modules['error']}")
        elif not modules:
            lines.append("No modules in this course.")
        else:
            for module in modules:
                lines.append(
                    f"\n## {fence_untrusted_inline(module.get('name', 'Untitled'), 'module name')}"
                )
                for item in module.get("items", []):
                    ids = []
                    if item.get("content_id"):
                        ids.append(f"content_id={item['content_id']}")
                    if item.get("page_url"):
                        ids.append(f"page_url={item['page_url']}")
                    lines.append(
                        f"- [{item.get('type', 'Unknown')}] "
                        f"{fence_untrusted_inline(item.get('title', 'Untitled'), 'module item title')}"
                        f" ({', '.join(ids) or 'no content id'}) -> "
                        f"{_item_guidance(item, files_by_id, transcripts, statuses)}"
                    )

        lines.append("\n## Course files")
        if isinstance(files, dict) and "error" in files:
            lines.append(f"Could not list files: {files['error']}")
        elif not file_list:
            lines.append("No files.")
        else:
            for entry in file_list:
                name = _file_name(entry)
                status = statuses[str(entry.get("id"))]
                lines.append(
                    f"- file_id={entry.get('id')} {fence_untrusted_inline(name, 'file name')}"
                    f" ({status.label()})"
                )

        if transcripts:
            lines.append("\n## Captions and transcripts")
            for entry in transcripts:
                status = statuses[str(entry.get("id"))]
                lines.append(
                    f"- file_id={entry.get('id')} "
                    f"{fence_untrusted_inline(_file_name(entry), 'transcript name')} — "
                    f"{status.label()}; read_course_material returns the spoken text "
                    "without timestamps"
                )

        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def read_course_material(
        course_identifier: str | int,
        file_id: str | int,
        refresh: bool = False,
        full_text: bool = False,
        max_chars: int = 12000,
    ) -> str:
        """Extract the text of a course file (PDF, slides, Word, Excel, notebook).

        Scanned PDFs and screenshot decks are OCR'd with the built-in engine.
        Legacy Word and Excel binaries (.doc/.xls) are read in process. Legacy
        PowerPoint (.ppt) is converted with a local LibreOffice; when that is not
        installed the call says so and names the install step instead of
        returning an empty result.

        Downloads the file once, extracts text, caches it in the student
        workspace, and by default returns a section index plus the cache path —
        call read_material_section for one section. Pass full_text=True to get
        the extracted text itself in the same call, stopping at the first section
        that does not fit max_chars and ending with a note that counts the
        sections not shown and names the next call. PDF decks exported from
        slides are read slide by slide (Slide 1, Slide 2, ...), and any section
        with no text is named rather than silently dropped. Slide speaker notes
        are included, since they usually say what matters.

        Args:
            course_identifier: Course code or Canvas ID
            file_id: Canvas file ID (see get_course_content_map or list_course_files)
            refresh: Re-download and re-extract even if a cache exists
            full_text: Return the extracted text, not just the index
            max_chars: Character budget for full_text (default 12000, minimum 1000)
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
                if full_text:
                    return render_extracted_text(cached, str(cache), max_chars)
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

        if full_text:
            return render_extracted_text(record, str(cache), max_chars)
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
            warnings=list(record.get("warnings") or []),
        )
        status = extracted.status
        found = extracted.section(section)
        if found is None:
            available = ", ".join(item["label"] for item in record.get("sections", [])[:15])
            return f"No section '{section}'. Available: {available}"

        if not found.text.strip():
            if status.reason == "image_only":
                hint = (
                    "This section has no text layer: it is an image or a scanned page. "
                )
                if ocr.is_available():
                    hint += (
                        "Built-in OCR found nothing readable here — re-run "
                        "read_course_material with refresh=true, or view the page in Canvas."
                    )
                else:
                    hint += f"It was not read. {ocr.unavailable_reason()}"
            elif status.reason == "empty":
                hint = "This file contains no text at all (it is empty)."
            elif status.reason == "encoding":
                hint = (
                    "This file has no readable text: its bytes are binary or not "
                    "UTF-8/UTF-16/Windows-1252 text."
                )
            elif status.reason == "failed":
                hint = f"This file could not be parsed: {status.detail}."
            else:
                hint = "This section has no text."
            return (
                f"{fence_untrusted_inline(record.get('filename', 'file'), 'file name')} "
                f"— {found.label}\n\n{hint}\nFile status: {status.label()}"
            )

        body = found.text[:_MAX_SECTION_RETURN]
        suffix = "" if len(found.text) <= _MAX_SECTION_RETURN else "\n...[section truncated]"
        return (
            f"{fence_untrusted_inline(record.get('filename', 'file'), 'file name')} "
            f"— {found.label}\n\n"
            + fence_untrusted(body, "course material")
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


def _status_note(status: extract.ContentStatus) -> str:
    """One paragraph explaining why a file has no text, and what to do."""
    if status.reason == "unsupported":
        return (
            "This file type cannot be read as text at all (audio, video, image or "
            "archive). Nothing was skipped silently: for a lecture video, look for a "
            "caption/transcript file (.vtt/.srt) — get_course_content_map lists any "
            "that exist and names the matching file_id."
        )
    if status.reason == "image_only":
        return (
            "No text exists in this file: every page/slide is an image. Built-in OCR "
            + ("ran and found no text in it" if ocr.is_available()
               else f"could not run ({ocr.unavailable_reason()})")
            + ", so the text has to come from a better scan or a transcript if one "
            "is posted."
        )
    if status.reason == "encoding":
        return (
            "This file has no readable text: its bytes are binary or not "
            "UTF-8/UTF-16/Windows-1252 text. Ask for a PDF or plain-text copy."
        )
    if status.reason == "legacy_office":
        return (
            "This legacy Office file needs one step before it can be read: "
            + status.detail + "."
        )
    if status.reason == "too_large":
        return (
            "This file is too large to extract in one call: " + status.detail +
            ". Download it with download_course_file and open it locally."
        )
    if status.reason == "failed":
        return f"Extraction failed: {status.detail}"
    return (
        "This file is empty: it contains no text at all (not image-only, just "
        "no content), so there is nothing to read."
    )


def render_extracted_text(
    record: dict[str, Any], cache_path: str, max_chars: int = 12000
) -> str:
    """The extracted text itself, so one call can read a file end to end.

    Non-reading files get the same honest status and reason as the index; a file
    bigger than ``max_chars`` is cut at the first section that does not fit (later
    smaller sections are not pulled in behind it) and the trailing note counts
    exactly how many sections were not shown, with the next call named. Sections
    that carry no text are dropped from the body and named in their own note.
    """
    sections = record.get("sections", [])
    extracted = extract.Extracted(
        filename=record.get("filename", "file"),
        kind=record.get("kind", "unknown"),
        sections=[extract.Section(item["label"], item["text"]) for item in sections],
        warnings=list(record.get("warnings") or []),
    )
    status = extracted.status
    lines = [
        f"Text: {fence_untrusted_inline(record.get('filename', 'file'), 'file name')}",
        f"Status: {status.label()}",
        f"Cache: {cache_path}",
    ]
    if not status.readable:
        lines.append("\n" + _status_note(status))
        return "\n".join(lines)

    budget = max(1000, max_chars)
    pending = [(s.label, s.text.strip()) for s in extracted.sections]
    pending = [(label, text) for label, text in pending if text]

    used = 0
    included: list[str] = []
    cut_at: int | None = None
    for index, (label, text) in enumerate(pending):
        block = f"## {label}\n{text}"
        if used + len(block) > budget:
            cut_at = index
            break
        included.append(block)
        used += len(block)

    lines.append("")
    lines.extend(included)
    if cut_at is not None:
        rest = pending[cut_at:]
        labels = ", ".join(label for label, _ in rest[:10])
        if len(rest) > 10:
            labels += f" (+{len(rest) - 10} more)"
        lines.append(
            f"\n...[stopped after {used} characters; {len(rest)} more section(s) "
            f"not shown, starting at '{rest[0][0]}': {labels}. "
            "Read them with read_material_section(file_id=..., section='...'), "
            "or raise max_chars.]"
        )

    # A section with no text is dropped from the body; say which ones, so a
    # screenshot slide inside a readable deck never disappears without a trace.
    untexted = [section.label for section in extracted.sections if not section.text.strip()]
    if untexted:
        listed = ", ".join(untexted[:10])
        if len(untexted) > 10:
            listed += f" (+{len(untexted) - 10} more)"
        lines.append(
            f"\n...[also not shown: {len(untexted)} section(s) with no text "
            f"(image-only or blank): {listed}. read_material_section reports "
            "what each one contains.]"
        )
    return "\n".join(lines)


def _describe_cached(record: dict[str, Any], cache_path: str) -> str:
    """Render the index a caller sees after extraction."""
    sections = record.get("sections", [])
    extracted = extract.Extracted(
        filename=record.get("filename", "file"),
        kind=record.get("kind", "unknown"),
        sections=[extract.Section(item["label"], item["text"]) for item in sections],
        warnings=list(record.get("warnings") or []),
    )
    status = extracted.status
    lines = [
        f"Extracted: {fence_untrusted_inline(record.get('filename', 'file'), 'file name')}",
        f"Type: {record.get('kind')} | sections: {len(sections)} | words: {extracted.word_count}",
        f"Status: {status.label()}",
        f"Cache: {cache_path}",
    ]
    if record.get("warnings"):
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in record["warnings"])
    unreadable = extracted.unreadable
    if unreadable:
        lines.append(
            f"\nSections with no text ({len(unreadable)}): " + ", ".join(unreadable[:12])
        )
        if not ocr.is_available():
            lines.append("  " + ocr.unavailable_reason())

    if not extracted.word_count:
        lines.append("\n" + _status_note(status))
        return "\n".join(lines)

    toc_lines = extracted.toc(max_labels=_MAX_TOC_LINES).splitlines()
    lines.append("\nSections:")
    lines.extend(toc_lines)
    # Name a label this file actually has: "Slide 3" is wrong for a p.N document.
    example = next(
        (section.label for section in extracted.sections if section.text.strip()),
        extracted.sections[0].label if extracted.sections else "1",
    )
    lines.append(
        f"\nNext: call read_material_section(file_id=..., section='{example}') to "
        "read the parts you need for notes or a study guide."
    )
    return "\n".join(lines)
