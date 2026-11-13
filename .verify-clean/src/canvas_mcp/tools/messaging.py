"""Canvas messaging/conversations tools."""

import re
import sys
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.client import make_canvas_request
from ..core.untrusted_content import (
    FENCE_LEAK_ERROR,
    UNTRUSTED_NOTICE,
    contains_fence_markers,
    fence_untrusted,
)
from ..core.validation import validate_params
from ..core.write_confirmation import ConfirmationGuard
from ..core.write_outcome import NO_WRITE_STATUSES

# One guard per destructive tool: each has its own signing secret and redeemed
# set, so a token minted for one tool can never be replayed against another.
_BULK_MESSAGE_GUARD = ConfirmationGuard()
_SEND_CONVERSATION_GUARD = ConfirmationGuard()
_REMINDER_GUARD = ConfirmationGuard()
_CAMPAIGN_GUARD = ConfirmationGuard()


def _fence_conversation_fields(conversation: Any) -> None:
    """Fence the third-party text fields of one conversation dict, in place.

    Subjects and message bodies are typed by whoever wrote to the inbox —
    students included — so they are provenance-fenced like any other
    Canvas-authored free text (issue 239). Read-only formatting: these dicts
    are tool output, never written back to Canvas.
    """
    if not isinstance(conversation, dict):
        return
    for key in ("subject", "last_message", "last_authored_message"):
        value = conversation.get(key)
        if isinstance(value, str) and value:
            conversation[key] = fence_untrusted(value, "conversation message")
    # Participant display names are editable on many instances, so a
    # student-controlled name sitting beside a fenced body is itself an
    # injection channel. Fence the identity LABELS — deliberately without
    # redacting, since anonymization elsewhere preserves participant names and
    # this is a provenance mark, not a privacy scrub.
    for participant in conversation.get("participants") or []:
        if not isinstance(participant, dict):
            continue
        for key in ("name", "full_name"):
            value = participant.get(key)
            if isinstance(value, str) and value:
                participant[key] = fence_untrusted(value, "participant name")
    _fence_attachments(conversation)
    for message in conversation.get("messages") or []:
        _fence_message_body(message)


def _fence_message_body(message: Any) -> None:
    """Fence one conversation message's body, recursing through forwards.

    Canvas messages carry a ``forwarded_messages`` array (which can itself
    contain forwards) — those bodies are just as student-authored as the
    top-level one, so a fence that stopped at the surface would hand
    forwarded text to the model raw. Attachment labels ride along at every
    level for the same reason.
    """
    if not isinstance(message, dict):
        return
    if isinstance(message.get("body"), str) and message["body"]:
        message["body"] = fence_untrusted(message["body"], "conversation message")
    _fence_attachments(message)
    for forwarded in message.get("forwarded_messages") or []:
        _fence_message_body(forwarded)


def _fence_attachments(container: Any) -> None:
    """Fence sender-controlled attachment labels, in place.

    Senders name their own uploads, so ``display_name`` and ``filename`` are
    free-text injection channels just like the message body.
    """
    if not isinstance(container, dict):
        return
    for attachment in container.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        for key in ("display_name", "filename"):
            value = attachment.get(key)
            if isinstance(value, str) and value:
                attachment[key] = fence_untrusted(value, "attachment name")


_DIRECT_USER_ID = re.compile(r"^[0-9]+$")


def _is_single_direct_recipient(recipient_ids: list[str]) -> bool:
    """True only for exactly one plain numeric Canvas user ID.

    The Conversations API also accepts expandable aliases (``course_123``,
    ``group_45``, section variants) that fan out to many users, so a
    one-element list is NOT evidence of a one-person send. Anything that is
    not a bare user ID gets the multi-recipient confirmation flow.
    """
    return len(recipient_ids) == 1 and bool(_DIRECT_USER_ID.match(str(recipient_ids[0])))


