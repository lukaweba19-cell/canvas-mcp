#!/usr/bin/env python3
"""
Canvas MCP Server

A Model Context Protocol server for Canvas LMS integration.
Provides educators and students with AI-powered tools for course management,
assignment handling, discussion facilitation, student analytics, and personal
academic tracking.

Supports two transport modes:
- stdio (default): Local process communication, credentials from .env
- streamable-http: HTTP server, per-request token via X-Canvas-Token header;
  the Canvas API URL is pinned by server config (CANVAS_API_URL), not the client.
"""

import argparse
import asyncio
import hmac
import json
import sys
from typing import Any

from fastmcp import FastMCP

from .core.config import get_config, validate_canvas_url_scheme, validate_config
from .core.credentials import (
    RequestCredentials,
    clear_http_request_context,
    set_http_request_active,
    set_request_credentials,
)
from .core.logging import log_error, log_info, log_warning
from .core.tool_results import install_tool_result_contract
from .resources import register_resources_and_prompts
from .tools import (
    register_course_tools,
    register_discovery_tools,
    register_material_tools,
    register_rubric_tools,
    register_self_identity_tools,
    register_shared_assignment_tools,
    register_shared_content_tools,
    register_shared_discussion_tools,
    register_shared_file_tools,
    register_shared_messaging_tools,
    register_shared_module_tools,
    register_student_insight_tools,
    register_student_tools,
    register_student_write_tools,
    register_study_tools,
    register_workspace_tools,
)


