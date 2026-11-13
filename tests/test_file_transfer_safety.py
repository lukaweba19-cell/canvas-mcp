"""The Canvas token goes to Canvas and nowhere else, on download and upload.

Canvas file URLs redirect to inst-fs/S3, whose pre-signed target carries its own
signature. Forwarding the bearer token there both leaks it and breaks the
request, so these tests use two real loopback origins and check what each one
actually received.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from canvas_mcp.core.client import (
    is_canvas_origin,
    iter_canvas_file_bytes,
    upload_file_to_storage,
)
from canvas_mcp.core.config import reset_config

PAYLOAD = b"%PDF-1.4\nsecret course notes\n"


class _Handler(BaseHTTPRequestHandler):
    def _handle(self) -> None:
        # Drain any request body first: an upload POST that is answered before
        # its body is consumed makes the client see a broken connection.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.requests.append((self.path, dict(self.headers)))  # type: ignore[attr-defined]
        route = self.server.routes.get(self.path)  # type: ignore[attr-defined]
        if route is None:
            self.send_error(404)
            return
        status, headers, body = route
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _handle  # noqa: N815 - stdlib handler naming
    do_POST = _handle  # noqa: N815

    def log_message(self, *args):
        pass


@contextmanager
def _server(routes: dict[str, tuple[int, dict[str, str], bytes]]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.routes = routes  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def canvas_config(monkeypatch):
    """Point the process at a loopback Canvas, as a stdio install would."""

    def configure(base: str) -> None:
        monkeypatch.setenv("CANVAS_API_URL", f"{base}/api/v1")
        monkeypatch.setenv("CANVAS_API_TOKEN", "student-token")
        monkeypatch.setenv("CANVAS_ALLOW_INSECURE_HTTP", "true")
        reset_config()

    yield configure
    reset_config()


async def _collect(url: str) -> bytes:
    chunks = []
    async for chunk in iter_canvas_file_bytes(url):
        chunks.append(chunk)
    return b"".join(chunks)


async def test_download_follows_storage_redirect_without_the_token(canvas_config):
    with _server({"/blob?signature=abc": (200, {}, PAYLOAD)}) as (storage, storage_base):
        with _server(
            {
                "/files/1": (
                    302,
                    {"Location": f"{storage_base}/blob?signature=abc"},
                    b"",
                ),
            }
        ) as (canvas, canvas_base):
            canvas_config(canvas_base)
            assert is_canvas_origin(f"{canvas_base}/files/1")

            body = await _collect(f"{canvas_base}/files/1")

            assert body == PAYLOAD
            canvas_requests = dict(canvas.requests)  # type: ignore[attr-defined]
            assert canvas_requests["/files/1"]["Authorization"] == "Bearer student-token"
            storage_requests = dict(storage.requests)  # type: ignore[attr-defined]
            assert "/blob?signature=abc" in storage_requests
            assert "Authorization" not in storage_requests["/blob?signature=abc"]


async def test_file_url_outside_canvas_is_fetched_unauthenticated(canvas_config):
    with _server({"/direct": (200, {}, PAYLOAD)}) as (storage, storage_base):
        with _server({}) as (_, canvas_base):
            canvas_config(canvas_base)

            body = await _collect(f"{storage_base}/direct")

            assert body == PAYLOAD
            received = dict(storage.requests)["/direct"]  # type: ignore[attr-defined]
            assert "Authorization" not in received


async def test_upload_confirmation_to_another_origin_is_refused(
    canvas_config, tmp_path: Path
):
    upload = tmp_path / "notes.txt"
    upload.write_bytes(b"hello")

    with _server({"/steal": (200, {}, b'{"id": 9}')}) as (storage, storage_base):
        with _server(
            {
                "/upload": (
                    303,
                    {"Location": f"{storage_base}/steal"},
                    b"",
                ),
            }
        ) as (_, canvas_base):
            canvas_config(canvas_base)

            result = await upload_file_to_storage(
                upload_url=f"{canvas_base}/upload",
                upload_params={},
                file_path=str(upload),
                filename="notes.txt",
                content_type="text/plain",
            )

            assert "Refused to confirm" in result["error"]
            # Nothing was sent to the origin the token must never reach.
            assert storage.requests == []  # type: ignore[attr-defined]


async def test_upload_confirmation_back_to_canvas_keeps_the_token(
    canvas_config, tmp_path: Path
):
    upload = tmp_path / "notes.txt"
    upload.write_bytes(b"hello")

    with _server({}) as (_, canvas_base):
        with _server(
            {
                "/upload": (303, {"Location": "/confirmed"}, b""),
                "/confirmed": (200, {}, b'{"id": 7, "success": true}'),
            }
        ) as (canvas, canvas_base):
            canvas_config(canvas_base)

            result = await upload_file_to_storage(
                upload_url=f"{canvas_base}/upload",
                upload_params={},
                file_path=str(upload),
                filename="notes.txt",
                content_type="text/plain",
            )

            assert result["id"] == 7
            confirmed = dict(canvas.requests)["/confirmed"]  # type: ignore[attr-defined]
            assert confirmed["Authorization"] == "Bearer student-token"
