"""The student's local workspace: extracted material, study guides, notes,
drafts, a work journal and a voice profile.

Everything here is plain files under one directory (``CANVAS_STUDENT_DIR``,
default ``~/.canvas-mcp/student``) so the student can open any of it in an
editor. Nothing in this module talks to Canvas: it only reads and writes disk.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = Path.home() / ".canvas-mcp" / "student"

_UNSAFE = re.compile(r"[^a-z0-9]+")
_FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def data_dir() -> Path:
    """Root of the local workspace, created on demand."""
    from .config import get_config

    configured = (get_config().student_dir or "").strip()
    root = Path(configured).expanduser() if configured else DEFAULT_DATA_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def slug(value: str, max_length: int = 60) -> str:
    """Filesystem-safe slug."""
    cleaned = _UNSAFE.sub("-", str(value).lower()).strip("-")
    return (cleaned or "item")[:max_length].rstrip("-")


def _now() -> datetime:
    return datetime.now().astimezone()


def _stamp() -> str:
    return _now().strftime("%Y%m%d-%H%M%S")


def course_slug(course_identifier: str | int, course_code: str | None = None) -> str:
    """Stable per-course directory name."""
    return slug(course_code or str(course_identifier))


def _course_candidates(
    course_identifier: str | int, course_code: str | None = None
) -> list[str]:
    """Every directory name this course could already live under.

    Tools sometimes know the course code and sometimes only the id, so the same
    course can be named two ways. Both are checked before a new directory is
    created, otherwise one call would write the brief under ``cs225`` and the
    next would put the draft under ``225``.
    """
    names: list[str] = []
    for value in (course_code, course_identifier):
        if value is None:
            continue
        name = slug(value)
        if name and name not in names:
            names.append(name)
    return names


def course_dir(course_identifier: str | int, course_code: str | None = None) -> Path:
    """The course's workspace directory, reusing an existing one under either name."""
    root = data_dir() / "courses"
    names = _course_candidates(course_identifier, course_code) or ["course"]
    for name in names:
        path = root / name
        if path.is_dir():
            return path
    path = root / names[0]
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_assignment_dir(
    course_identifier: str | int,
    assignment_id: str | int,
    course_code: str | None = None,
) -> Path | None:
    """Locate an existing assignment folder by its id prefix, without creating one.

    Callers that only know the assignment id (submitting an approved draft,
    checking style) must not invent a second folder next to the real one.
    """
    prefix = slug(str(assignment_id), 12)
    root = data_dir() / "courses"
    for name in _course_candidates(course_identifier, course_code):
        base = root / name
        if not base.is_dir():
            continue
        found = sorted(path for path in base.glob(f"{prefix}-*") if path.is_dir())
        if found:
            return found[0]

    # The folder may have been created under the course's other name. Accept a
    # match from elsewhere only when it is unambiguous.
    elsewhere = sorted(
        path for path in root.glob(f"*/{prefix}-*") if path.is_dir()
    )
    if len(elsewhere) == 1:
        return elsewhere[0]
    return None


def assignment_dir(
    course_identifier: str | int,
    assignment_id: str | int,
    title: str | None = None,
    course_code: str | None = None,
) -> Path:
    """Per-assignment workspace directory, reusing an existing one if present."""
    existing = find_assignment_dir(course_identifier, assignment_id, course_code)
    if existing is not None:
        (existing / "drafts").mkdir(exist_ok=True)
        return existing
    label = f"{slug(str(assignment_id), 12)}-{slug(title or 'assignment', 40)}"
    path = course_dir(course_identifier, course_code) / label
    path.mkdir(parents=True, exist_ok=True)
    (path / "drafts").mkdir(exist_ok=True)
    return path


def materials_dir(course_identifier: str | int, course_code: str | None = None) -> Path:
    path = course_dir(course_identifier, course_code) / "materials"
    path.mkdir(parents=True, exist_ok=True)
    return path


def material_cache_path(
    course_identifier: str | int,
    file_id: str | int,
    filename: str,
    course_code: str | None = None,
) -> Path:
    """Where one extracted file's cached text lives."""
    return materials_dir(course_identifier, course_code) / f"{file_id}-{slug(filename, 50)}.json"


def find_material_cache(
    course_identifier: str | int,
    file_id: str | int,
    course_code: str | None = None,
) -> Path | None:
    directory = materials_dir(course_identifier, course_code)
    matches = sorted(directory.glob(f"{file_id}-*.json"))
    return matches[0] if matches else None


def load_material_record(
    course_identifier: str | int,
    file_id: str | int,
    course_code: str | None = None,
) -> dict[str, Any] | None:
    """The cached extraction for a file, or None when it was never read."""
    path = find_material_cache(course_identifier, file_id, course_code)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def study_dir(course_identifier: str | int, course_code: str | None = None) -> Path:
    path = course_dir(course_identifier, course_code) / "study"
    path.mkdir(parents=True, exist_ok=True)
    return path


def notes_dir() -> Path:
    path = data_dir() / "notes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def journal_path() -> Path:
    path = data_dir() / "journal"
    path.mkdir(parents=True, exist_ok=True)
    return path / "entries.jsonl"


def voice_path() -> Path:
    return data_dir() / "voice.md"


def write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