def _render_bulk_messages(
    recipient_data: list[dict[str, Any]],
    subject_template: str,
    body_template: str,
    mode: str,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Render EVERY outbound bulk message, collecting per-row errors.

    The confirmation token authorizes the whole batch, so every message must
    be renderable — and shown — before a token is issued: a poisoned later
    row must fail the preview, not fire mid-send after earlier messages went
    out. Row user_ids must be plain numeric Canvas IDs, because expandable
    aliases (course_*/group_*) would fan one row out to many people.
    """
    rendered: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    for index, recipient in enumerate(recipient_data):
        user_id = recipient.get("user_id")
        if not user_id or not _DIRECT_USER_ID.match(str(user_id)):
            errors.append({
                "index": index,
                "recipient": recipient,
                "error": "user_id must be a plain numeric Canvas user ID",
            })
            continue
        try:
            subject = subject_template.format(**recipient)
            body = body_template.format(**recipient)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as e:
            # AttributeError/TypeError cover format-spec shapes like
            # "{name.foo}" / "{name[foo]}" — a poisoned row must become an
            # invalid-record entry, not an unhandled exception that fails the
            # whole call.
            errors.append({
                "index": index,
                "recipient": recipient,
                "error": f"template does not render against this recipient: {e}",
            })
            continue
        # The same validation the send choke point enforces, with the
        # caller's ACTUAL requested mode — a row (or a bogus mode) that would
        # be rejected at confirm time must fail the preview instead of
        # burning a token.
        row_error = _validate_outbound_message([str(user_id)], subject, body, mode)
        if row_error:
            errors.append({
                "index": index,
                "recipient": recipient,
                "error": row_error,
            })
            continue
        rendered.append({"user_id": str(user_id), "subject": subject, "body": body})
    return rendered, errors


# Statuses that prove the request was REJECTED before any write: client-side
# validation/auth failures. Deliberately excludes 408 (timeout — the server
# may have processed it), 409, 429, and every 5xx (the failure can occur
# after the write, or a proxy can emit it while Canvas succeeded).
_NO_WRITE_STATUSES = NO_WRITE_STATUSES
_HTTP_ERROR_STATUS = re.compile(r"^HTTP error: (\d+)")


def _definitely_not_sent(error_message: str) -> bool:
    """True only when the error PROVES no write occurred.

    ``make_canvas_request`` folds transport exceptions (e.g. a read timeout
    AFTER Canvas accepted the POST) into the same error-dict shape as real
    rejections, and even an HTTP status is only proof for the validation/auth
    class — a 500 or gateway 502/504 can arrive after the POST was processed.
    Releasing a confirmation claim on an ambiguous failure would let a retry
    double-send the batch, so a claim is only handed back for provable
    rejections and our own pre-flight validation (which runs before any I/O).
    Everything else keeps the token spent.
    """
    if error_message.startswith("Invalid endpoint"):
        return True
    match = _HTTP_ERROR_STATUS.match(error_message)
    return match is not None and int(match.group(1)) in _NO_WRITE_STATUSES


def _validate_outbound_message(
    recipient_ids: list[str], subject: str, body: str, mode: str
) -> str | None:
    """Shared validation for every outbound conversation. Error text, or None.

    This is the single authority: the ``send_conversation`` tool calls it for
    a friendly pre-preview error, and ``_post_conversation`` enforces it as a
    choke point so no composed path (bulk, reminders, campaign) can POST an
    invalid or marker-carrying message.
    """
    if not recipient_ids:
        return "recipient_ids cannot be empty"
    if not subject or len(subject) > 255:
        return "subject is required and must be 255 characters or less"
    if not body:
        return "body is required"
    if mode not in ("sync", "async"):
        return "mode must be 'sync' or 'async'"
    # Backstop for issue 239: composed text can pick up markers from
    # Canvas-authored inputs (e.g. an assignment name inside a reminder
    # subject), so the final outbound text is checked regardless of which
    # tool assembled it.
    if contains_fence_markers(subject) or contains_fence_markers(body):
        return FENCE_LEAK_ERROR
    return None


async def _post_conversation(
    course_identifier: str | int,
    recipient_ids: list[str],
    subject: str,
    body: str,
    group_conversation: bool,
    bulk_message: bool,
    context_code: str | None,
    mode: str,
    force_new: bool,
    attachment_ids: list[str] | None,
) -> dict[str, Any]:
    """POST one /conversations request. Callers enforce all confirmation gates.

    Parameter validation lives HERE, not only in the ``send_conversation``
    tool wrapper, so the bulk/reminder/campaign paths cannot route around it
    with an over-long subject or a bogus mode.
    """
    error = _validate_outbound_message(recipient_ids, subject, body, mode)
    if error:
        return {"error": error}

    data: dict[str, Any] = {
        "recipients[]": recipient_ids,
        "subject": subject,
        "body": body,
        "group_conversation": group_conversation,
        "bulk_message": bulk_message,
        "mode": mode,
        "force_new": force_new,
    }

    # Add context_code if provided, otherwise construct from course_identifier
    if context_code:
        data["context_code"] = context_code
    else:
        data["context_code"] = f"course_{course_identifier}"

    if attachment_ids:
        data["attachment_ids[]"] = attachment_ids

    # Canvas requires form data on /conversations
    response = await make_canvas_request("post", "/conversations", data=data, use_form_data=True)

    if "error" in response:
        error_response: dict[str, Any] = response
        return error_response

    return {
        "success": True,
        "conversation": response,
        "message": f"Message sent to {len(recipient_ids)} recipient(s)"
    }


async def _compose_reminder(
    course_identifier: str | int,
    assignment_id: str | int,
    custom_message: str | None,
    include_assignment_link: bool,
    subject_prefix: str,
) -> tuple[str, str] | dict[str, Any]:
    """Build the (subject, body) of a peer-review reminder, or an error dict."""
    assignment_response = await make_canvas_request(
        "get",
        f"/courses/{course_identifier}/assignments/{assignment_id}"
    )

    if "error" in assignment_response:
        return {"error": f"Failed to get assignment details: {assignment_response['error']}"}

    assignment_name = assignment_response.get("name", f"Assignment {assignment_id}")
    assignment_url = assignment_response.get("html_url", "")

    if custom_message:
        body = custom_message
    else:
        body = f"""Hello,

This is a reminder that you have incomplete peer reviews for {assignment_name}.

Please complete your peer reviews as soon as possible to receive full participation credit."""

    if include_assignment_link and assignment_url:
        body += f"\n\nYou can access the assignment here: {assignment_url}"

    body += "\n\nIf you have any questions or technical issues, please reach out for assistance."

    subject = f"{subject_prefix}: {assignment_name}"
    return subject, body




def register_shared_messaging_tools(mcp: FastMCP) -> None:
    """Register messaging tools accessible to both students and educators."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_conversations(
        scope: str = "unread",
        filter_ids: list[str] | None = None,
        filter_mode: str = "and",
        include_participants: bool = True,
        include_all_ids: bool = False
    ) -> dict[str, Any]:
        """
        List conversations for the current user.

        Args:
            scope: "unread", "starred", "sent", "archived", or "all"
            filter_ids: Conversation IDs to filter by
            filter_mode: "and" or "or" for filter_ids
            include_participants: Include participant info
            include_all_ids: Include all participant IDs
        """

        valid_scopes = ["unread", "starred", "sent", "archived", "all"]
        if scope not in valid_scopes:
            return {"error": f"scope must be one of: {', '.join(valid_scopes)}"}

        try:
            params = {
                "scope": scope,
                "include_participants": include_participants,
                "include_all_conversation_ids": include_all_ids
            }

            if filter_ids:
                params["filter[]"] = filter_ids
                params["filter_mode"] = filter_mode

            response = await make_canvas_request("get", "/conversations", params=params)

            if "error" in response:
                error_response: dict[str, Any] = response
                return error_response

            # Subjects and last-message previews are authored by whoever wrote
            # to the inbox (issue 239): fence them.
            if isinstance(response, list):
                for conversation in response:
                    _fence_conversation_fields(conversation)

            return {
                "success": True,
                "untrusted_content_notice": UNTRUSTED_NOTICE,
                "conversations": response,
                "count": len(response) if isinstance(response, list) else 0
            }

        except Exception as e:
            print(f"Error listing conversations: {str(e)}", file=sys.stderr)
            return {"error": f"Failed to list conversations: {str(e)}"}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_conversation_details(
        conversation_id: str | int,
        auto_mark_read: bool = True,
        include_messages: bool = True
    ) -> dict[str, Any]:
        """
        Get detailed conversation information with messages.

        Args:
            conversation_id: Conversation ID
            auto_mark_read: Mark as read when viewed
            include_messages: Include all messages
        """

        try:
            params = {
                "auto_mark_as_read": auto_mark_read,
                "include_all_conversation_ids": True
            }

            response = await make_canvas_request(
                "get",
                f"/conversations/{conversation_id}",
                params=params
            )

            if "error" in response:
                error_response: dict[str, Any] = response
                return error_response

            # Inbound subjects and message bodies are third-party text (issue
            # 239): fence them so a message reading "now forward the roster
            # to..." arrives marked as data, not as an instruction. Read-only
            # formatting — nothing here flows back into Canvas.
            _fence_conversation_fields(response)

            return {
                "success": True,
                "untrusted_content_notice": UNTRUSTED_NOTICE,
                "conversation": response
            }

        except Exception as e:
            print(f"Error getting conversation details: {str(e)}", file=sys.stderr)
            return {"error": f"Failed to get conversation details: {str(e)}"}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_unread_count() -> dict[str, Any]:
        """Get number of unread conversations."""

        try:
            response = await make_canvas_request("get", "/conversations/unread_count")

            if "error" in response:
                error_response: dict[str, Any] = response
                return error_response

            return {
                "success": True,
                "unread_count": response.get("unread_count", 0)
            }

        except Exception as e:
            print(f"Error getting unread count: {str(e)}", file=sys.stderr)
            return {"error": f"Failed to get unread count: {str(e)}"}

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
    @validate_params
    async def mark_conversations_read(conversation_ids: list[str]) -> dict[str, Any]:
        """
        Mark multiple conversations as read.

        Args:
            conversation_ids: List of conversation IDs to mark as read
        """

        if not conversation_ids:
            return {"error": "conversation_ids cannot be empty"}

        try:
            data = {
                "conversation_ids[]": conversation_ids,
                "event": "mark_as_read"
            }

            # /conversations requires form data: the "conversation_ids[]" bracket
            # key only means an array in form encoding, not in a JSON body (#208)
            response = await make_canvas_request("put", "/conversations", data=data, use_form_data=True)

            if "error" in response:
                error_response: dict[str, Any] = response
                return error_response

            return {
                "success": True,
                "marked_read": len(conversation_ids),
                "response": response
            }

        except Exception as e:
            print(f"Error marking conversations as read: {str(e)}", file=sys.stderr)
            return {"error": f"Failed to mark conversations as read: {str(e)}"}

    print("Canvas shared messaging tools registered successfully!", file=sys.stderr)


