"""HTTP client and Canvas API utilities."""

import asyncio
import json
import re
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any, Final, Literal, cast
from urllib.parse import urlencode

import httpx

from .anonymization import anonymize_response_data, scrub_identity
from .logging import log_debug, log_error, log_warning, sanitize_url
from .session import (
    clear_session,
    ensure_session,
    load_session,
    looks_like_login,
    strip_xssi,
)
from .write_outcome import NO_WRITE_STATUSES, RequestFailure, WriteOutcome

MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2

DEFAULT_PAGE_SIZE = 100
MAX_PAGINATION_PAGES = 10000
API_ROOT_REST: Final = "rest"
API_ROOT_QUIZ: Final = "quiz"

SESSION_EXPIRED_MESSAGE = (
    "Canvas session expired or was signed out. The next Canvas tool call will "
    "open the Chrome login window again so you can sign in."
)


class CanvasSessionExpired(RuntimeError):
    """The stored Canvas session was rejected; a fresh login is required."""

    def __init__(self) -> None:
        super().__init__(SESSION_EXPIRED_MESSAGE)


def _session_headers() -> dict[str, str]:
    """Canvas session headers: CSRF for writes plus a stable User-Agent.

    There is deliberately no Authorization header: this server authenticates
    with the student's browser session cookie, never a bearer token.
    """
    from .. import __version__

    headers = {
        "User-Agent": f"canvas-mcp/{__version__} (https://github.com/vishalsachdev/canvas-mcp)",
        "Accept": "application/json",
    }
    session = load_session()
    if session is not None and session.csrf:
        headers["X-CSRF-Token"] = session.csrf
    return headers


def _is_login_response(response: httpx.Response) -> bool:
    """Whether Canvas answered with a login page instead of API data."""
    if response.is_redirect:
        location = response.headers.get("location", "").lower()
        return any(token in location for token in ("/login", "saml", "oauth", "auth"))
    if "text/html" in response.headers.get("content-type", "").lower():
        return looks_like_login(response.text)
    return False


def _response_json(response: httpx.Response) -> Any:
    """Parse a Canvas response, removing the XSSI guard cookie auth adds."""
    text = strip_xssi(response.text)
    if not text.strip():
        return {}
    return json.loads(text)

def _resolve_canvas_api_root(base_api_url: str, api_root: Literal["rest", "quiz"]) -> str:
    """Resolve a configured ``…/api/v<N>`` base URL to a selected Canvas API root.

    ``rest`` keeps the configured URL unchanged. ``quiz`` rewrites only the
    trailing API version segment to ``/api/quiz/v1`` while preserving any
    institution prefix (e.g. ``/lms``). This is an explicit call-site opt-in;
    endpoint strings never select a base path implicitly.
    """
    if api_root == API_ROOT_REST:
        return base_api_url

    match = re.search(r"/api/v\d+$", base_api_url)
    if not match:
        raise ValueError(
            "Invalid Canvas API base URL for quiz root resolution: expected trailing /api/v<N>"
        )

    return f"{base_api_url[:match.start()]}/api/quiz/v1"


_request_semaphore: asyncio.Semaphore | None = None
_semaphore_loop_ref: "weakref.ref[asyncio.AbstractEventLoop] | None" = None


def _get_request_semaphore() -> asyncio.Semaphore:
    """Get or create the concurrency semaphore from config.

    Recreates the semaphore when the running event loop has changed (e.g. after
    asyncio.run() closes one loop and mcp.run() starts a new one) to avoid
    "Event loop is closed" errors on asyncio synchronization primitives.

    A weakref to the creating loop is stored so that when the old loop is
    garbage-collected (as asyncio.run() does), the weakref goes dead and we
    detect the loop change reliably without relying on object id() reuse.
    """
    global _request_semaphore, _semaphore_loop_ref
    try:
        current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    stored_loop = _semaphore_loop_ref() if _semaphore_loop_ref is not None else None

    if _request_semaphore is not None and current_loop is not None and stored_loop is not current_loop:
        _request_semaphore = None
        _semaphore_loop_ref = None

    if _request_semaphore is None:
        from .config import get_config
        _request_semaphore = asyncio.Semaphore(get_config().max_concurrent_requests)
        _semaphore_loop_ref = weakref.ref(current_loop) if current_loop is not None else None
    return _request_semaphore


def _path_segments(endpoint: str) -> list[str]:
    """Lower-cased, query-stripped path segments of a Canvas API endpoint."""
    path = endpoint.lower().split('?', 1)[0]
    return [seg for seg in path.split('/') if seg]


