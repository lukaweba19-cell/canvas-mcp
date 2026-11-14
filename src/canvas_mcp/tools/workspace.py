"""The student's workspace: assignment prep, drafts, notes, journal, voice.

These tools are how the agent works while the student is away. Everything they
write lands in local markdown under CANVAS_STUDENT_DIR, and get_my_handoff
summarises what was done and what still needs the student's decision.
"""

from __future__ import annotations

from pathlib import Path

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core import extract, student_store
from ..core import rubric as rubric_core
from ..core import study as study_core
from ..core.cache import get_course_code, get_course_id
from ..core.client import make_canvas_request
from ..core.dates import format_date
from ..core.validation import coerce_canvas_id, validate_params


def register_workspace_tools(mcp: FastMCP) -> None:
    """Register workspace tools."""

    @mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
    @validate_params
    async def start_assignment_work(
        course_identifier: str | int,
        assignment_id: str | int,
    ) -> str:
        """Open a workspace for one assignment and write its brief to disk.

        Pulls the prompt, rubric, due dates, submission rules, your current
        submission state and any feedback, then writes brief.md and rubric.md
        into the assignment's workspace folder. Do this before drafting.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Canvas assignment ID
        """
        validated = coerce_canvas_id(assignment_id)
        if validated is None:
            return "Error: assignment_id must be a numeric Canvas assignment ID."
        assignment_id = validated

        course_id = await get_course_id(course_identifier)
        course_code = await get_course_code(course_id) or str(course_identifier)

        assignment = await make_canvas_request(
            "get",
            f"/courses/{course_id}/assignments/{assignment_id}",
            params={"include[]": ["rubric"]},
        )
        if isinstance(assignment, dict) and "error" in assignment:
            return f"Error fetching assignment: {assignment['error']}"

        submission = await make_canvas_request(
            "get",
            f"/courses/{course_id}/assignments/{assignment_id}/submissions/self",
            params={"include[]": ["submission_comments", "rubric_assessment"]},
        )
        if not isinstance(submission, dict) or "error" in submission:
            submission = {}

        title = assignment.get("name", f"Assignment {assignment_id}")
        workspace = student_store.assignment_dir(course_id, assignment_id, title, course_code)
        description = extract.html_to_text(assignment.get("description") or "")

        brief = [
            f"# {title}",
            "",
            f"- Course: {course_code}",
            f"- Assignment ID: {assignment_id}",
            f"- Due: {format_date(assignment.get('due_at'))}",
            f"- Unlocks: {format_date(assignment.get('unlock_at'))}",
            f"- Locks: {format_date(assignment.get('lock_at'))}",
            f"- Points: {assignment.get('points_possible', 'n/a')}",
            f"- Submission types: {', '.join(assignment.get('submission_types') or []) or 'none'}",
            f"- Allowed extensions: {', '.join(assignment.get('allowed_extensions') or []) or 'any'}",
            f"- Attempts: {submission.get('attempt', 0)} used, "
            f"{assignment.get('allowed_attempts', 'unlimited')} allowed",
            f"- Submitted: {format_date(submission.get('submitted_at'))}"
            if submission.get("submitted_at") else "- Submitted: not yet",
            f"- Grade: {submission.get('grade', 'not graded')}",
            "",
            "## Prompt",
            description or "(no description)",
        ]
        if assignment.get("group_category_id"):
            brief += ["", "⚠️ Group assignment — agent submission is refused by design."]
        student_store.write_text(workspace / "brief.md", "\n".join(brief))

        parsed_rubric = rubric_core.from_assignment(assignment)
        if parsed_rubric.criteria:
            rubric_path = rubric_core.save(parsed_rubric, course_id, assignment_id, title)
            rubric_summary = (
                f"Rubric written: {rubric_path} ({parsed_rubric.source}, "
                f"{len(parsed_rubric.criteria)} criteria, "
                f"{parsed_rubric.total_points:g} pts)"
            )
        else:
            rubric_summary = (
                "No rubric: Canvas has none attached and none could be parsed from the "
                "prompt — ask the instructor what is being marked, and write to the "
                "assignment instructions."
            )

        return "\n".join(
            [
                f"Workspace: {workspace}",
                f"Brief written: {workspace / 'brief.md'}",
                rubric_summary,
                "",
                "Files in the prompt (read with read_course_material if the prompt "
                "references them):",
                description[:1500] or "(no prompt text)",
                "",
                "Next: save_assignment_draft(...) to write your draft, "
                "save_note(...) for anything worth remembering, then "
                "submit_assignment(..., from_draft=true) once the student approves.",
            ]
        )

    @mcp.tool(annotations=ToolAnnotations(idempotent_hint=False))
    @validate_params
    async def save_assignment_draft(
        course_identifier: str | int,
        assignment_id: str | int,
        content: str,
        kind: str = "draft",
        notes: str = "",
        title: str | None = None,
    ) -> str:
        """Save a versioned draft for an assignment (never overwrites history).

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Canvas assignment ID
            content: Draft text in markdown
            kind: outline, draft, or final
            notes: Short note about this version, for the student's handoff
            title: Assignment title (helps name the folder)
        """
        if not content.strip():
            return "Error: content cannot be empty."

        result = student_store.save_draft(
            course_identifier, assignment_id, content, title=title, kind=kind, notes=notes
        )
        coverage = ""
        parsed_rubric = rubric_core.load(course_identifier, assignment_id)
        if parsed_rubric is not None and parsed_rubric.criteria:
            verdicts = rubric_core.check(parsed_rubric, content)
            addressed = sum(1 for verdict in verdicts if verdict.verdict == "addressed")
            missing = [
                verdict.criterion.plain()
                for verdict in verdicts
                if verdict.verdict == "not found"
            ]
            coverage = (
                f"\nRubric ({parsed_rubric.source}): {addressed}/{len(verdicts)} "
                "criteria referenced"
                + (f"; not found: {', '.join(missing[:5])}" if missing else "")
                + "\nFull verdict: check_work_against_rubric"
            )
        return (
            f"Draft v{result['version']} saved ({kind}): {result['path']}\n"
            f"Current copy: {result['current']}\n"
            f"Words: {study_core.word_count(content)}"
            f"{coverage}"
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def read_assignment_draft(
        course_identifier: str | int,
        assignment_id: str | int,
        version: int = 0,
    ) -> str:
        """Read a saved draft. version=0 reads the latest copy.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Canvas assignment ID
            version: 0 for the latest, or a specific version number
        """
        draft = student_store.read_draft(
            course_identifier, assignment_id, version=None if version == 0 else version
        )
        if draft is None:
            return "No draft saved for that assignment yet."
        return f"{draft['path']}\n\n{draft['content']}"

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_assignment_drafts(course_identifier: str | int | None = None) -> str:
        """List assignments that have saved drafts.

        Args:
            course_identifier: Course code or Canvas ID (omit for all)
        """
        drafts = student_store.list_drafts(course_identifier)
        if not drafts:
            return "No drafts yet."
        lines = ["Saved drafts (most recent first):\n"]
        for draft in drafts:
            lines.append(
                f"- {draft['assignment']} [{draft['course']}] — {draft['versions']} versions, "
                f"{draft['words']} words, updated {draft['updated']}"
            )
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
    @validate_params
    async def save_note(
        title: str,
        content: str,
        tags: str = "",
        course_identifier: str | int | None = None,
        kind: str = "note",
    ) -> str:
        """Save a markdown note in the student workspace.

        Use kind="question" for things that need the student's answer, and
        kind="decision" for choices you made that they should know about.

        Args:
            title: Short title
            content: Note body in markdown
            tags: Comma-separated tags
            course_identifier: Course this relates to (optional)
            kind: note, question, decision, or source-summary
        """
        saved = student_store.save_note(title, content, tags, course_identifier, kind)
        return f"Note saved [{saved['kind']}]: {saved['path']}"

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def search_notes(
        query: str,
        course_identifier: str | int | None = None,
        limit: int = 10,
    ) -> str:
        """Search saved notes by text.

        Args:
            query: Text to look for
            course_identifier: Restrict to one course (optional)
            limit: Maximum matches (default 10)
        """
        hits = student_store.search_notes(query, course_identifier, limit)
        if not hits:
            return f"No notes match '{query}'."
        lines = [f"Notes matching '{query}':\n"]
        for hit in hits:
            lines.append(f"- {hit['title']} ({hit['id']}, {hit['created']})\n  {hit['snippet']}")
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_notes(
        course_identifier: str | int | None = None,
        kind: str | None = None,
    ) -> str:
        """List saved notes.

        Args:
            course_identifier: Restrict to one course (optional)
            kind: Restrict to note, question, decision or source-summary
        """
        notes = student_store.list_notes(course_identifier, kind)
        if not notes:
            return "No notes yet."
        return "\n".join(
            f"- [{note['kind']}] {note['title']} ({note['id']})"
            f"{' — course ' + str(note['course']) if note['course'] else ''}"
            for note in notes
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def read_note(note_id: str) -> str:
        """Read one note by id (or a unique part of it).

        Args:
            note_id: Note id from list_notes or search_notes
        """
        note = student_store.read_note(note_id)
        if note is None:
            return f"No note matching '{note_id}'."
        return (
            f"{note.get('path')}\nTitle: {note.get('title')}\n"
            f"Tags: {note.get('tags', '')}\n\n{note.get('body', '')}"
        )

    @mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
    @validate_params
    async def add_work_log_entry(
        summary: str,
        course_identifier: str | int | None = None,
        assignment_id: str | int | None = None,
        needs_you: str = "",
        next_step: str = "",
    ) -> str:
        """Record what you did, so the student can pick up later.

        Write one entry per work session. Be specific about what is done and
        what is NOT done.

        Args:
            summary: What you did
            course_identifier: Course it relates to
            assignment_id: Assignment it relates to
            needs_you: What the student must decide or provide ("" if nothing)
            next_step: The next concrete action
        """
        record = student_store.append_journal(
            {
                "summary": summary,
                "course": str(course_identifier) if course_identifier else None,
                "assignment": str(assignment_id) if assignment_id else None,
                "needs_you": needs_you,
                "next_step": next_step,
            }
        )
        return f"Logged at {record['at']}."

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_handoff(days: int = 7) -> str:
        """Summarise work done recently, what is blocked, and what needs you.

        Use this when the student comes back: it reports sessions, open
        questions, drafts waiting for review, and next steps.

        Args:
            days: How far back to look (default 7)
        """
        from datetime import datetime, timedelta

        cutoff = datetime.now().astimezone() - timedelta(days=max(1, days))
        entries = [
            entry
            for entry in student_store.read_journal(limit=200)
            if datetime.fromisoformat(entry["at"]) >= cutoff
        ]

        lines = [f"Work log (last {days} days): {len(entries)} session(s)\n"]
        open_items = []
        for entry in entries:
            scope = " / ".join(
                part for part in (entry.get("course"), entry.get("assignment")) if part
            )
            lines.append(f"- {entry['at']} {scope}: {entry.get('summary', '')}")
            if entry.get("next_step"):
                lines.append(f"    next: {entry['next_step']}")
            if entry.get("needs_you"):
                open_items.append(entry)
                lines.append(f"    NEEDS YOU: {entry['needs_you']}")

        questions = student_store.list_notes(kind="question")
        if questions:
            lines.append("\nOpen questions in notes:")
            lines.extend(f"- {note['title']}" for note in questions[:10])

        drafts = student_store.list_drafts()
        if drafts:
            lines.append("\nDrafts on disk:")
            lines.extend(
                f"- {draft['assignment']} [{draft['course']}] — {draft['versions']} versions, "
                f"{draft['words']} words"
                for draft in drafts[:10]
            )

        if open_items:
            lines.append(f"\n{len(open_items)} item(s) are waiting on you before I continue.")
        else:
            lines.append("\nNothing is blocked on you right now.")
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
    @validate_params
    async def save_voice_sample(
        text: str = "",
        course_identifier: str | int | None = None,
        assignment_ids: str = "",
    ) -> str:
        """Build a writing-voice profile from the student's own work.

        Either pass their writing directly, or comma-separated assignment IDs
        whose submissions the student already wrote. Drafts then match their
        real voice instead of generic assistant prose.

        Args:
            text: Sample of the student's own writing
            course_identifier: Course to read past submissions from
            assignment_ids: Comma-separated assignment IDs to read submissions from
        """
        samples = [text] if text.strip() else []
        if assignment_ids and course_identifier is not None:
            course_id = await get_course_id(course_identifier)
            for raw_id in assignment_ids.split(","):
                validated = coerce_canvas_id(raw_id)
                if validated is None:
                    continue
                submission = await make_canvas_request(
                    "get",
                    f"/courses/{course_id}/assignments/{validated}/submissions/self",
                )
                if isinstance(submission, dict) and "error" in submission:
                    return (
                        f"Error reading submission {validated}: "
                        f"{submission['error']}"
                    )
                if isinstance(submission, dict) and submission.get("body"):
                    samples.append(extract.html_to_text(submission["body"]))

        if not samples:
            return (
                "No writing to learn from. Pass `text` with something the student wrote, "
                "or course_identifier + assignment_ids for submissions they already made."
            )

        combined = "\n\n".join(samples)
        stats = study_core.analyze_style(combined)
        sentences = study_core.sentence_split(combined)[:6]
        profile = [
            "# Writing voice",
            "",
            f"Built from {len(samples)} sample(s), {stats['words']} words.",
            "",
            "## Measured habits",
            f"- Average sentence: {stats['avg_sentence_words']} words",
            f"- Sentence length spread: {stats['sentence_length_spread']}",
            f"- Paragraphs: {stats['paragraphs']}",
            "",
            "## How they open",
            *(f"- {sentence}" for sentence in sentences[:3]),
            "",
            "## Rules for drafts",
            "- Match the average sentence length and the spread above; do not smooth the rhythm out.",
            "- Reuse their vocabulary level rather than upgrading it to sound academic.",
            "- Keep their punctuation habits; do not add em dashes they do not use.",
            "- Do not add stock connectives (moreover, furthermore, in conclusion).",
            "- Prefer concrete examples over general claims, as they do.",
        ]
        path = student_store.save_voice("\n".join(profile))
        return f"Voice profile saved: {path}\n\n" + "\n".join(profile[:20])

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def check_draft_style(
        text: str = "",
        path: str = "",
        course_identifier: str | int | None = None,
        assignment_id: str | int | None = None,
    ) -> str:
        """Check a draft for stock phrasing and mechanical rhythm.

        Reports specific lines to change, not a score. Pass text, a file path,
        or a course+assignment to check the saved draft.

        Args:
            text: Draft text
            path: Path to a markdown file
            course_identifier: Course of the saved draft
            assignment_id: Assignment of the saved draft
        """
        content = text
        if not content and path:
            target = Path(path).expanduser()
            if not target.is_file():
                return f"No file at {path}."
            content = target.read_text(encoding="utf-8", errors="replace")
        if not content and course_identifier is not None and assignment_id is not None:
            draft = student_store.read_draft(course_identifier, assignment_id)
            if draft is None:
                return "No saved draft for that assignment."
            content = draft["content"]
        if not content.strip():
            return "Nothing to check. Pass text, path, or a course+assignment."

        voice = student_store.read_voice()
        stats = study_core.analyze_style(content)
        lines = [
            f"Style check: {stats['words']} words, {stats['sentences']} sentences, "
            f"{stats['avg_sentence_words']} words/sentence (spread {stats['sentence_length_spread']})",
        ]
        lines.extend(f"- {finding}" for finding in stats["findings"])
        if voice:
            lines.append("\nA voice profile exists — compare against voice.md before submitting.")
        else:
            lines.append("\nNo voice profile yet. save_voice_sample(...) makes drafts sound like you.")
        return "\n".join(lines)
