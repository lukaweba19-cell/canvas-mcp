"""Shared fixtures: isolated workspace, a fake Canvas, and the real MCP entry point.

Every test runs against a fresh ``CANVAS_STUDENT_DIR`` under pytest's tmp_path and
a cleared course cache, so a second run behaves exactly like the first and no
test can see another's files.
"""

from __future__ import annotations

import importlib
import json
from copy import deepcopy
from typing import Any

import pytest
from fastmcp import FastMCP

import canvas_mcp.server as server_module
from canvas_mcp.core import cache, ocr
from canvas_mcp.core.config import reset_config


class FakeCanvas:
    """A route table in place of the Canvas API.

    Tests register exact (method, endpoint-prefix) responses and can then assert
    on what was actually requested — which is how cache reuse is proven: if a
    call was served from disk, the fake sees no request at all.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.payloads: list[tuple[str, str, Any]] = []
        self._routes: list[tuple[str, str, Any]] = []

    def route(self, method: str, prefix: str, payload: Any) -> FakeCanvas:
        self._routes = [
            existing
            for existing in self._routes
            if not (existing[0] == method.upper() and existing[1] == prefix)
        ]
        self._routes.append((method.upper(), prefix, payload))
        return self

    def calls_to(self, method: str, prefix: str = "") -> list[tuple[str, str]]:
        return [
            call
            for call in self.calls
            if call[0] == method.upper() and call[1].startswith(prefix)
        ]

    async def request(
        self, method: str, endpoint: str, params: Any = None, **kwargs: Any
    ) -> Any:
        self.calls.append((method.upper(), endpoint))
        self.payloads.append((method.upper(), endpoint, kwargs.get("data")))
        for route_method, prefix, payload in self._routes:
            if route_method == method.upper() and endpoint == prefix:
                return deepcopy(payload)
        for route_method, prefix, payload in self._routes:
            if route_method == method.upper() and endpoint.startswith(prefix):
                return deepcopy(payload)
        return {"error": f"no fake route for {method.upper()} {endpoint}"}

    async def paginated(self, endpoint: str, params: Any = None, **kwargs: Any) -> Any:
        return await self.request("get", endpoint, params)


_CANVAS_MODULES = (
    "canvas_mcp.core.cache",
    "canvas_mcp.tools.materials",
    "canvas_mcp.tools.student_write",
    "canvas_mcp.tools.student_insight",
    "canvas_mcp.tools.workspace",
    "canvas_mcp.tools.rubrics",
    "canvas_mcp.tools.files",
)


@pytest.fixture
def blank_ocr(monkeypatch):
    """Built-in OCR that runs but finds no text (blank or unusable images)."""
    from helpers import BlankOcr

    from canvas_mcp.core import ocr

    monkeypatch.setattr(ocr, "backend", lambda: BlankOcr())
    yield


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    """Fresh student directory, fresh caches, no confirmation state."""
    monkeypatch.setenv("CANVAS_STUDENT_DIR", str(tmp_path / "student"))
    monkeypatch.setenv("CANVAS_API_URL", "https://canvas.example.edu/api/v1")
    session_file = tmp_path / "session.json"
    session_file.write_text(
        json.dumps(
            {
                "base_url": "https://canvas.example.edu/api/v1",
                "cookies": {
                    "_canvas_session": "test-session",
                    "_csrf_token": "test-csrf",
                },
                "csrf": "test-csrf",
                "saved_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CANVAS_SESSION_FILE", str(session_file))
    monkeypatch.setenv("CANVAS_AUTH_NO_BROWSER", "1")
    from canvas_mcp.core.session import reset_session_cache

    reset_session_cache()
    reset_config()
    cache.course_code_to_id_cache.clear()
    cache.id_to_course_code_cache.clear()
    ocr.reset_detection_cache()

    from canvas_mcp.tools.student_write import reset_pending_confirmations

    reset_pending_confirmations()

    yield tmp_path

    reset_pending_confirmations()
    cache.course_code_to_id_cache.clear()
    cache.id_to_course_code_cache.clear()
    ocr.reset_detection_cache()
    from canvas_mcp.core.session import reset_session_cache

    reset_session_cache()
    reset_config()


@pytest.fixture
def canvas(monkeypatch) -> FakeCanvas:
    """Route every Canvas call in every tool module through one fake."""
    fake = FakeCanvas()
    fake.route("get", "/courses", [])
    for name in _CANVAS_MODULES:
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "make_canvas_request", fake.request, raising=False)
        if hasattr(module, "fetch_all_paginated_results"):
            monkeypatch.setattr(
                module, "fetch_all_paginated_results", fake.paginated, raising=False
            )
    return fake


@pytest.fixture
def policy_allow(monkeypatch):
    """Let writes through: course policy itself is not what these tests cover."""

    async def allow(course_id: str, tool_name: str) -> tuple[bool, str]:
        return True, ""

    module = importlib.import_module("canvas_mcp.tools.student_write")
    monkeypatch.setattr(module, "check_student_write_allowed", allow)
    return allow


@pytest.fixture
def mcp() -> FastMCP:
    """The real server registry, built the same way ``main`` builds it."""
    instance = FastMCP(name="canvas-test")
    server_module.register_all_tools(instance)
    return instance