def _is_route_segment(segments: list[str], index: int) -> bool:
    """Whether segments[index] is a route keyword rather than a user slug.

    A segment directly after 'pages' is a user-controlled page slug — e.g. a
    page named "users" at /courses/{id}/pages/users — and must never be treated
    as a route keyword.
    """
    return not (index > 0 and segments[index - 1] == 'pages')


def _has_route_segment(segments: list[str], names: set[str]) -> bool:
    """Whether any route (non-slug) segment matches one of `names`."""
    return any(
        seg in names and _is_route_segment(segments, i)
        for i, seg in enumerate(segments)
    )


def _self_submission_indices(segments: list[str]) -> set[int]:
    """Indices of 'submissions' route segments followed by the literal 'self'.

    Anchored on the literal 'self' segment IMMEDIATELY after a 'submissions'
    route segment. A looser match (e.g. 'self' anywhere in the path) would
    recreate the #164 bypass class, where a broad short-circuit silently
    disabled anonymization for unrelated student-data endpoints.
    """
    return {
        i
        for i, seg in enumerate(segments)
        if seg == 'submissions'
        and _is_route_segment(segments, i)
        and i + 1 < len(segments)
        and segments[i + 1] == 'self'
    }


_SELF_ONLY_ENDPOINTS = frozenset({
    "users/self",
    "users/self/profile",
})


def _is_self_only_endpoint(segments: list[str]) -> bool:
    """Whether the path is EXACTLY one of the caller-only endpoints."""
    return "/".join(segments) in _SELF_ONLY_ENDPOINTS


def _determine_data_type(endpoint: str) -> str:
    """Determine the type of data based on the API endpoint.

    Segment-aware (query string stripped, page slugs excluded) so that a
    substring match on a user-controlled slug cannot mis-route a response into
    the wrong typed handler.
    """
    segments = _path_segments(endpoint)

    if _has_route_segment(segments, {'users'}):
        return 'users'
    if _has_route_segment(segments, {'discussion_topics', 'discussion_entries'}):
        return 'discussions'
    if _has_route_segment(segments, {'submissions'}):
        return 'submissions'
    if _has_route_segment(segments, {'assignments'}):
        return 'assignments'
    if _has_route_segment(segments, {'enrollments'}):
        return 'users'
    return 'general'



ANONYMIZE_NONE = "none"

ANONYMIZE_FULL = "full"

ANONYMIZE_IDENTITY = "identity"

ANONYMIZE_FREE_TEXT = "free_text"


def _endpoint_anonymization_mode(endpoint: str) -> str:
    """Which anonymization tier applies to `endpoint`.

    Sensitive-data checks MUST run before any safe-endpoint reasoning: almost
    every call here is scoped /courses/{id}/..., so a '/courses' short-circuit
    checked first silently disables anonymization for the whole codebase
    (issue #164). Anything not matched defaults to ANONYMIZE_NONE.

    The tiers are ordered most-protective-first, so a path that matches several
    families (e.g. /courses/1/pages plus a /users segment) gets the strongest
    treatment rather than the narrowest.

    Partially gated (issue #179) — matched, but NOT at the "full" tier:
    - /conversations, /conversations/{id} -> ANONYMIZE_FREE_TEXT. Previously
      ungated entirely: a live call returned 97 records with student email
      addresses inlined in `last_message`/`last_authored_message` plus
      participant pronouns and avatars. But this is the caller's OWN inbox, so
      the participants are their own correspondents, not third parties whose
      records they are browsing — pseudonymising `participants[].name` would
      make "who emailed me?" unanswerable while protecting nobody.
    - /pages, /courses/{id}/pages/{slug}, /courses/{id}/front_page ->
      ANONYMIZE_IDENTITY. Previously ungated: `last_edited_by` leaked a display
      name and avatar URL. front_page returns the same block but carries no
      'pages' segment, so it stayed ungated after #179 until a live check found
      it still returning display_name, pronouns and avatar_image_url. Page
      *bodies* are deliberately exempt — instructors publish office hours,
      contact addresses and phone numbers on course pages, and redacting those
      would break the page's purpose.

    Intentionally NOT matched at all:
    - /groups listings — carry group names, not student names; generic
      anonymization would mangle them. Membership goes via /groups/{id}/users,
      which the '/users' rule covers.
    - /discussion_topics listings (incl. announcements) — typically
      instructor-authored; student content lives under the content endpoints
      matched below.
    - /submissions/self — the caller's OWN submission. Anonymizing it redacts
      body/url/attachments, so a student cannot read back what they submitted
      (issue #166). Only the literal 'self' sub-route is excluded; any other
      sensitive segment in the same path still forces anonymization.
    - the exact self-only paths in _SELF_ONLY_ENDPOINTS — the caller's OWN
      identity (issue #171). Exact full-path match only.
    """
    segments = _path_segments(endpoint)

    if _is_self_only_endpoint(segments):
        return ANONYMIZE_NONE

    self_indices = _self_submission_indices(segments)
    if self_indices:
        segments = [seg for i, seg in enumerate(segments) if i not in self_indices]

    if 'discussion_topics' in segments and _has_route_segment(
        segments, {'entries', 'view', 'entry_list', 'replies'}
    ):
        return ANONYMIZE_FULL

    if _has_route_segment(segments, {'users', 'submissions', 'enrollments', 'analytics'}):
        return ANONYMIZE_FULL

    if _has_route_segment(segments, {'conversations'}):
        return ANONYMIZE_FREE_TEXT

    if _has_route_segment(segments, {'pages'}) or 'front_page' in segments:
        return ANONYMIZE_IDENTITY

    return ANONYMIZE_NONE


