"""Capture a Canvas web session from the student's own Chrome.

Canvas disables personal access tokens for many student accounts, and Chrome
136+ refuses to open a debug port on the default profile. The supported path is
therefore a small dedicated Chrome profile:

1. attach if that profile is already running with a debug port, otherwise
2. launch the installed Chrome with ``--remote-debugging-port=0`` and a
   dedicated ``--user-data-dir`` at the Canvas login page,
3. read cookies over CDP, keep only Canvas's, store them, and close the window.

The student signs in themselves (SSO/MFA); no password ever reaches this code.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .logging import log_debug, log_warning

DEVTOOLS_PORT_FILE = "DevToolsActivePort"


# ---------------------------------------------------------------------------
# Chrome discovery
# ---------------------------------------------------------------------------

def _windows_candidates() -> list[str]:
    paths: list[str] = []
    try:
        import winreg  # type: ignore[import-not-found]

        for root in (
            winreg.HKEY_CURRENT_USER,
            winreg.HKEY_LOCAL_MACHINE,
        ):
            for view in (0, getattr(winreg, "KEY_WOW64_64KEY", 0)):
                try:
                    with winreg.OpenKey(
                        root,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                        0,
                        winreg.KEY_READ | view,
                    ) as key:
                        value, _ = winreg.QueryValueEx(key, "")
                        if value:
                            paths.append(str(value))
                except OSError:
                    continue
    except ImportError:
        pass
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            paths.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    return paths


def chrome_candidates() -> list[str]:
    """Every plausible Chrome executable for this OS, in preference order."""
    from .config import get_config

    candidates: list[str] = []
    configured = (get_config().canvas_chrome_path or "").strip()
    if configured:
        candidates.append(configured)
    found = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which(
        "google-chrome-stable"
    )
    if found:
        candidates.append(found)
    if sys.platform.startswith("win"):
        candidates.extend(_windows_candidates())
    elif sys.platform == "darwin":
        candidates.append(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )
    else:
        candidates.extend(
            [
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
            ]
        )
    # De-duplicate while keeping order.
    seen: set[str] = set()
    ordered: list[str] = []
    for path in candidates:
        if path and path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def find_chrome() -> str | None:
    for candidate in chrome_candidates():
        if Path(candidate).is_file():
            return candidate
    return None


def chrome_profile_dir() -> Path:
    from .config import get_config

    configured = (get_config().canvas_chrome_profile or "").strip()
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".canvas-mcp" / "chrome-profile"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def launch_arguments(chrome: str, profile: Path, login_url: str) -> list[str]:
    """Flags for the dedicated login profile.

    ``--user-data-dir`` is mandatory: without a non-default profile Chrome 136+
    ignores the debugging switch entirely.
    """
    return [
        chrome,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        login_url,
    ]


# ---------------------------------------------------------------------------
# Cross-process guard: only one login window at a time
# ---------------------------------------------------------------------------

def _lock_path() -> Path:
    from .session import session_path

    return session_path().with_suffix(".login.lock")


@contextmanager
def _login_guard(timeout: float) -> Iterator[bool]:
    """Yield True when this process owns the login, False when another does."""
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(1.0, timeout)
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = 0
            if age > max(60.0, timeout):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(0.5)
    try:
        yield True
    finally:
        try:
            path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# CDP
# ---------------------------------------------------------------------------

def _devtools_endpoint(profile: Path) -> tuple[int, str] | None:
    """The running profile's (port, websocket path), if it exposes one."""
    try:
        lines = (profile / DEVTOOLS_PORT_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    try:
        return int(lines[0].strip()), lines[1].strip()
    except ValueError:
        return None


def _wait_for_endpoint(profile: Path, timeout: float) -> tuple[int, str] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        endpoint = _devtools_endpoint(profile)
        if endpoint is not None:
            return endpoint
        time.sleep(0.25)
    return None


async def _read_cookies(port: int, path: str, timeout: float) -> list[dict]:
    """Ask the running Chrome for its cookies over CDP."""
    import websockets

    url = f"ws://127.0.0.1:{port}{path}"
    async with websockets.connect(url, open_timeout=timeout, max_size=None) as ws:
        await ws.send(
            json.dumps({"id": 1, "method": "Storage.getCookies", "params": {}})
        )
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            message = json.loads(raw)
            if message.get("id") == 1:
                result = message.get("result") or {}
                cookies = result.get("cookies")
                return cookies if isinstance(cookies, list) else []


async def _close_browser(port: int, path: str) -> None:
    import websockets

    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{port}{path}", open_timeout=5
        ) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            await asyncio.wait_for(ws.recv(), timeout=5)
    except Exception:
        pass