def _front_matter(meta: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if value in (None, "", []):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def _parse_note(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta: dict[str, Any] = {"path": str(path), "id": path.stem}
    match = _FRONT_MATTER.match(raw)
    body = raw
    if match:
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
        body = raw[match.end():]
    meta["body"] = body.strip()
    meta["title"] = meta.get("title") or path.stem
    return meta


def save_note(
    title: str,
    content: str,
    tags: str = "",
    course_identifier: str | int | None = None,
    kind: str = "note",
) -> dict[str, Any]:
    """Write a note as markdown with front matter. Returns its id and path."""
    note_id = f"{_stamp()}-{slug(title, 50)}"
    meta = {
        "id": note_id,
        "title": title.strip() or "Untitled note",
        "created": _now().isoformat(timespec="seconds"),
        "course": course_identifier,
        "kind": kind,
        "tags": tags,
    }
    path = write_text(notes_dir() / f"{note_id}.md", f"{_front_matter(meta)}\n\n{content.strip()}\n")
    return {"id": note_id, "path": str(path), "title": meta["title"], "kind": kind}


def list_notes(
    course_identifier: str | int | None = None, kind: str | None = None
) -> list[dict[str, Any]]:
    notes = []
    for path in sorted(notes_dir().glob("*.md"), reverse=True):
        meta = _parse_note(path)
        if course_identifier is not None and str(meta.get("course", "")) != str(course_identifier):
            continue
        if kind and meta.get("kind") != kind:
            continue
        notes.append({key: meta.get(key, "") for key in ("id", "title", "created", "course", "kind", "tags", "path")})
    return notes


def find_note(note_id: str) -> Path | None:
    direct = notes_dir() / f"{note_id}.md"
    if direct.is_file():
        return direct
    for path in notes_dir().glob("*.md"):
        if note_id in path.stem:
            return path
    return None


def read_note(note_id: str) -> dict[str, Any] | None:
    path = find_note(note_id)
    return _parse_note(path) if path else None


def search_notes(
    query: str, course_identifier: str | int | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """Case-insensitive substring search over note titles, tags and bodies."""
    needle = query.strip().lower()
    if not needle:
        return []
    hits: list[dict[str, Any]] = []
    for path in sorted(notes_dir().glob("*.md"), reverse=True):
        meta = _parse_note(path)
        haystack = " ".join(
            str(meta.get(key, "")) for key in ("title", "tags", "body")
        ).lower()
        if needle not in haystack:
            continue
        if course_identifier is not None and str(meta.get("course", "")) != str(course_identifier):
            continue
        body = str(meta.get("body", ""))
        position = body.lower().find(needle)
        snippet = body[max(0, position - 120): position + 200].strip() if position >= 0 else body[:200]
        hits.append(
            {
                "id": meta.get("id"),
                "title": meta.get("title"),
                "created": meta.get("created"),
                "snippet": re.sub(r"\s+", " ", snippet),
            }
        )
        if len(hits) >= limit:
            break
    return hits


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


def save_draft(
    course_identifier: str | int,
    assignment_id: str | int,
    content: str,
    title: str | None = None,
    kind: str = "draft",
    notes: str = "",
    course_code: str | None = None,
) -> dict[str, Any]:
    """Version a draft. Every save keeps the previous file, nothing is lost."""
    workspace = assignment_dir(course_identifier, assignment_id, title, course_code)
    drafts = sorted((workspace / "drafts").glob("v*.md"))
    version = len(drafts) + 1
    header = _front_matter(
        {
            "assignment_id": assignment_id,
            "course": course_identifier,
            "kind": kind,
            "version": version,
            "saved": _now().isoformat(timespec="seconds"),
            "notes": notes,
        }
    )
    path = write_text(
        workspace / "drafts" / f"v{version}.md", f"{header}\n\n{content.strip()}\n"
    )
    write_text(workspace / "current.md", content.strip() + "\n")
    return {"version": version, "path": str(path), "current": str(workspace / "current.md"), "kind": kind}


def list_drafts(
    course_identifier: str | int | None = None,
) -> list[dict[str, Any]]:
    root = data_dir() / "courses"
    found: list[dict[str, Any]] = []
    for current in root.glob("*/*/current.md"):
        parts = current.parts
        course = parts[-3]
        assignment = parts[-2]
        if course_identifier is not None and slug(str(course_identifier)) not in course:
            continue
        versions = sorted(current.parent.glob("drafts/v*.md"))
        found.append(
            {
                "course": course,
                "assignment": assignment,
                "versions": len(versions),
                "updated": datetime.fromtimestamp(
                    current.stat().st_mtime
                ).astimezone().isoformat(timespec="seconds"),
                "path": str(current),
                "words": len(current.read_text(encoding="utf-8", errors="replace").split()),
            }
        )
    return sorted(found, key=lambda item: item["updated"], reverse=True)


def read_draft(
    course_identifier: str | int,
    assignment_id: str | int,
    title: str | None = None,
    version: int | None = None,
    course_code: str | None = None,
) -> dict[str, Any] | None:
    """Read a saved draft. ``version=None`` reads the latest/current copy."""
    if title is None:
        workspace = find_assignment_dir(course_identifier, assignment_id, course_code)
        if workspace is None:
            return None
    else:
        workspace = assignment_dir(course_identifier, assignment_id, title, course_code)
    if version is None:
        target = workspace / "current.md"
    else:
        target = workspace / "drafts" / f"v{version}.md"
    if not target.is_file():
        return None
    return {"path": str(target), "content": target.read_text(encoding="utf-8", errors="replace")}


# ---------------------------------------------------------------------------
# Work journal
# ---------------------------------------------------------------------------


def append_journal(entry: dict[str, Any]) -> dict[str, Any]:
    """Append one work-log entry. JSONL so the handoff can be reconstructed."""
    record = {"at": _now().isoformat(timespec="seconds"), **entry}
    with journal_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_journal(limit: int = 50) -> list[dict[str, Any]]:
    path = journal_path()
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    records = []
    for line in lines[-limit:]:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


# ---------------------------------------------------------------------------
# Voice profile
# ---------------------------------------------------------------------------


def save_voice(markdown: str) -> Path:
    return write_text(voice_path(), markdown.strip() + "\n")


def read_voice() -> str:
    path = voice_path()
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