def _should_anonymize_endpoint(endpoint: str) -> bool:
    """Whether `endpoint` gets any anonymization at all.

    Thin boolean view over :func:`_endpoint_anonymization_mode`. Callers that
    need to know *which* treatment applies must use the mode function; this one
    only answers "is this endpoint gated".
    """
    return _endpoint_anonymization_mode(endpoint) != ANONYMIZE_NONE


def _anonymize_for_endpoint(result: Any, endpoint: str) -> tuple[Any, str]:
    """Apply `endpoint`'s anonymization tier to a Canvas response.

    Returns the (possibly rewritten) payload plus a short label for debug
    logging. The identity/free_text tiers deliberately skip the typed
    refinements in ``_apply_type_refinements``: those exist for submission and
    assignment records, which neither /conversations nor /pages returns.
    """
    mode = _endpoint_anonymization_mode(endpoint)

    if mode == ANONYMIZE_FULL:
        data_type = _determine_data_type(endpoint)
        return anonymize_response_data(result, data_type), data_type
    if mode == ANONYMIZE_IDENTITY:
        return scrub_identity(result, scrub_text=False), mode
    if mode == ANONYMIZE_FREE_TEXT:
        return scrub_identity(result, scrub_display_names=False), mode
    return result, mode


def new_session_client() -> httpx.AsyncClient:
    """A Canvas client carrying the captured session cookies."""
    from .config import get_config

    config = get_config()
    session = load_session()
    return httpx.AsyncClient(
        cookies=dict(session.cookies) if session is not None else {},
        headers=_session_headers(),
        timeout=config.api_timeout,
        follow_redirects=False,
    )


async def cleanup_http_client() -> None:
    """Compatibility no-op: clients are created per request now."""
    return None


@asynccontextmanager
async def canvas_authenticated_client() -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client authenticated with the student's Canvas session cookie."""
    session, reason = await ensure_session()
    if session is None:
        raise PermissionError(reason or "No Canvas session available.")
    async with new_session_client() as client:
        yield client


