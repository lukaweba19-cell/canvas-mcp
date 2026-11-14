"""File-related MCP tools for Canvas API.

Provides tools for uploading, downloading, reading, and listing files in Canvas courses.
Uploaded files can be used with other tools like add_module_item (for adding
files to modules) and send_conversation (for attaching files to messages).

The Canvas file upload process uses a 3-step protocol:
1. Request upload URL from Canvas API
2. Upload file to external storage (S3/Instructure)
3. Confirm upload and get final file object

This module handles all three steps transparently.
"""

import base64
import os
import tempfile

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core import extract, student_store
from ..core.cache import get_course_code, get_course_id
from ..core.client import (
    fetch_all_paginated_results,
    iter_canvas_file_bytes,
    make_canvas_request,
)
from ..core.config import get_config
from ..core.file_validation import (
    format_file_size,
    sanitize_filename,
)
from ..core.untrusted_content import fence_untrusted_inline
from ..core.validation import validate_params


def register_shared_file_tools(mcp: FastMCP) -> None:
    """Register file tools accessible to both students and educators."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def download_course_file(
        course_identifier: str | int,
        file_id: str | int,
        save_directory: str | None = None,
    ) -> str:
        """Download a file from a Canvas course to the local filesystem.

        Saves to the local machine the server runs on (stdio or loopback
        HTTP). Use read_course_file to get file content back in the response
        instead.

        Use list_course_files or list_module_items to find file IDs.

        Args:
            course_identifier: Course code or Canvas ID
            file_id: Canvas file ID
            save_directory: Local directory to save to (default: system temp dir, must exist)
        """
        course_id = await get_course_id(course_identifier)

        file_info = await make_canvas_request(
            "get",
            f"/courses/{course_id}/files/{file_id}"
        )

        if isinstance(file_info, dict) and "error" in file_info:
            return f"Error getting file info: {file_info['error']}"

        raw_filename = file_info.get("display_name") or file_info.get("filename", f"file_{file_id}")
        filename = sanitize_filename(raw_filename)
        download_url = file_info.get("url")
        content_type = file_info.get("content-type", "unknown")

        if not download_url:
            return "Error: No download URL available for this file. Check permissions."

        from pathlib import Path
        save_dir = Path(save_directory or tempfile.gettempdir()).resolve()
        if not save_dir.is_dir():
            return f"Error: Directory does not exist: {save_directory}"

        save_path = (save_dir / filename).resolve()
        if not save_path.is_relative_to(save_dir):
            return "Error: Invalid filename - path outside allowed directory"

        open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(save_path, open_flags, 0o600)
        except FileExistsError:
            return (
                f"Error: '{save_path}' already exists. Refusing to overwrite it — "
                f"remove it first or pass a different save_directory."
            )
        except OSError as e:
            return f"Error creating destination file: {e}"

        try:
            total_bytes = 0
            with os.fdopen(fd, 'wb') as f:
                async for chunk in iter_canvas_file_bytes(download_url):
                    f.write(chunk)
                    total_bytes += len(chunk)
        except Exception as e:
            try:
                os.unlink(save_path)
            except OSError:
                pass
            return f"Error downloading file: {str(e)}"

        size_str = format_file_size(total_bytes)
        course_display = await get_course_code(course_id) or course_identifier

        result = f"Downloaded: {fence_untrusted_inline(filename, 'file name')}\n"
        result += f"  Path: {save_path}\n"
        result += f"  Size: {size_str}\n"
        result += f"  Type: {content_type}\n"
        result += f"  Course: {course_display}\n"
        return result

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def read_course_file(
        course_identifier: str | int,
        file_id: str | int,
        max_size_mb: float = 25.0,
    ) -> str:
        """Read a file from a Canvas course and return its content as base64.

        Unlike download_course_file which saves to the server's local filesystem,
        this tool returns the file content directly in the response. This is useful
        when the MCP server runs on a different machine than the client.

        Use list_course_files or list_module_items to find file IDs.

        Args:
            course_identifier: Course code or Canvas ID
            file_id: Canvas file ID
            max_size_mb: Maximum file size in MB to read (default: 25). Clamped server-side to
                READ_FILE_MAX_SIZE_MB (default 100). Files larger than the effective limit are
                rejected to avoid excessive memory usage.
        """
        if max_size_mb <= 0:
            return (
                f"Error: max_size_mb must be positive (got {max_size_mb}). "
                f"Pass a value like 25 for a 25 MB limit."
            )

        server_max_mb = get_config().read_file_max_size_mb
        effective_max_mb = min(float(max_size_mb), server_max_mb)
        max_size_bytes = int(effective_max_mb * 1024 * 1024)

        course_id = await get_course_id(course_identifier)

        file_info = await make_canvas_request(
            "get",
            f"/courses/{course_id}/files/{file_id}"
        )

        if isinstance(file_info, dict) and "error" in file_info:
            return f"Error getting file info: {file_info['error']}"

        raw_filename = file_info.get("display_name") or file_info.get("filename", f"file_{file_id}")
        filename = sanitize_filename(raw_filename)
        download_url = file_info.get("url")
        content_type = file_info.get("content-type", "unknown")
        reported_size = file_info.get("size", 0)

        if not download_url:
            return "Error: No download URL available for this file. Check permissions."

        if reported_size and reported_size > max_size_bytes:
            return (
                f"Error: File '{filename}' is {format_file_size(reported_size)}, "
                f"which exceeds the {effective_max_mb} MB limit. "
                f"Use download_course_file instead for large files."
            )

        try:
            buffer = bytearray()
            async for chunk in iter_canvas_file_bytes(download_url):
                if len(buffer) + len(chunk) > max_size_bytes:
                    return (
                        f"Error: File '{filename}' exceeds the {effective_max_mb} MB limit "
                        f"during download. Use download_course_file instead for large files."
                    )
                buffer.extend(chunk)

            base64_content = base64.b64encode(buffer).decode("ascii")

            size_str = format_file_size(len(buffer))
            course_display = await get_course_code(course_id) or course_identifier

            result = f"Read: {fence_untrusted_inline(filename, 'file name')}\n"
            result += f"  Size: {size_str}\n"
            result += f"  Type: {content_type}\n"
            result += f"  Course: {course_display}\n"
            result += "  Encoding: base64\n"
            result += f"  Content:\n{base64_content}\n"
            return result

        except Exception as e:
            return f"Error reading file: {str(e)}"

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_course_files(
        course_identifier: str | int,
        search_term: str | None = None,
        sort: str = "updated_at",
        order: str = "desc",
    ) -> str:
        """List files in a Canvas course with optional search.

        Args:
            course_identifier: Course code or Canvas ID
            search_term: Filter files by name
            sort: Sort field: name, size, created_at, updated_at, content_type (default: updated_at)
            order: "asc" or "desc" (default: desc)
        """
        valid_sort_fields = {"name", "size", "created_at", "updated_at", "content_type"}
        if sort not in valid_sort_fields:
            return f"Invalid sort field: '{sort}'. Must be one of: {', '.join(sorted(valid_sort_fields))}"

        if order not in ("asc", "desc"):
            return f"Invalid order: '{order}'. Must be 'asc' or 'desc'."

        course_id = await get_course_id(course_identifier)

        params = {
            "per_page": 100,
            "sort": sort,
            "order": order,
        }
        if search_term:
            params["search_term"] = search_term

        files = await fetch_all_paginated_results(
            f"/courses/{course_id}/files",
            params
        )

        if isinstance(files, dict) and "error" in files:
            return f"Error listing files: {files['error']}"

        if not files:
            msg = "No files found"
            if search_term:
                msg += f" matching '{search_term}'"
            return msg

        course_display = await get_course_code(course_id) or course_identifier
        result = f"Files in {course_display}:\n\n"

        for f in files:
            fid = f.get("id", "?")
            name = f.get("display_name") or f.get("filename", "unknown")
            size = format_file_size(f.get("size", 0))
            ctype = f.get("content-type", "unknown")
            status = extract.status_for(
                name,
                student_store.load_material_record(course_id, fid, course_display),
            )
            result += (
                f"  ID: {fid} | {fence_untrusted_inline(name, 'file name')} "
                f"({size}, {ctype}) — {status.label()}\n"
            )

        result += f"\nTotal: {len(files)} file(s)"
        return result


