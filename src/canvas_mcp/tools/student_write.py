"""Tier 1 student write tools (#170).

These are the first tools that let an agent act *on* Canvas on a student's
behalf rather than only read. Four properties are load-bearing:

1. **No identity override on the wire.** The submit endpoint
   (``POST /courses/:id/assignments/:id/submissions``) is not structurally
   self-scoped: Canvas accepts ``submission[user_id]`` there when the session
   carries grading permission, and a real person can hold mixed student and TA
   enrollments. So rather than trusting the tool profile, every outbound write
   body is checked against an identity-override denylist immediately before it
   is sent (``assert_no_identity_override``).
2. **Operator ceiling.** A tool absent from ``STUDENT_WRITE_TOOLS`` is never
   registered, so it never enters the MCP tool list. The default is empty.
3. **Instructor agency.** Within that ceiling, a per-course policy can further
   restrict writes, and it is re-checked immediately before the write itself,
   not merely during the preview. See ``core/course_policy.py``.
4. **Confirmation bound to content.** ``submit_assignment`` will not submit on
   a bare boolean. The preview issues a short-lived, single-use token bound to
   the target, the payload hash and the observed attempt number, so an agent
   cannot submit without first surfacing a preview, and cannot submit something
   other than what was previewed.

Group assignments are refused in Tier 1: a submission to a group assignment
becomes the whole group's submission, affecting students who never consented,
and those shared-attempt semantics deserve their own decision.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import tempfile
import time
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core import student_store
from ..core.cache import get_course_id
from ..core.client import make_canvas_request, upload_file_to_storage
from ..core.config import get_config
from ..core.course_policy import (
    assert_no_identity_override,
    check_student_write_allowed,
)
from ..core.dates import format_date
from ..core.file_validation import (
    DEFAULT_MAX_FILE_SIZE_BYTES,
    detect_mime_type,
    sanitize_filename,
)
from ..core.untrusted_content import (
    FENCE_LEAK_ERROR,
    contains_fence_markers,
    fence_untrusted,
    fence_untrusted_inline,
)
from ..core.validation import coerce_canvas_id, validate_params
from ..core.write_confirmation import ConfirmationGuard, unconfirmed_write_warning

_SUPPORTED_TYPES = ("online_text_entry", "online_url", "online_upload")

_UNSUBMITTABLE_TYPES = {
    "media_recording": (
        "Canvas only accepts these from its own in-browser recorder, which the "
        "API cannot create a media id for"
    ),
    "student_annotation": (
        "Canvas only accepts these from its own PDF annotation viewer, which the "
        "API cannot produce"
    ),
}


def _rejected_submission_type(submission_type: str) -> str:
    """Explain an unsupported submission type instead of only listing valid ones."""
    supported = ", ".join(_SUPPORTED_TYPES)
    reason = _UNSUBMITTABLE_TYPES.get(submission_type)
    if reason:
        return (
            f"Error: '{submission_type}' cannot be submitted through Canvas's API: "
            f"{reason}. Submit it in Canvas itself, or — if the assignment also "
            f"accepts one of these — resubmit as {supported}."
        )
    return f"Error: submission_type must be one of {supported} (got '{submission_type}')"

_INVALID_ASSIGNMENT_ID = (
    "Error: assignment_id must be a numeric Canvas assignment ID. "
    "Use list_assignments to find it."
)

_MAX_UPLOAD_FILES = 20
_MAX_TOTAL_UPLOAD_BYTES = DEFAULT_MAX_FILE_SIZE_BYTES


def _too_large_message() -> str:
    limit_mb = _MAX_TOTAL_UPLOAD_BYTES // (1024 * 1024)
    return (
        f"❌ Those files total more than the {limit_mb} MB allowed for one "
        "submission. Submit fewer or smaller files, or upload them in Canvas."
    )

_CONFIRM_TTL_SECONDS = 300

_SUBMISSION_GUARD = ConfirmationGuard(
    ttl_seconds=_CONFIRM_TTL_SECONDS, nothing_done="Nothing was submitted."
)
_redeemed: dict[str, float] = {}
_active: set[str] = set()


def reset_pending_confirmations() -> None:
    """Discard confirmation state (used by tests with no active submissions)."""
    _SUBMISSION_GUARD.reset()
    _redeemed.clear()
    _active.clear()


def _purge_redeemed() -> None:
    now = time.monotonic()
    for fingerprint in [
        f for f, expiry in _redeemed.items() if expiry < now and f not in _active
    ]:
        _redeemed.pop(fingerprint, None)


def _reserve_confirmation(fingerprint: str, token: str) -> bool:
    """Claim both nonce and fingerprint, without yielding to the event loop.

    Caller must check the content binding first. A live fingerprint owner is
    retained even beyond the preview TTL, so a fresh token cannot overlap it.
    """
    _purge_redeemed()
    if fingerprint in _redeemed or not _SUBMISSION_GUARD.reserve(token):
        return False
    _redeemed[fingerprint] = time.monotonic() + _CONFIRM_TTL_SECONDS
    _active.add(fingerprint)
    return True


def _release_confirmation(fingerprint: str, token: str) -> None:
    """Owner-only: no submission was attempted. A nonce burn remains terminal."""
    _active.discard(fingerprint)
    _redeemed.pop(fingerprint, None)
    _SUBMISSION_GUARD.release(token)


def _finish_confirmation(fingerprint: str) -> None:
    """Retire the active owner; retain uncertain/completed attempts for one TTL."""
    _active.discard(fingerprint)
    if fingerprint in _redeemed:
        _redeemed[fingerprint] = time.monotonic() + _CONFIRM_TTL_SECONDS


def _issue_token(fingerprint: str, now: float | None = None) -> str:
    return _SUBMISSION_GUARD.issue(fingerprint, now=now)


def _check_token(token: str, fingerprint: str) -> str | None:
    error = _SUBMISSION_GUARD.check(token, fingerprint)
    if error:
        return error
    _purge_redeemed()
    if fingerprint in _redeemed:
        return (
            "❌ That confirmation was already used. Nothing was submitted. "
            "Run the preview again."
        )
    return None


def _caller_identity() -> str:
    return _SUBMISSION_GUARD.caller_identity()


class _PreparedFile:
    """A file staged for upload, with its bytes already resolved.

    Holds bytes rather than a path because the two ingress modes differ: a
    stdio caller names a local file, while an HTTP caller must inline the
    content. Normalizing early keeps the upload path identical for both.
    """

    def __init__(self, name: str, content: bytes, mime_type: str) -> None:
        self.name = name
        self.content = content
        self.mime_type = mime_type

    @property
    def size(self) -> int:
        return len(self.content)


def _fingerprint(
    course_id: str,
    assignment_id: str,
    submission_type: str,
    payload_digest: str,
    attempt: int,
    allowed_attempts: int | None = None,
) -> str:
    """Bind a confirmation to exactly what was previewed, and to who previewed it.

    Including the observed attempt number means a submission that lands between
    preview and confirm invalidates the token rather than silently consuming a
    second attempt.

    The caller identity is constant here: one process holds one Canvas session,
    so every request acts as the same student. Keeping it in the fingerprint
    still binds a confirmation to the account that saw the preview, which is the
    property a future multi-user mode would need without changing the format.

    Including the attempt *limit* as well as the count matters because an
    instructor can change ``allowed_attempts`` in between. A preview that said
    "unlimited" could otherwise be confirmed against a freshly capped assignment
    and spend what is now the final attempt, with the student having agreed to
    something different.
    """
    raw = (
        f"{_caller_identity()}|{course_id}|{assignment_id}|"
        f"{submission_type}|{payload_digest}|{attempt}|{allowed_attempts}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _digest_payload(
    body: str | None,
    url: str | None,
    comment: str | None,
    files: list[_PreparedFile],
) -> str:
    """Hash the exact content that would be submitted.

    Every field is length-prefixed rather than concatenated, because plain
    concatenation is ambiguous: a file named ``a.txt`` holding ``XPAYLOAD``
    would hash identically to one named ``a.txtX`` holding ``PAYLOAD``. That
    would let a token approve content other than what was previewed, which is
    precisely the guarantee this digest exists to provide.

    ``comment`` is covered too. The preview displays it, so a confirmation that
    did not commit to it could swap in text the student never saw before it
    reached their instructor.
    """
    hasher = hashlib.sha256()

    def absorb(chunk: bytes) -> None:
        hasher.update(len(chunk).to_bytes(8, "big"))
        hasher.update(chunk)

    absorb((body or "").encode())
    absorb((url or "").encode())
    absorb((comment or "").encode())
    absorb(len(files).to_bytes(8, "big"))
    for prepared in files:
        absorb(prepared.name.encode())
        absorb(prepared.content)
    return hasher.hexdigest()




def _describe_attempts(assignment: dict, submission: dict) -> str:
    """Render attempt usage for the preview.

    Canvas encodes "unlimited" as ``allowed_attempts = -1`` (and often omits the
    field), which is the detail worth stating plainly: the student needs to know
    whether proceeding spends a scarce resource.
    """
    allowed = assignment.get("allowed_attempts")
    used = submission.get("attempt") or 0

    if allowed is None or allowed == -1:
        return f"Attempts: {used} used, unlimited allowed"

    remaining = allowed - used
    warning = "  ⚠️  This is your LAST attempt." if remaining <= 1 else ""
    return f"Attempts: {used} of {allowed} used, {remaining} remaining.{warning}"


def _decoded_size(encoded: str) -> int:
    """How many bytes a base64 string will decode to, computed without decoding.

    Used to reject an oversized upload before allocating it. The naive
    ``len(encoded) // 4 * 3`` overshoots by the number of padding characters, so
    a file whose decoded size is exactly the documented limit would be refused;
    subtracting the trailing '=' makes this exact for well-formed input.
    """
    padding = len(encoded) - len(encoded.rstrip("="))
    return max(0, len(encoded) // 4 * 3 - padding)


def _normalize_extensions(raw: list[str] | None) -> frozenset[str] | None:
    """Normalize an assignment's ``allowed_extensions`` into ``{'.pdf', ...}``.

    Canvas stores these without leading dots and with inconsistent case. An
    empty or absent list means the assignment does not restrict types.
    """
    if not raw:
        return None
    return frozenset(
        f".{str(ext).strip().lstrip('.').lower()}" for ext in raw if str(ext).strip()
    )


def _check_submission_name(
    name: str, allowed_extensions: frozenset[str] | None
) -> tuple[str, str | None]:
    """Sanitize a filename and check it against the ASSIGNMENT's own rules.

    Returns ``(safe_name, error)``.

    Deliberately no global extension allowlist. The instructor's
    ``allowed_extensions`` on the assignment is the legitimate statement of what
    that assignment accepts; a separate hard-coded list can only disagree with
    it, and the one previously used here rejected ordinary student work such as
    ``.heic`` photos and ``.tex`` sources. Sanitization is still applied, since
    a malicious *path* is a real attack whereas an unusual extension is not.
    """
    safe_name = sanitize_filename(name)
    if not safe_name or safe_name in (".", ".."):
        return "", f"❌ '{name}' is not a usable filename."

    if allowed_extensions is not None:
        extension = os.path.splitext(safe_name)[1].lower()
        if extension not in allowed_extensions:
            accepted = ", ".join(sorted(allowed_extensions))
            return "", (
                f"❌ This assignment does not accept "
                f"'{extension or 'files without an extension'}'. "
                f"It accepts: {accepted}"
            )
    return safe_name, None


def _prepare_files(
    file_paths: list[str] | None,
    file_contents: list[dict[str, str]] | None,
    allowed_extensions: frozenset[str] | None = None,
) -> tuple[list[_PreparedFile], str | None]:
    """Resolve either ingress mode into raw bytes.

    Returns ``(files, error)``.

    ``file_paths`` reads the *server's* filesystem. That is the caller's own
    machine here: this server is single-user and refuses non-loopback binds,
    whether it runs over stdio or HTTP. Callers that cannot reach the machine's
    filesystem should pass ``file_contents`` as base64 instead.
    """
    prepared: list[_PreparedFile] = []

    total_files = len(file_paths or []) + len(file_contents or [])
    if total_files > _MAX_UPLOAD_FILES:
        return [], (
            f"❌ Too many files ({total_files}). At most {_MAX_UPLOAD_FILES} may "
            "be submitted at once."
        )

    running_bytes = 0

    for path in file_paths or []:
        if not os.path.isfile(path):
            return [], f"❌ Cannot submit '{path}': no such file."
        try:
            file_size = os.path.getsize(path)
        except OSError as exc:
            return [], f"❌ Could not read '{path}': {exc}"
        if file_size > DEFAULT_MAX_FILE_SIZE_BYTES:
            return [], f"❌ '{path}' exceeds the maximum upload size."
        running_bytes += file_size
        if running_bytes > _MAX_TOTAL_UPLOAD_BYTES:
            return [], _too_large_message()

        safe_name, name_error = _check_submission_name(
            os.path.basename(path), allowed_extensions
        )
        if name_error:
            return [], name_error
        try:
            with open(path, "rb") as handle:
                content = handle.read()
        except OSError as exc:
            return [], f"❌ Could not read '{path}': {exc}"
        prepared.append(
            _PreparedFile(safe_name, content, detect_mime_type(safe_name))
        )

    for entry in file_contents or []:
        name = str(entry.get("name") or "").strip()
        encoded = entry.get("content_base64")
        if not name or not encoded:
            return [], "Error: each file_contents entry needs 'name' and 'content_base64'"

        encoded = encoded.strip()
        approx_size = _decoded_size(encoded)
        if approx_size > DEFAULT_MAX_FILE_SIZE_BYTES:
            return [], f"❌ '{name}' exceeds the maximum upload size."
        running_bytes += approx_size
        if running_bytes > _MAX_TOTAL_UPLOAD_BYTES:
            return [], _too_large_message()

        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return [], f"❌ '{name}' is not valid base64."

        safe_name, name_error = _check_submission_name(name, allowed_extensions)
        if name_error:
            return [], name_error
        if len(content) > DEFAULT_MAX_FILE_SIZE_BYTES:
            return [], f"❌ '{name}' exceeds the maximum upload size."

        prepared.append(
            _PreparedFile(safe_name, content, detect_mime_type(safe_name))
        )

    return prepared, None


async def _final_preflight(
    course_id: str,
    assignment_id: str,
    submission_type: str,
    payload_digest: str,
    confirmed_fingerprint: str,
) -> str | None:
    """Re-verify EVERY precondition immediately before the submit call.

    Returns an error message if anything has changed, or None if the write may
    proceed.

    This exists because arbitrary time passes between the earlier checks and the
    POST: the policy read, and for uploads a multi-step round trip per file. Any
    precondition checked earlier can have changed in that window, so all of them
    are re-checked here rather than only the attempt count. Three extra requests
    on a rare, irreversible, attempt-consuming operation is a trade worth making.

    If the state cannot be re-read, that counts as changed. Proceeding would mean
    submitting without the guarantee the student was promised.
    """
    allowed, reason = await check_student_write_allowed(course_id, "submit_assignment")
    if not allowed:
        return f"❌ Submission blocked. {reason}"

    assignment = await make_canvas_request(
        "get", f"/courses/{course_id}/assignments/{assignment_id}"
    )
    submission = await make_canvas_request(
        "get", f"/courses/{course_id}/assignments/{assignment_id}/submissions/self"
    )
    if (
        not isinstance(assignment, dict)
        or "error" in assignment
        or not isinstance(submission, dict)
        or "error" in submission
    ):
        return (
            "❌ Could not re-check your attempt count just before submitting, so "
            "nothing was submitted. Try again shortly."
        )

    if assignment.get("group_category_id"):
        return (
            "❌ This became a group assignment while the submission was being "
            "prepared, so nothing was submitted. Agent-assisted submission is "
            "not supported for group assignments. Please submit it in Canvas."
        )

    current = _fingerprint(
        course_id,
        assignment_id,
        submission_type,
        payload_digest,
        submission.get("attempt") or 0,
        assignment.get("allowed_attempts"),
    )
    if current != confirmed_fingerprint:
        return (
            "❌ Your submission state changed while this was being prepared "
            "(another submission landed, or the attempt limit changed). Nothing "
            "was submitted. Check get_my_submission, then preview again."
        )
    return None


async def _upload_one(
    course_id: str, assignment_id: str, prepared: _PreparedFile
) -> tuple[str | None, str | None]:
    """Run Canvas's 3-step upload for one file. Returns ``(file_id, error)``.

    Step 1 targets ``/submissions/self/files``, which *is* structurally
    self-scoped: the slot Canvas hands back belongs to the calling user's own
    submission and cannot be redirected at another student.
    """
    slot = await make_canvas_request(
        "post",
        f"/courses/{course_id}/assignments/{assignment_id}/submissions/self/files",
        data={
            "name": prepared.name,
            "size": prepared.size,
            "content_type": prepared.mime_type,
        },
        use_form_data=True,
    )
    if isinstance(slot, dict) and "error" in slot:
        return None, f"❌ Failed to request an upload slot for '{prepared.name}': {slot['error']}"

    upload_url = slot.get("upload_url")
    if not upload_url:
        return None, f"❌ Canvas returned no upload URL for '{prepared.name}'."

    handle_fd, temp_path = tempfile.mkstemp()
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(prepared.content)
        stored = await upload_file_to_storage(
            upload_url=upload_url,
            upload_params=slot.get("upload_params", {}),
            file_path=temp_path,
            filename=prepared.name,
            content_type=prepared.mime_type,
        )
    finally:
        os.unlink(temp_path)

    if isinstance(stored, dict) and "error" in stored:
        return None, f"❌ Upload failed for '{prepared.name}': {stored['error']}"

    file_id = (
        stored.get("id")
        or (stored.get("attachment") or {}).get("id")
        or (stored.get("file") or {}).get("id")
    )
    if not file_id:
        if stored.get("success"):
            return None, (
                f"❌ '{prepared.name}' uploaded, but Canvas returned no file ID "
                "to attach it with, so the submission was not sent. Check "
                "whether the file appears in Canvas before retrying."
            )
        return None, f"❌ Canvas did not return a file ID for '{prepared.name}'."
    return str(file_id), None


def register_student_write_tools(mcp: FastMCP) -> None:
    """Register Tier 1 student tools.

    ``get_my_submission`` is read-only and always registered. The write tools
    register only when the operator has named them in ``STUDENT_WRITE_TOOLS``,
    so an unlisted tool never becomes visible to an agent at all.
    """
    enabled = get_config().student_write_tools

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_submission(
        course_identifier: str | int,
        assignment_id: str | int,
    ) -> str:
        """Get your own submission for an assignment, including attempts used.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Canvas assignment ID
        """
        validated_assignment_id = coerce_canvas_id(assignment_id)
        if validated_assignment_id is None:
            return _INVALID_ASSIGNMENT_ID
        assignment_id = validated_assignment_id

        course_id = await get_course_id(course_identifier)
        if not course_id:
            return f"Error: Could not find course {course_identifier}"

        submission = await make_canvas_request(
            "get",
            f"/courses/{course_id}/assignments/{assignment_id}/submissions/self",
            params={"include[]": ["submission_comments", "assignment"]},
        )
        if isinstance(submission, dict) and "error" in submission:
            return f"Error fetching submission: {submission['error']}"

        assignment = submission.get("assignment") or {}
        lines = [
            f"Submission for: {fence_untrusted_inline(assignment.get('name', f'Assignment {assignment_id}'), 'assignment name')}",
            f"Status: {submission.get('workflow_state', 'unsubmitted')}",
        ]

        if submission.get("submitted_at"):
            lines.append(f"Submitted: {format_date(submission['submitted_at'])}")
        else:
            lines.append("Submitted: not yet")

        if assignment.get("due_at"):
            lines.append(f"Due: {format_date(assignment['due_at'])}")
        if assignment.get("lock_at"):
            lines.append(f"Locks: {format_date(assignment['lock_at'])}")

        lines.append(_describe_attempts(assignment, submission))

        if submission.get("grade") is not None:
            lines.append(f"Grade: {submission['grade']}")

        comments = submission.get("submission_comments") or []
        if comments:
            lines.append(f"\nComments ({len(comments)}):")
            for comment in comments:
                author = comment.get("author_name")
                prefix = (
                    f"{fence_untrusted_inline(author, 'comment author')}: "
                    if author else ""
                )
                lines.append(
                    f"• {prefix}"
                    f"{fence_untrusted(comment.get('comment', ''), 'submission comment')}"
                )

        return "\n".join(lines)

    if "submit_assignment" in enabled:

        @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
        @validate_params
        async def submit_assignment(
            course_identifier: str | int,
            assignment_id: str | int,
            submission_type: str,
            body: str | None = None,
            url: str | None = None,
            file_paths: list[str] | None = None,
            file_contents: list[dict[str, str]] | None = None,
            comment: str | None = None,
            confirmation_token: str | None = None,
            from_draft: bool = False,
        ) -> str:
            """Submit one of YOUR OWN assignments. Consumes an attempt.

            Two-step by design. Call it without a confirmation_token to get a
            preview of exactly what would be sent plus a token; show that preview
            to the student, and only after they approve it call again passing the
            token to actually submit.
            The token expires, is single-use, and is void if the content or the
            attempt count changed since the preview.

            Args:
                course_identifier: Course code or Canvas ID
                assignment_id: Canvas assignment ID
                submission_type: online_text_entry, online_url, or online_upload
                body: HTML/text content for online_text_entry
                url: URL for online_url
                file_paths: Local file paths (local stdio servers only, any file type)
                file_contents: Inline files as [{"name": ..., "content_base64": ...}]
                comment: Optional comment to include with the submission
                confirmation_token: Token from the preview call; omit to preview
                from_draft: Submit the latest saved draft (online_text_entry only)
            """
            if submission_type not in _SUPPORTED_TYPES:
                return _rejected_submission_type(submission_type)

            validated_assignment_id = coerce_canvas_id(assignment_id)
            if validated_assignment_id is None:
                return _INVALID_ASSIGNMENT_ID
            assignment_id = validated_assignment_id

            course_id = await get_course_id(course_identifier)
            if not course_id:
                return f"Error: Could not find course {course_identifier}"

            allowed, reason = await check_student_write_allowed(
                course_id, "submit_assignment"
            )
            if not allowed:
                return f"❌ Submission blocked. {reason}"

            if contains_fence_markers(body or "") or contains_fence_markers(comment or ""):
                return FENCE_LEAK_ERROR

            if from_draft:
                if submission_type != "online_text_entry":
                    return "Error: from_draft only applies to online_text_entry submissions."
                draft = student_store.read_draft(course_id, assignment_id)
                if draft is None:
                    return (
                        "Error: no saved draft for this assignment. Use "
                        "save_assignment_draft first, or pass body explicitly."
                    )
                body = draft["content"]

            if submission_type == "online_text_entry" and not body:
                return "Error: online_text_entry requires 'body'"
            if submission_type == "online_url" and not url:
                return "Error: online_url requires 'url'"
            if submission_type == "online_upload" and not (file_paths or file_contents):
                return "Error: online_upload requires 'file_paths' or 'file_contents'"

            assignment = await make_canvas_request(
                "get", f"/courses/{course_id}/assignments/{assignment_id}"
            )
            if isinstance(assignment, dict) and "error" in assignment:
                return f"Error fetching assignment: {assignment['error']}"

            if assignment.get("group_category_id"):
                return (
                    "❌ This is a group assignment. Agent-assisted submission is "
                    "not supported for group assignments, because it would submit "
                    "on behalf of your whole group. Please submit it in Canvas."
                )

            if submission_type not in (assignment.get("submission_types") or []):
                return (
                    f"❌ This assignment does not accept '{submission_type}'. "
                    f"It accepts: {', '.join(assignment.get('submission_types') or []) or 'nothing'}"
                )

            submission = await make_canvas_request(
                "get",
                f"/courses/{course_id}/assignments/{assignment_id}/submissions/self",
            )
            if not isinstance(submission, dict) or "error" in submission:
                detail = (
                    submission.get("error")
                    if isinstance(submission, dict)
                    else "unexpected response from Canvas"
                )
                return (
                    "❌ Could not read your current submission state, so the "
                    f"attempt count is unknown: {detail}\n"
                    "Nothing was submitted. Try again shortly."
                )
            attempt = submission.get("attempt") or 0

            prepared, prep_error = _prepare_files(
                file_paths,
                file_contents,
                _normalize_extensions(assignment.get("allowed_extensions")),
            )
            if prep_error:
                return prep_error

            digest = _digest_payload(body, url, comment, prepared)
            fingerprint = _fingerprint(
                course_id,
                str(assignment_id),
                submission_type,
                digest,
                attempt,
                assignment.get("allowed_attempts"),
            )

            if not confirmation_token:
                token = _issue_token(fingerprint)

                preview = [
                    "📋 Submission preview — NOTHING has been submitted yet.",
                    "",
                    f"Assignment: {assignment.get('name', assignment_id)}",
                    f"Type: {submission_type}",
                ]
                if assignment.get("due_at"):
                    preview.append(f"Due: {format_date(assignment['due_at'])}")
                if assignment.get("lock_at"):
                    preview.append(f"Locks: {format_date(assignment['lock_at'])}")
                preview.append(_describe_attempts(assignment, submission))
                preview.append("")

                if submission_type == "online_text_entry":
                    text = body or ""
                    preview.append(f"Content ({len(text)} chars):\n{text}")
                elif submission_type == "online_url":
                    preview.append(f"URL: {url}")
                else:
                    preview.append("Files:")
                    for item in prepared:
                        preview.append(f"• {item.name} ({item.mime_type}, {item.size} bytes)")
                if comment:
                    preview.append(f"\nComment: {comment}")

                preview.append(
                    "\n➡️  Show this to the student. Only after they approve it, "
                    f"submit by calling again with confirmation_token='{token}' "
                    "and identical content.\n"
                    "This consumes an attempt and cannot be undone."
                )
                return "\n".join(preview)

            token_error = _check_token(confirmation_token, fingerprint)
            if token_error:
                return token_error

            if not _reserve_confirmation(fingerprint, confirmation_token):
                return (
                    "❌ That confirmation was already used. Nothing was "
                    "submitted. Run the preview again."
                )

            try:
                allowed, reason = await check_student_write_allowed(
                    course_id, "submit_assignment"
                )
                if not allowed:
                    _release_confirmation(fingerprint, confirmation_token)
                    return f"❌ Submission blocked. {reason}"

                data: dict[str, Any] = {"submission[submission_type]": submission_type}
                if submission_type == "online_text_entry":
                    data["submission[body]"] = body
                elif submission_type == "online_url":
                    data["submission[url]"] = url
                else:
                    file_ids = []
                    for item in prepared:
                        file_id, upload_error = await _upload_one(
                            course_id, str(assignment_id), item
                        )
                        if upload_error:
                            _release_confirmation(fingerprint, confirmation_token)
                            return f"{upload_error}\nNothing was submitted."
                        file_ids.append(file_id)
                    data["submission[file_ids][]"] = file_ids

                if comment:
                    data["comment[text_comment]"] = comment

                assert_no_identity_override(data)

                preflight_error = await _final_preflight(
                    course_id, str(assignment_id), submission_type, digest, fingerprint
                )
                if preflight_error:
                    _release_confirmation(fingerprint, confirmation_token)
                    return preflight_error

                response = await make_canvas_request(
                    "post",
                    f"/courses/{course_id}/assignments/{assignment_id}/submissions",
                    data=data,
                    use_form_data=True,
                )
                if isinstance(response, dict) and "error" in response:
                    return (
                        f"❌ Submission failed: {response['error']}\n"
                        "Check get_my_submission before retrying — if Canvas accepted "
                        "it and only the reply was lost, retrying would spend another "
                        "attempt."
                    )

                lines = ["✅ Submitted.", f"Assignment: {assignment.get('name', assignment_id)}"]
                if response.get("submitted_at"):
                    lines.append(f"Submitted at: {format_date(response['submitted_at'])}")
                if response.get("attempt"):
                    lines.append(f"Attempt: {response['attempt']}")
                if response.get("late"):
                    lines.append("⚠️  Canvas marked this submission LATE.")
                return "\n".join(lines)
            finally:
                _finish_confirmation(fingerprint)

    if "comment_on_my_submission" in enabled:

        @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
        @validate_params
        async def comment_on_my_submission(
            course_identifier: str | int,
            assignment_id: str | int,
            comment: str,
        ) -> str:
            """Add a comment to YOUR OWN submission.

            Args:
                course_identifier: Course code or Canvas ID
                assignment_id: Canvas assignment ID
                comment: The comment text
            """
            if not comment.strip():
                return "Error: comment cannot be empty"

            if contains_fence_markers(comment):
                return FENCE_LEAK_ERROR

            validated_assignment_id = coerce_canvas_id(assignment_id)
            if validated_assignment_id is None:
                return _INVALID_ASSIGNMENT_ID
            assignment_id = validated_assignment_id

            course_id = await get_course_id(course_identifier)
            if not course_id:
                return f"Error: Could not find course {course_identifier}"

            allowed, reason = await check_student_write_allowed(
                course_id, "comment_on_my_submission"
            )
            if not allowed:
                return f"❌ Comment blocked. {reason}"

            data = {"comment[text_comment]": comment}
            assert_no_identity_override(data)

            response = await make_canvas_request(
                "put",
                f"/courses/{course_id}/assignments/{assignment_id}/submissions/self",
                data=data,
                use_form_data=True,
            )
            if isinstance(response, dict) and "error" in response:
                return f"❌ Comment failed: {response['error']}"

            return "✅ Comment added to your submission."

    if "mark_module_item_done" in enabled:

        @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
        @validate_params
        async def mark_module_item_done(
            course_identifier: str | int,
            module_id: str | int,
            item_id: str | int,
        ) -> str:
            """Mark a module item done for YOURSELF.

            Args:
                course_identifier: Course code or Canvas ID
                module_id: Canvas module ID
                item_id: Canvas module item ID
            """
            course_id = await get_course_id(course_identifier)
            if not course_id:
                return f"Error: Could not find course {course_identifier}"

            allowed, reason = await check_student_write_allowed(
                course_id, "mark_module_item_done"
            )
            if not allowed:
                return f"❌ Update blocked. {reason}"

            item_endpoint = (
                f"/courses/{course_id}/modules/{module_id}/items/{item_id}"
            )

            item = await make_canvas_request("get", item_endpoint)
            if not isinstance(item, dict) or "error" in item:
                detail = item.get("error") if isinstance(item, dict) else item
                return f"❌ Could not read module item: {detail}"

            requirement = item.get("completion_requirement")
            if not isinstance(requirement, dict) or requirement.get("type") != "must_mark_done":
                have = (
                    f"a '{requirement.get('type')}' completion requirement"
                    if isinstance(requirement, dict)
                    else "no completion requirement"
                )
                return (
                    f"❌ '{item.get('title', item_id)}' cannot be marked done: it has "
                    f"{have}, not 'must_mark_done'. Canvas accepts the request but "
                    "nothing changes. Only items the instructor configured with a "
                    "'Mark as done' requirement support this."
                )

            if requirement.get("completed"):
                return "✅ Module item is already marked done."

            response = await make_canvas_request(
                "put",
                f"{item_endpoint}/done",
            )
            if isinstance(response, dict) and "error" in response:
                return f"❌ Could not mark item done: {response['error']}"

            after = await make_canvas_request("get", item_endpoint)
            if isinstance(after, dict) and "error" in after:
                return (
                    "❌ Canvas accepted the mark-done request, but the follow-up "
                    f"read failed: {after['error']}\n"
                    "Check the module in Canvas before retrying."
                )
            confirmed = (
                isinstance(after, dict)
                and isinstance(after.get("completion_requirement"), dict)
                and after["completion_requirement"].get("completed")
            )
            if not confirmed:
                return unconfirmed_write_warning(
                    "the module item was marked done",
                    {
                        "Item": item.get("title", item_id),
                        "Module": module_id,
                        "Course": course_id,
                    },
                    "Canvas accepted the request but the item still shows as not "
                    "done. Check the module in Canvas and retry.",
                )

            return "✅ Module item marked done."
