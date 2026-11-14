"""Browser-session authentication: cookies, CSRF, XSSI, login detection.

There is no API token in this build. These tests pin the pieces that make a
captured Canvas web session work as the credential, and that keep it scoped to
Canvas.
"""

from __future__ import annotations

import json
import os
import stat

import httpx
import pytest

from canvas_mcp.core import client as client_module
from canvas_mcp.core import session as session_module
from canvas_mcp.core.chrome_login import launch_arguments
from canvas_mcp.core.config import reset_config
from canvas_mcp.core.session import (
    domain_matches,
    filter_cookies,
    reset_session_cache,
    save_session,
    strip_xssi,
)

CANVAS = "https://canvas.example.edu/api/v1"


@pytest.fixture
def canvas_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CANVAS_API_URL", CANVAS)
    monkeypatch.setenv("CANVAS_SESSION_FILE", str(tmp_path / "session.json"))
    monkeypatch.setenv("CANVAS_AUTH_NO_BROWSER", "1")
    reset_config()
    reset_session_cache()
    yield tmp_path / "session.json"
    reset_session_cache()
    reset_config()


def test_cookie_filter_keeps_only_the_canvas_host():
    cookies = [
        {"domain": ".canvas.example.edu", "name": "_canvas_session", "value": "abc"},
        {"domain": "canvas.example.edu", "name": "_csrf_token", "value": "csrf"},
        {"domain": ".google.com", "name": "SID", "value": "should-never-be-stored"},
        {"domain": ".example.edu", "name": "sso", "value": "other-school"},
    ]

    kept = filter_cookies(cookies, "canvas.example.edu")

    assert kept == {"_canvas_session": "abc", "_csrf_token": "csrf", "sso": "other-school"}
    assert "SID" not in kept


def test_cookie_domain_matching_does_not_accept_lookalikes():
    assert domain_matches(".example.edu", "canvas.example.edu")
    assert domain_matches("example.edu", "example.edu")
    assert not domain_matches("example.edu", "notexample.edu")


def test_xssi_guard_is_stripped_before_json_parsing():
    assert strip_xssi('while(1);{"id": 7}') == '{"id": 7}'
    assert strip_xssi('{}&&{"id": 7}') == '{"id": 7}'
    assert strip_xssi('{"id": 7}') == '{"id": 7}'


def test_session_is_written_owner_only(canvas_session):
    saved = save_session({"_canvas_session": "abc"}, "csrf-token")

    assert canvas_session.is_file()
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(canvas_session).st_mode)
        assert mode == 0o600, oct(mode)
    assert saved.cookies["_canvas_session"] == "abc"
    assert canvas_session.read_text(encoding="utf-8").count("abc") == 1


def test_session_client_carries_cookie_and_csrf(canvas_session):
    save_session({"_canvas_session": "abc"}, "csrf-token")

    client = client_module.new_session_client()

    assert dict(client.cookies)["_canvas_session"] == "abc"
    assert client.headers["X-CSRF-Token"] == "csrf-token"
    assert "Authorization" not in client.headers


def test_session_for_another_canvas_host_is_ignored(canvas_session):
    canvas_session.write_text(
        json.dumps(
            {
                "base_url": "https://other.instructure.com/api/v1",
                "cookies": {"_canvas_session": "stolen"},
                "csrf": "",
            }
        ),
        encoding="utf-8",
    )
    reset_session_cache()

    assert session_module.load_session(force=True) is None


async def test_xssi_prefixed_response_is_parsed(canvas_session, monkeypatch):
    async def fake_ensure(force: bool = False):
        return (object(), "")

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text='while(1);{"id": 7, "name": "Student"}',
            headers={"content-type": "application/json"},
        )
    )
    monkeypatch.setattr(client_module, "ensure_session", fake_ensure)
    monkeypatch.setattr(
        client_module,
        "new_session_client",
        lambda: httpx.AsyncClient(transport=transport),
    )

    result = await client_module.make_canvas_request("get", "/users/self")

    assert result == {"id": 7, "name": "Student"}


async def test_login_redirect_is_reported_as_expired_session(canvas_session, monkeypatch):
    save_session({"_canvas_session": "expired"}, "csrf")

    async def fake_ensure(force: bool = False):
        return (object(), "")

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            302,
            headers={"location": "https://canvas.example.edu/login"},
        )
    )
    monkeypatch.setattr(client_module, "ensure_session", fake_ensure)
    monkeypatch.setattr(
        client_module,
        "new_session_client",
        lambda: httpx.AsyncClient(transport=transport),
    )

    result = await client_module.make_canvas_request("get", "/users/self")

    assert "session expired" in result["error"].lower()
    assert "Chrome" in result["error"]
    assert session_module.load_session(force=True) is None


def test_chrome_launch_uses_a_dedicated_profile_and_random_port(tmp_path):
    args = launch_arguments(
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        tmp_path / "chrome-profile",
        "https://canvas.example.edu/login",
    )

    assert "--remote-debugging-port=0" in args
    assert any(arg.startswith("--user-data-dir=") for arg in args)
    assert any(str(tmp_path / "chrome-profile") in arg for arg in args)


def test_http_transport_is_loopback_only():
    from canvas_mcp.server import _is_loopback_host

    assert _is_loopback_host("127.0.0.1")
    assert _is_loopback_host("localhost")
    assert not _is_loopback_host("0.0.0.0")
    assert not _is_loopback_host("192.168.1.10")


async def test_http_app_exposes_the_mcp_endpoint(mcp):
    app = mcp.http_app(stateless_http=True)

    paths = {getattr(route, "path", "") for route in app.routes}

    assert "/mcp" in paths, sorted(paths)