async def _send_json_error(send: Any, status: int, message: str) -> None:
    """Emit a minimal ASGI JSON error response (used to fail closed)."""
    body = json.dumps({"error": message}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _access_key_ok(presented: str, allowed: frozenset[str]) -> bool:
    """Constant-time check of a presented access key against the allowed set."""
    if not presented:
        return False
    return any(hmac.compare_digest(presented, key) for key in allowed)


class CanvasCredentialMiddleware:
    """ASGI middleware that extracts the caller's Canvas token from headers.

    For each incoming HTTP request, reads X-Canvas-Token and combines it with
    the server-pinned CANVAS_API_URL so make_canvas_request uses the caller's
    own Canvas token instead of any server .env token.

    Fail-closed semantics:
    - When MCP_ACCESS_KEYS is configured, a missing/invalid X-MCP-Access-Key
      returns HTTP 401 before anything else (the v1 multi-user gate).
    - A missing/blank X-Canvas-Token returns HTTP 401 before the app runs.
    - X-Canvas-URL is ignored (logged); the Canvas API URL is never
      client-controlled, which removes the SSRF surface entirely.
    - The "HTTP request active" marker is set so downstream code refuses to
      fall back to the server's own credentials.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            # Passthrough for lifespan and other non-HTTP scopes
            await self.app(scope, receive, send)
            return

        config = get_config()

        set_http_request_active(True)
        try:
            # Parse headers from ASGI scope (list of [name, value] byte pairs)
            headers = dict(scope.get("headers", []))

            # Access-key gate (when configured): reject before touching credentials.
            allowed_keys = config.mcp_access_keys
            if allowed_keys:
                presented = headers.get(b"x-mcp-access-key", b"").decode("utf-8", errors="ignore").strip()
                if not _access_key_ok(presented, allowed_keys):
                    await _send_json_error(send, 401, "Invalid or missing X-MCP-Access-Key")
                    return

            token = headers.get(b"x-canvas-token", b"").decode("utf-8", errors="ignore").strip()

            if b"x-canvas-url" in headers:
                log_warning("Ignoring X-Canvas-URL header; Canvas API URL is server-pinned")

            if not token:
                await _send_json_error(send, 401, "Missing X-Canvas-Token header")
                return

            canvas_url = config.canvas_api_url.strip()
            if not canvas_url:
                log_error("CANVAS_API_URL is required in HTTP mode but is not configured")
                await _send_json_error(send, 500, "Server Canvas API URL is not configured")
                return

            set_request_credentials(
                RequestCredentials(api_token=token, api_url=canvas_url)
            )
            await self.app(scope, receive, send)
        finally:
            clear_http_request_context()


def create_server() -> FastMCP:
    """Create and configure the Canvas MCP server.

    fastmcp takes host/port at serve time (run()/uvicorn), not on the
    constructor — see _run_http_server.
    """
    config = get_config()
    return FastMCP(name=config.mcp_server_name)


def register_all_tools(mcp: FastMCP) -> None:
    """Register the student tool set."""
    log_info("Registering Canvas student MCP tools...")
    install_tool_result_contract(mcp)

    # Shared reads a student needs to do coursework.
    register_course_tools(mcp)
    register_shared_content_tools(mcp)
    register_shared_assignment_tools(mcp)
    register_shared_discussion_tools(mcp)
    register_shared_module_tools(mcp)
    register_shared_file_tools(mcp)
    register_shared_messaging_tools(mcp)
    register_rubric_tools(mcp)
    register_discovery_tools(mcp)
    register_self_identity_tools(mcp)

    # Student-specific reads, plus the write tools the operator enables.
    register_student_tools(mcp)
    register_student_insight_tools(mcp)
    register_student_write_tools(mcp)

    # Course material extraction (PDF, slides, Word), study guides, and the
    # local workspace for drafts, notes, journal and voice.
    register_material_tools(mcp)
    register_study_tools(mcp)
    register_workspace_tools(mcp)

    register_resources_and_prompts(mcp)
    log_info("Canvas student MCP tools registered successfully!")


async def _validate_token() -> tuple[bool, str]:
    """Validate the Canvas API token by calling /users/self.

    Returns:
        Tuple of (success, message). On success the message contains the
        authenticated user name; on failure it describes the error.
    """
    from .core.client import make_canvas_request

    try:
        response = await make_canvas_request("get", "/users/self")
        if isinstance(response, dict) and "error" in response:
            return (False, f"Token validation failed: {response['error']}")
        user_name = response.get("name", "Unknown") if isinstance(response, dict) else "Unknown"
        return (True, f"Authenticated as: {user_name}")
    except Exception as e:
        return (False, f"Token validation error: {type(e).__name__}: {e}")


def test_connection() -> bool:
    """Test the Canvas API connection."""
    log_info("Testing Canvas API connection...")

    try:
        async def test_api() -> bool:
            ok, message = await _validate_token()
            if ok:
                log_info(f"✓ API connection successful! {message}")
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
        default="stdio",
        help="Transport protocol (default: stdio)"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind HTTP server (default: 0.0.0.0)"
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

    # Admin access-approval commands talk only to the overlay store (Azure, via
    # az login) — not Canvas — so dispatch them before the Canvas-credential /
    # HTTP-mode validation below, which they don't need.
    # HTTP mode: the Canvas URL is server-pinned and per-user tokens arrive via
    # X-Canvas-Token. A server token must NOT be set, or a missing request token
    # could silently fall back to it (mis-attributing actions to the operator).
    if is_http:
        if not config.canvas_api_url:
            log_error("CANVAS_API_URL is required in HTTP mode (the Canvas API URL is server-pinned)")
            sys.exit(1)
        # HTTP mode never calls validate_config() (that path is stdio's .env
        # check), so the cleartext-URL rejection has to be applied here too.
        # It matters more here than in stdio: the URL is server-pinned, so one
        # http:// typo leaks every caller's token, not just the operator's.
        if not validate_canvas_url_scheme():
            sys.exit(1)
        if config.canvas_api_token:
            log_error(
                "CANVAS_API_TOKEN must NOT be set in HTTP mode — clients supply their "
                "own token via the X-Canvas-Token header. Unset it and restart."
            )
            sys.exit(1)
        if not config.mcp_access_keys:
            # Secure-by-default: refuse to start an ungated endpoint unless the
            # operator has explicitly accepted that external auth fronts it.
            # Student education records are FERPA "Sensitive" data (U of I DAT01)
            # and must never be accessed without specific authorization, so a
            # silent fail-open here is a compliance failure, not a warning.
            if config.mcp_allow_unauthenticated:
                log_warning(
                    "HTTP mode has no MCP_ACCESS_KEYS, but MCP_ALLOW_UNAUTHENTICATED=true "
                    "is set — assuming external authentication (e.g. Entra/Easy Auth) "
                    "fronts this endpoint. The app-level key gate is DISABLED."
                )
            else:
                log_error(
                    "HTTP mode requires MCP_ACCESS_KEYS (the app-level auth gate). "
                    "Refusing to start an unauthenticated endpoint that can read and "
                    "write Canvas gradebooks. Set MCP_ACCESS_KEYS, or if an external "
                    "authenticator (Entra/Easy Auth) fronts this endpoint, set "
                    "MCP_ALLOW_UNAUTHENTICATED=true to opt in deliberately."
                )
                sys.exit(1)
    else:
        # stdio mode: .env credentials are required (single-user, env-based auth)
        if not validate_config():
            log_error("Please check your .env file configuration")
            log_error("Use the env.template file as a reference")
            sys.exit(1)

    # Handle special commands
    if args.config:
        print("Canvas MCP Server Configuration:", file=sys.stderr)
        print(f"  Server Name: {config.mcp_server_name}", file=sys.stderr)
        print(f"  Transport: {args.transport}", file=sys.stderr)
        if is_http:
            print(f"  Host: {args.host}", file=sys.stderr)
            print(f"  Port: {args.port}", file=sys.stderr)
        else:
            print(f"  Canvas API URL: {config.canvas_api_url}", file=sys.stderr)
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

    # Initialize audit logging (before any API calls)
    from .core.audit import init_audit_logging
    init_audit_logging()

    # Normal server startup
    if is_http:
        log_info(
            f"Starting Canvas MCP server in HTTP mode on {args.host}:{args.port}"
        )
        log_info("Credentials: per-request via X-Canvas-Token header; Canvas API URL is server-pinned")
    else:
        log_info(f"Starting Canvas MCP server with API URL: {config.canvas_api_url}")

    if config.institution_name:
        log_info(f"Institution: {config.institution_name}")

    # Validate token on startup for stdio mode only
    if not is_http:
        try:
            ok, message = asyncio.run(_validate_token())
            if ok:
                log_info(f"✓ {message}")
            else:
                log_warning(
                    f"Token validation failed: {message}. "
                    "Check your CANVAS_API_TOKEN. Server will start anyway."
                )
        except Exception:
            log_warning(
                "Could not validate token on startup (network may be unavailable). "
                "Server will start anyway."
            )
        finally:
            # asyncio.run() creates and immediately closes its own event loop.
            # Any global httpx client or asyncio semaphore created during token
            # validation is now bound to that closed loop.  Reset them so they
            # are recreated fresh inside the event loop that mcp.run() starts.
            from .core import client as _client_module
            _client_module.http_client = None
            _client_module._http_client_loop_ref = None
            _client_module._request_semaphore = None
            _client_module._semaphore_loop_ref = None

    log_info("Use Ctrl+C to stop the server")

    # Create and configure server
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
        # Cleanup HTTP client resources (stdio mode only — HTTP uses per-request clients)
        if not is_http:
            from .core.client import cleanup_http_client

            try:
                asyncio.run(cleanup_http_client())
            except RuntimeError:
                pass  # Event loop already closed — safe to ignore
        log_info("Server stopped")


def _run_http_server(mcp: FastMCP, host: str, port: int) -> None:
    """Run the MCP server with HTTP transport and credential middleware."""
    import uvicorn

    # fastmcp: http_app() replaces v1 streamable_http_app(); streamable
    # HTTP is the default transport and the endpoint stays mounted at /mcp.
    # CanvasCredentialMiddleware passes lifespan scopes through, so the
    # inner app's lifespan (required by fastmcp) still runs under uvicorn.
    #
    # stateless_http=True: every request is self-contained (credentials arrive
    # per-request via X-Canvas-Token; no tool uses server-initiated session
    # features), so keeping an in-memory session table only creates a failure
    # mode — an App Service recycle drops it, and mcp-remote hangs forever on
    # the resulting stale-session 404 instead of re-initializing (issue #159).
    starlette_app = mcp.http_app(stateless_http=True)
    app = CanvasCredentialMiddleware(starlette_app)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    import anyio

    anyio.run(server.serve)


if __name__ == "__main__":
    main()