async def make_canvas_request(
    method: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | list[tuple[str, Any]] | None = None,
    use_form_data: bool = False,
    skip_anonymization: bool = False,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    api_root: Literal["rest", "quiz"] = API_ROOT_REST,
    _pagination: dict[str, str | None] | None = None,
) -> Any:
    """Make a request to the Canvas API with proper error handling.

    Automatically retries on rate limit errors (429) with exponential backoff.

    Args:
        method: HTTP method (get, post, put, delete)
        endpoint: Canvas API endpoint
        params: Query parameters
        data: Request body data
        use_form_data: Use form data instead of JSON
        skip_anonymization: Skip anonymization (used by paginated fetchers)
        files: Dictionary of file objects for multipart form uploads
        api_root: Which Canvas API root to call ("rest" => /api/v<N>, "quiz" => /api/quiz/v1)
    """

    from .audit import log_data_access
    from .config import get_config

    config = get_config()

    session, session_reason = await ensure_session()
    if session is None:
        return {"error": session_reason or "No Canvas session available."}

    if not endpoint.startswith('/'):
        endpoint = f"/{endpoint}"

    bad_delimiter = next((c for c in ("?", "#") if c in endpoint), None)
    if bad_delimiter is not None:
        log_warning(
            "Blocked Canvas API request with a delimiter in the endpoint path",
            endpoint=sanitize_url(endpoint),
        )
        return RequestFailure(f"Invalid endpoint: '{bad_delimiter}' is not allowed in a request path", WriteOutcome.NOT_DISPATCHED)
    if any(seg == ".." for seg in endpoint.split("/")):
        log_warning(
            "Blocked Canvas API request with a traversal segment in the endpoint path",
            endpoint=sanitize_url(endpoint),
        )
        return RequestFailure("Invalid endpoint: '..' is not allowed in a request path", WriteOutcome.NOT_DISPATCHED)

    if api_root not in (API_ROOT_REST, API_ROOT_QUIZ):
        return {"error": f"Unsupported api_root: {api_root}"}

    client = new_session_client()
    _close_client = True
    try:
        base_url = _resolve_canvas_api_root(config.canvas_api_url.rstrip('/'), api_root)
    except ValueError as exc:
        await client.aclose()
        return {"error": str(exc)}
    url = f"{base_url}{endpoint}"

    try:
        if _pagination is not None and _pagination.get("url"):
            try:
                target = httpx.URL(cast(str, _pagination["url"]))
            except httpx.InvalidURL:
                return {"error": "Invalid pagination link"}
            expected = httpx.URL(url)
            if target.copy_with(query=None) != expected.copy_with(query=None):
                return {"error": "Invalid pagination link: origin or endpoint changed"}
            url = str(target)
            params = None
        semaphore = _get_request_semaphore()
        async with semaphore:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    if config.log_api_requests:
                        retry_info = f" (retry {attempt}/{MAX_RETRIES})" if attempt > 0 else ""
                        log_debug(f"Making {method.upper()} request to {sanitize_url(url)}{retry_info}")

                    if method.lower() == "get":
                        response = await client.get(url, params=params)
                    elif method.lower() == "post":
                        if files:
                            response = await client.post(
                                url,
                                data=cast("dict[str, Any] | None", data),
                                files=files,
                            )
                        elif use_form_data:
                            if isinstance(data, list):
                                encoded = urlencode(data)
                                response = await client.post(
                                    url,
                                    content=encoded,
                                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                                )
                            else:
                                response = await client.post(url, data=data)
                        else:
                            response = await client.post(url, json=data)
                    elif method.lower() == "put":
                        if use_form_data:
                            if isinstance(data, list):
                                encoded = urlencode(data)
                                response = await client.put(
                                    url,
                                    content=encoded,
                                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                                )
                            else:
                                response = await client.put(url, data=data)
                        else:
                            response = await client.put(url, json=data)
                    elif method.lower() == "delete":
                        response = await client.delete(url, params=params)
                    else:
                        return {"error": f"Unsupported method: {method}"}

                    if _is_login_response(response):
                        clear_session()
                        return {"error": SESSION_EXPIRED_MESSAGE}
                    response.raise_for_status()
                    result = _response_json(response)
                    if _pagination is not None:
                        _pagination["current"] = str(response.request.url)
                        _pagination["next"] = response.links.get("next", {}).get("url")

                    if not skip_anonymization and config.enable_data_anonymization:
                        result, applied = _anonymize_for_endpoint(result, endpoint)

                        if config.anonymization_debug and applied != ANONYMIZE_NONE:
                            log_debug(f"Applied {applied} anonymization to {endpoint}")

                    log_data_access(method, endpoint, "success")

                    return result

                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429 and attempt < MAX_RETRIES:
                        retry_after = e.response.headers.get('Retry-After')
                        if retry_after:
                            try:
                                wait_time = int(retry_after)
                            except ValueError:
                                wait_time = INITIAL_BACKOFF_SECONDS * (2 ** attempt)
                        else:
                            wait_time = INITIAL_BACKOFF_SECONDS * (2 ** attempt)

                        log_warning(f"Rate limited (429). Retrying in {wait_time}s...", attempt=attempt + 1, max_retries=MAX_RETRIES)
                        await asyncio.sleep(wait_time)
                        continue

                    error_message = f"HTTP error: {e.response.status_code}"
                    try:
                        error_details = e.response.json()
                        error_message += f", Details: {error_details}"
                    except ValueError:
                        error_details = e.response.text
                        error_message += f", Text: {error_details}"

                    log_error(f"API error on {sanitize_url(endpoint)}", status_code=e.response.status_code)

                    log_data_access(method, endpoint, "error", f"HTTP {e.response.status_code}")

                    outcome = (WriteOutcome.REJECTED if e.response.status_code in NO_WRITE_STATUSES
                               else WriteOutcome.MAY_HAVE_WRITTEN)
                    return RequestFailure(error_message, outcome)

                except Exception as e:
                    log_error(f"Request failed for {sanitize_url(endpoint)}", error_type=type(e).__name__)

                    log_data_access(method, endpoint, "error", type(e).__name__)

                    return RequestFailure(f"Request failed: {str(e)}", WriteOutcome.MAY_HAVE_WRITTEN)

            return {"error": "Max retries exceeded"}
    finally:
        if _close_client:
            await client.aclose()


