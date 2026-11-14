"""Canvas web-session authentication.

This server does not use personal access tokens. Canvas's own web UI calls the
same ``/api/v1`` REST API with a session cookie, so the server does the same:
the student signs in once in a Chrome window, the cookies are captured and
stored locally, and every request carries them.

What lives here:

- the on-disk session (``~/.canvas-mcp/session.json``, mode 0600),
- Canvas-domain cookie filtering (nothing from another site is ever written),
- the CSRF token Canvas requires on writes,
- the XSSI guard Canvas puts in front of cookie-authenticated JSON.

Cookie values are never logged and never included in an error message.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from .logging import log_debug, log_warning

XSSI_PREFIXES = ("while(1);", "while(1)", "{}&&", ")]}',")

_CSRF_META = re.compile(
    r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CanvasSession:
    """A captured Canvas web session."""

    base_url: str
    cookies: dict[str, str]
    csrf: str
    saved_at: str

    @property
    def host(self) -> str:
        return (urlparse(self.base_url).hostname or "").lower()

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self.cookies.items())

    def is_usable(self) -> bool:
        """A session needs the session cookie; the CSRF token can be refetched."""
        return bool(self.host) and bool(self.cookies)


def session_path() -> Path:
    """Where the captured session is stored."""
    from .config import get_config

    configured = (get_config().canvas_session_file or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".canvas-mcp" / "session.json"


def _canvas_host() -> str:
    from .config import get_config

    return (urlparse(get_config().canvas_api_url).hostname or "").lower()


def domain_matches(domain: str, host: str) -> bool:
    """Whether a cookie domain belongs to ``host``.

    Canvas sets cookies on the bare host, sometimes with a leading dot. A
    cookie for ``example.edu`` must not match ``notexample.edu``.
    """
    domain = domain.lstrip(".").lower()
    host = host.lower()
    if not domain or not host:
        return False
    return host == domain or host.endswith("." + domain)


def filter_cookies(cookies: list[dict[str, object]], host: str) -> dict[str, str]:
    """Keep only cookies for the Canvas host, newest value per name.

    Chrome returns cookies for every site in the profile; writing any of them
    to disk would be a credential leak. The caller passes the raw CDP list and
    gets back a plain name->value map for Canvas alone.
    """
    kept: dict[str, str] = {}
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        name = str(cookie.get("name") or "")
        value = cookie.get("value")
        if not name or value is None or not domain_matches(domain, host):
            continue
        kept[name] = str(value)
    return kept


def strip_xssi(text: str) -> str:
    """Remove Canvas's XSSI guard from a response body."""
    stripped = text.lstrip("\ufeff \t\r\n")
    for prefix in XSSI_PREFIXES:
        if stripped.startswith(prefix):
            return stripped[len(prefix):].lstrip()
    return stripped


def looks_like_login(body: str) -> bool:
    """Whether a response body is Canvas's login page rather than JSON."""
    lowered = body[:2000].lower()
    return "<html" in lowered and (
        "login" in lowered or "sign in" in lowered or "sso" in lowered
    )


def extract_csrf_from_html(html: str) -> str:
    match = _CSRF_META.search(html)
    return match.group(1) if match else ""



_cached: CanvasSession | None = None


def reset_session_cache() -> None:
    """Forget the in-memory session (tests and explicit logout)."""
    global _cached
    _cached = None


def load_session(force: bool = False) -> CanvasSession | None:
    """Read the stored session, or None when there is none/invalid."""
    global _cached
    if _cached is not None and not force:
        return _cached
    path = session_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _cached = None
        return None
    if not isinstance(raw, dict):
        _cached = None
        return None
    cookies = raw.get("cookies")
    if not isinstance(cookies, dict) or not cookies:
        _cached = None
        return None
    session = CanvasSession(
        base_url=str(raw.get("base_url") or ""),
        cookies={str(k): str(v) for k, v in cookies.items()},
        csrf=str(raw.get("csrf") or ""),
        saved_at=str(raw.get("saved_at") or ""),
    )
    if session.host and _canvas_host() and session.host != _canvas_host():
        log_warning(
            "Stored Canvas session belongs to a different host; ignoring it",
            session_host=session.host,
            canvas_host=_canvas_host(),
        )
        _cached = None
        return None
    _cached = session
    return session


def save_session(cookies: dict[str, str], csrf: str = "") -> CanvasSession:
    """Persist a session with owner-only permissions."""
    from .config import get_config

    session = CanvasSession(
        base_url=get_config().canvas_api_url,
        cookies=dict(cookies),
        csrf=csrf,
        saved_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "base_url": session.base_url,
                "cookies": session.cookies,
                "csrf": session.csrf,
                "saved_at": session.saved_at,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    global _cached
    _cached = session
    log_debug("Stored Canvas browser session", cookie_names=sorted(session.cookies))
    return session


def clear_session() -> None:
    """Remove the stored session (logout / revoked cookies)."""
    global _cached
    _cached = None
    try:
        session_path().unlink()
    except OSError:
        pass


async def refresh_csrf(session: CanvasSession) -> str:
    """Fetch a CSRF token from Canvas using the session cookies."""
    import httpx

    if not session.cookies:
        return ""
    try:
        async with httpx.AsyncClient(
            cookies=session.cookies,
            timeout=15.0,
            follow_redirects=True,
        ) as client:
            response = await client.get(f"https://{session.host}/")
            if response.status_code == 200:
                return extract_csrf_from_html(response.text)
    except Exception:
        return ""
    return ""


async def ensure_session(force: bool = False) -> tuple[CanvasSession | None, str]:
    """Return a usable session, capturing one from Chrome when necessary.

    Returns ``(session, reason)``. ``reason`` is a human-readable explanation
    of every route that was tried when no session could be obtained.
    """
    if not force:
        existing = load_session()
        if existing is not None and existing.is_usable():
            return existing, ""

    from . import chrome_login

    return await chrome_login.capture_session(force=force)