async def _capture_loop(
    profile: Path, host: str, timeout: float
) -> tuple[dict[str, str], tuple[int, str]] | None:
    """Poll a running Chrome until the Canvas session cookie appears."""
    endpoint = _devtools_endpoint(profile)
    if endpoint is None:
        return None
    port, path = endpoint
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            cookies = await _read_cookies(port, path, timeout=10)
        except Exception:
            await asyncio.sleep(1)
            continue
        from .session import filter_cookies

        kept = filter_cookies(cookies, host)
        if "_canvas_session" in kept:
            return kept, endpoint
        await asyncio.sleep(1)
    return None


def _launch(chrome: str, profile: Path, login_url: str) -> subprocess.Popen:
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(launch_arguments(chrome, profile, login_url), **kwargs)


async def capture_session(force: bool = False) -> tuple[object | None, str]:
    """Obtain a Canvas session, launching Chrome only when needed."""
    from .config import get_config
    from .session import (
        _canvas_host,
        load_session,
        refresh_csrf,
        save_session,
    )

    config = get_config()
    host = _canvas_host()
    if not host:
        return None, "CANVAS_API_URL is not configured, so there is no Canvas to log in to."

    existing = load_session(force=True)
    if existing is not None and existing.is_usable() and not force:
        return existing, ""

    if config.canvas_auth_no_browser:
        return (
            None,
            "No Canvas session is stored and browser login is disabled "
            "(CANVAS_AUTH_NO_BROWSER=1). Run once without that variable, sign in, "
            "then restart.",
        )

    timeout = max(15, int(config.canvas_login_timeout))
    with _login_guard(timeout) as owns:
        if not owns:
            # Another process is showing the window; wait for its result.
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                stored = load_session(force=True)
                if stored is not None and stored.is_usable():
                    return stored, ""
                await asyncio.sleep(1)
            return (
                None,
                "Another Canvas login is already in progress and did not finish "
                "in time. Finish signing in in the open Chrome window, then retry.",
            )

        profile = chrome_profile_dir()
        chrome = find_chrome()
        running = await _capture_loop(profile, host, timeout=3)
        if running is not None:
            cookies, endpoint = running
        else:
            if chrome is None:
                return (
                    None,
                    "No Canvas session is stored and Google Chrome could not be "
                    "found. Install Chrome, or set CANVAS_CHROME_PATH to its "
                    "executable.",
                )
            login_url = f"https://{host}/login"
            log_debug("Opening Chrome to capture a Canvas session", url=login_url)
            _launch(chrome, profile, login_url)
            endpoint = _wait_for_endpoint(profile, timeout=30)
            if endpoint is None:
                return (
                    None,
                    "Chrome was launched but never exposed a debugging port. "
                    "Close the login window and try again; if it persists, check "
                    f"that {profile} is writable.",
                )
            captured = await _capture_loop(profile, host, timeout=timeout)
            if captured is None:
                # Leave the window open: the student may still be signing in.
                return (
                    None,
                    "The Chrome login window is open but no Canvas session was "
                    "captured yet. Finish signing in, then retry the tool.",
                )
            cookies, endpoint = captured

        csrf = cookies.get("_csrf_token", "")
        session = save_session(cookies, csrf)
        if not session.csrf:
            refreshed = await refresh_csrf(session)
            if refreshed:
                session = save_session(session.cookies, refreshed)
        if endpoint is not None:
            await _close_browser(*endpoint)
        log_debug("Canvas session captured", cookies=sorted(session.cookies))
        return session, ""


def logout() -> None:
    """Forget the stored session (does not sign the browser out)."""
    from .session import clear_session

    clear_session()
    log_warning("Stored Canvas session cleared")