def _url_origin(url: str) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` for a URL, or None when it is not absolute."""
    try:
        parsed = httpx.URL(url)
    except Exception:
        return None
    if not parsed.scheme or not parsed.host:
        return None
    scheme = parsed.scheme.lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, parsed.host.lower(), port


def canvas_origin() -> tuple[str, str, int] | None:
    """The configured Canvas API's origin: scheme, host and port."""
    from .config import get_config

    return _url_origin(get_config().canvas_api_url)


def is_canvas_origin(url: str) -> bool:
    """Whether a URL points at this Canvas instance and nothing else."""
    origin = _url_origin(url)
    return origin is not None and origin == canvas_origin()


async def iter_canvas_file_bytes(
    url: str, max_redirects: int = 5
) -> AsyncIterator[bytes]:
    """Yield a Canvas file's bytes, keeping the session on Canvas's own origin.

    Canvas file URLs answer with a redirect to inst-fs/S3 whose target carries
    its own signature. Sending the session cookie there would leak the login
    and break the request, so cookies go only to the configured Canvas origin —
    including across redirect hops. Once the download leaves Canvas it continues
    unauthenticated, as the signature expects.

    A Canvas file URL that redirects to the login page is an expired session,
    not a file. It raises ``CanvasSessionExpired`` rather than downloading the
    HTML login page as if it were course content.
    """
    from .config import get_config

    timeout = get_config().api_timeout
    target = url
    for _ in range(max_redirects + 1):
        client_cm = (
            canvas_authenticated_client()
            if is_canvas_origin(target)
            else httpx.AsyncClient(timeout=timeout)
        )
        async with client_cm as client:
            async with client.stream("GET", target, follow_redirects=False) as response:
                if response.is_redirect:
                    if is_canvas_origin(target) and _is_login_response(response):
                        clear_session()
                        raise CanvasSessionExpired()
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError(
                            "Canvas file download redirected without a Location header"
                        )
                    target = str(response.url.join(location))
                    continue
                if (
                    is_canvas_origin(target)
                    and "text/html" in response.headers.get("content-type", "").lower()
                ):
                    body = await response.aread()
                    if looks_like_login(body.decode("utf-8", errors="ignore")):
                        clear_session()
                        raise CanvasSessionExpired()
                    yield body
                    return
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    yield chunk
                return
    raise RuntimeError("Canvas file download redirected too many times")


async def upload_file_to_storage(
    upload_url: str,
    upload_params: dict[str, Any],
    file_path: str,
    filename: str,
    content_type: str
) -> dict[str, Any]:
    """Upload a file to Canvas storage URL (step 2 of 3-step upload process).

    This function handles the multipart file upload to S3/Instructure storage.
    It's called after requesting an upload URL from the Canvas API.

    Args:
        upload_url: The pre-signed upload URL from Canvas API
        upload_params: Additional parameters required by the storage (from Canvas API)
        file_path: Local filesystem path to the file
        filename: Name to use for the uploaded file
        content_type: MIME type of the file

    Returns:
        Response from the storage service, typically containing file confirmation
        or redirect location

    Note:
        This posts to external storage (S3/Instructure), not the Canvas API.
        The response handling differs from regular Canvas API calls.
    """
    from .config import get_config

    config = get_config()

    async with httpx.AsyncClient(timeout=config.api_timeout) as client:
        try:
            with open(file_path, 'rb') as f:
                file_content = f.read()

            files = {
                'file': (filename, file_content, content_type)
            }

            if config.log_api_requests:
                log_debug(f"Uploading file to storage: {sanitize_url(upload_url)}", filename=filename, content_type=content_type, size=len(file_content))

            response = await client.post(
                upload_url,
                data=upload_params,
                files=files,
                follow_redirects=False
            )

            if response.status_code in (200, 201):
                try:
                    body: dict[str, Any] = response.json()
                    return body
                except ValueError:
                    return {"success": True, "status_code": response.status_code}

            elif response.status_code in (301, 302, 303):
                redirect_url = response.headers.get('Location')
                if not redirect_url:
                    return {"error": "Redirect without Location header"}

                absolute = str(response.url.join(redirect_url))
                if not is_canvas_origin(absolute):
                    return {
                        "error": (
                            "Refused to confirm this upload: the storage host "
                            "redirected to a different origin, and the Canvas "
                            "session is only sent to Canvas. Nothing was confirmed."
                        )
                    }
                try:
                    async with canvas_authenticated_client() as canvas_client:
                        confirm_response = await canvas_client.get(absolute)
                        if _is_login_response(confirm_response):
                            clear_session()
                            return {"error": SESSION_EXPIRED_MESSAGE}
                        confirm_response.raise_for_status()
                        confirmed: dict[str, Any] = _response_json(confirm_response)
                        return confirmed
                except PermissionError as e:
                    return {"error": str(e)}

            else:
                error_text = response.text
                return {
                    "error": f"Storage upload failed with status {response.status_code}",
                    "details": error_text
                }

        except FileNotFoundError:
            return {"error": f"File not found: {file_path}"}
        except PermissionError:
            return {"error": f"Permission denied reading file: {file_path}"}
        except httpx.TimeoutException:
            return {"error": "Upload timed out"}
        except Exception as e:
            return {"error": f"Upload failed: {str(e)}"}


async def fetch_all_paginated_results(
    endpoint: str,
    params: dict[str, Any] | None = None,
    skip_anonymization: bool = False,
    api_root: Literal["rest", "quiz"] = API_ROOT_REST,
) -> Any:
    """Fetch all results from a paginated Canvas API endpoint.

    Handles pagination automatically and applies anonymization once to the complete dataset
    to ensure consistent anonymization across all pages.

    Args:
        endpoint: Canvas API endpoint.
        params: Query parameters.
        skip_anonymization: Return the raw records. Reserved for the one caller
            whose whole purpose is to see real identities — the local
            pseudonym-map export. Anonymizing that fetch maps pseudonyms to
            pseudonyms, which is not a privacy win, just a broken tool.
        api_root: Which Canvas API root to call ("rest" => /api/v<N>,
            "quiz" => /api/quiz/v1). Only the base URL changes; the
            anonymization gate keys off ``endpoint``, so an alternate root
            cannot route around it. Paginated quiz-root callers must use this
            rather than looping over ``make_canvas_request`` themselves, or they
            lose the single-anonymization-pass-over-the-complete-dataset
            property this function exists to provide.
    """
    current_params = deepcopy(params) if params is not None else {}
    current_params.setdefault("per_page", DEFAULT_PAGE_SIZE)
    current_params["page"] = 1
    pagination: dict[str, str | None] = {}
    seen: set[str] = set()
    all_results: list[Any] = []

    for _ in range(MAX_PAGINATION_PAGES):
        response = await make_canvas_request(
            "get", endpoint, params=current_params, skip_anonymization=True,
            api_root=api_root, _pagination=pagination,
        )
        if isinstance(response, dict) and "error" in response:
            return response
        if not isinstance(response, list):
            return {"error": "Invalid paginated response: expected a list"}
        all_results.extend(response)
        current = pagination.get("current")
        if current is not None:
            seen.add(str(httpx.URL(current)))
        next_url = pagination.get("next")
        if not next_url:
            break
        try:
            next_identity = str(httpx.URL(next_url))
        except httpx.InvalidURL:
            return {"error": "Invalid pagination link"}
        if next_identity in seen:
            return {"error": "Pagination cycle detected; no partial result returned"}
        pagination["url"] = next_url
        current_params = {}
    else:
        return {"error": f"Pagination exceeded {MAX_PAGINATION_PAGES} pages; no partial result returned"}

    from .config import get_config
    config = get_config()

    if not skip_anonymization and config.enable_data_anonymization:
        all_results, applied = _anonymize_for_endpoint(all_results, endpoint)

        if config.anonymization_debug and applied != ANONYMIZE_NONE:
            log_debug(f"Applied {applied} anonymization to paginated results from {endpoint}")

    return all_results
