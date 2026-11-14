#!/usr/bin/env python3
"""
Canvas MCP Server

A Model Context Protocol server for Canvas LMS integration.
Provides educators and students with AI-powered tools for course management,
assignment handling, discussion facilitation, student analytics, and personal
academic tracking.

Supports two transport modes:
- streamable-http (default): serves /mcp on 127.0.0.1:8819 so an MCP client can
  simply be pointed at the URL.
- stdio: the MCP client spawns this process as a child.

Authentication is the student's Canvas browser session, captured from Chrome
(see core/session.py and core/chrome_login.py). There is no API token.

Quizzes can always be read (list, details, your own attempts). Taking a quiz
is a separate opt-in, CANVAS_ENABLE_QUIZ_TAKING=false by default; only Classic
Quizzes can be taken, because Canvas exposes no student take API for New
Quizzes. See tools/quizzes.py and env.template.
"""

import argparse
import asyncio
import importlib.resources
import os
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.providers.skills import SkillsDirectoryProvider

from .core.config import get_config, validate_canvas_url_scheme
from .core.logging import log_error, log_info, log_warning
from .core.tool_results import install_tool_result_contract
from .resources import register_resources_and_prompts
from .tools import (
    register_course_tools,
    register_discovery_tools,
    register_material_tools,
    register_quiz_tools,
    register_rubric_tools,
    register_self_identity_tools,
    register_shared_assignment_tools,
    register_shared_content_tools,
    register_shared_discussion_tools,
    register_shared_file_tools,
    register_shared_messaging_tools,
    register_shared_module_tools,
    register_student_insight_tools,
    register_student_rubric_tools,
    register_student_tools,
    register_student_write_tools,
    register_study_tools,
    register_workspace_tools,
)


def skills_root() -> Path | None:
    """Where the shipped skill folders live, or None when the install is broken.

    The skills are packaged inside canvas_mcp (hatch force-include), so a normal
    install always has them; CANVAS_SKILLS_DIR overrides the location for a
    checkout that keeps them elsewhere.
    """
    candidates: list[Path] = []
    configured = os.environ.get("CANVAS_SKILLS_DIR")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(Path(__file__).resolve().parents[2] / "skills")
    candidates.append(Path(str(importlib.resources.files("canvas_mcp"))) / "skills")
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*/SKILL.md")):
            return candidate
    return None


def create_server() -> FastMCP:
    """Create and configure the Canvas MCP server.

    fastmcp takes host/port at serve time (run()/uvicorn), not on the
    constructor — see _run_http_server.
    """
    config = get_config()
    mcp = FastMCP(name=config.mcp_server_name)

    skills = skills_root()
    if skills is None:
        log_error(
            "Canvas skills are missing from this install; the model will not get "
            "its workflow guidance. Reinstall canvas-mcp (`uv sync --reinstall`)."
        )
    else:
        mcp.add_provider(SkillsDirectoryProvider(roots=skills))
    return mcp


def register_all_tools(mcp: FastMCP) -> None:
    """Register the student tool set."""
    log_info("Registering Canvas student MCP tools...")
    install_tool_result_contract(mcp)

    register_course_tools(mcp)
    register_shared_content_tools(mcp)
    register_shared_assignment_tools(mcp)
    register_shared_discussion_tools(mcp)
    register_shared_module_tools(mcp)
    register_shared_file_tools(mcp)
    register_shared_messaging_tools(mcp)
    register_quiz_tools(mcp)
    register_rubric_tools(mcp)
    register_student_rubric_tools(mcp)
    register_discovery_tools(mcp)
    register_self_identity_tools(mcp)

    register_student_tools(mcp)
    register_student_insight_tools(mcp)
    register_student_write_tools(mcp)

    register_material_tools(mcp)
    register_study_tools(mcp)
    register_workspace_tools(mcp)

    register_resources_and_prompts(mcp)
    log_info("Canvas student MCP tools registered successfully!")


def _is_loopback_host(host: str) -> bool:
    """Whether a bind address never leaves this machine."""
    import ipaddress

    host = (host or "").strip().strip("[]").lower()
    if host in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _check_connection() -> tuple[bool, str]:
    """Confirm a Canvas session works by calling /users/self."""
    from .core.client import make_canvas_request
    from .core.session import ensure_session

    try:
        session, reason = await ensure_session()
        if session is None:
            return (False, reason or "No Canvas session available.")
        response = await make_canvas_request("get", "/users/self")
        if isinstance(response, dict) and "error" in response:
            return (False, f"Session check failed: {response['error']}")
        user_name = response.get("name", "Unknown") if isinstance(response, dict) else "Unknown"
        return (True, f"Authenticated as: {user_name}")
    except Exception as e:
        return (False, f"Session check error: {type(e).__name__}: {e}")


