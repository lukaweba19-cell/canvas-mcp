"""The default transport: a real HTTP server serving MCP at /mcp.

The student points their client at ``http://127.0.0.1:8819/mcp``. This starts
the same ASGI app uvicorn serves and completes a real MCP handshake over HTTP,
proving the endpoint is reachable and the tools are listed — no token involved.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def http_server(mcp):
    port = _free_port()
    config = uvicorn.Config(
        mcp.http_app(stateless_http=True),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            httpx.get(base + "/mcp", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


async def test_mcp_initialize_over_http(http_server):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    }

    response = httpx.post(
        http_server + "/mcp",
        json=payload,
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        timeout=30,
    )

    assert response.status_code == 200, response.text
    assert "protocolVersion" in response.text or "serverInfo" in response.text
    # The HTTP endpoint holds no token; a bearer header is never required.
    assert "Authorization" not in response.request.headers


async def test_tools_list_over_http(http_server):
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }

    response = httpx.post(
        http_server + "/mcp",
        json=payload,
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        timeout=30,
    )

    assert response.status_code == 200, response.text
    assert "read_course_material" in response.text
    assert "submit_assignment" in response.text