def test_connection() -> bool:
    """Test the Canvas API connection."""
    log_info("Testing Canvas API connection...")

    try:
        async def test_api() -> bool:
            ok, message = await _check_connection()
            if ok:
                log_info(f"✓ Canvas connection successful! {message}")
                return True
            else:
                log_error(message)
                return False

        return asyncio.run(test_api())

    except Exception as e:
        log_error("API test failed with exception", exc=e)
        return False


def main() -> None:
    """Main entry point for the Canvas MCP server."""
    parser = argparse.ArgumentParser(
        description="Canvas MCP Server - AI-powered Canvas LMS integration"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test Canvas API connection and exit"
    )
    parser.add_argument(
        "--config",
        action="store_true",
        help="Show current configuration and exit"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="streamable-http",
        help="Transport protocol (default: streamable-http, served at /mcp)"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind HTTP server (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8819,
        help="Port for HTTP server (default: 8819)"
    )
    args = parser.parse_args()
    is_http = args.transport == "streamable-http"

    config = get_config()

    if not config.canvas_api_url:
        log_error("CANVAS_API_URL is required (e.g. https://school.instructure.com/api/v1)")
        log_error("Use the env.template file as a reference")
        sys.exit(1)
    if not validate_canvas_url_scheme():
        sys.exit(1)
    if is_http and not _is_loopback_host(args.host):
        log_error(
            "HTTP transport refuses to bind a non-loopback address: the server holds "
            "your Canvas browser session, so exposing it would let anyone on the "
            "network act as you. Use --host 127.0.0.1 (or a local tunnel/proxy that "
            "adds its own authentication)."
        )
        sys.exit(1)

    if args.config:
        print("Canvas MCP Server Configuration:", file=sys.stderr)
        print(f"  Server Name: {config.mcp_server_name}", file=sys.stderr)
        print(f"  Transport: {args.transport}", file=sys.stderr)
        print(f"  Canvas API URL: {config.canvas_api_url}", file=sys.stderr)
        if is_http:
            print(f"  Host: {args.host}", file=sys.stderr)
            print(f"  Port: {args.port}", file=sys.stderr)
        from .core.session import load_session, session_path

        stored = load_session()
        print(f"  Session file: {session_path()}", file=sys.stderr)
        print(
            f"  Session: {'stored' if stored is not None else 'none (will open Chrome on first use)'}",
            file=sys.stderr,
        )
        print(f"  Debug Mode: {config.debug}", file=sys.stderr)
        print(f"  API Timeout: {config.api_timeout}s", file=sys.stderr)
        print(f"  Cache TTL: {config.cache_ttl}s", file=sys.stderr)
        if config.institution_name:
            print(f"  Institution: {config.institution_name}", file=sys.stderr)
        sys.exit(0)

    if args.test:
        if test_connection():
            log_info("All tests passed!")
            sys.exit(0)
        else:
            log_error("Connection test failed!")
            sys.exit(1)

    from .core.audit import init_audit_logging
    init_audit_logging()

    if is_http:
        log_info(
            f"Starting Canvas MCP server in HTTP mode on {args.host}:{args.port}"
        )
        log_info(
            "Authentication: captured Canvas browser session; Canvas API URL is "
            "server-pinned"
        )
    else:
        log_info(f"Starting Canvas MCP server with API URL: {config.canvas_api_url}")

    if config.institution_name:
        log_info(f"Institution: {config.institution_name}")

    try:
        from .core.session import load_session

        stored = load_session()
        if stored is not None:
            log_info("Canvas session: stored (captured from Chrome)")
        else:
            log_info(
                "Canvas session: none yet. The first Canvas tool call opens a Chrome "
                "window so you can sign in."
            )
    except Exception:
        log_warning("Could not read the stored Canvas session; will retry on first use")

    log_info("Use Ctrl+C to stop the server")

    mcp = create_server()
    register_all_tools(mcp)

    try:
        if is_http:
            _run_http_server(mcp, host=args.host, port=args.port)
        else:
            mcp.run()
    except KeyboardInterrupt:
        log_info("\nShutting down server...")
    except Exception as e:
        log_error("Server error", exc=e)
        sys.exit(1)
    finally:
        log_info("Server stopped")


def _run_http_server(mcp: FastMCP, host: str, port: int) -> None:
    """Serve the MCP endpoint over streamable HTTP.

    The server owns the single student session, so requests need no per-caller
    credential header; the endpoint is loopback-only by default and is refused
    on any other bind address in ``main``.
    """
    import anyio
    import uvicorn

    app = mcp.http_app(stateless_http=True)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
    )
    anyio.run(uvicorn.Server(config).serve)


if __name__ == "__main__":
    main()
