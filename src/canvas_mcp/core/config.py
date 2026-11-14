"""Configuration management for Canvas MCP server."""

import os
import re
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv

from .logging import log_error, log_info, log_warning

# Load environment variables from .env file
load_dotenv()

_INVALID_INT_ENV_VARS: dict[str, str] = {}
_INVALID_FLOAT_ENV_VARS: dict[str, str] = {}

# Canonical names of the student write tools that can be enabled. Quiz-taking is
# deliberately absent: it is an academic-integrity decision of its own.
STUDENT_WRITE_TOOL_NAMES = frozenset({
    "submit_assignment",
    "comment_on_my_submission",
    "mark_module_item_done",
})


def _parse_keys(raw: str) -> frozenset[str]:
    """Parse a comma/whitespace-separated list of access keys into a set."""
    if not raw:
        return frozenset()
    return frozenset(k for k in raw.replace(",", " ").split() if k)


def _normalize_canvas_url(raw: str) -> str:
    """Normalize ``CANVAS_API_URL`` to the canonical ``…/api/v1`` form.

    Canvas REST endpoints live under ``/api/v1``. Users frequently enter just
    the base host (e.g. ``https://canvas.school.edu``); requests without the
    suffix make Canvas issue a 302 redirect to SSO login, which surfaces as a
    misleading ``HTTP error: 302`` that looks like a bad session. Canonicalize
    the path to exactly ``/api/v1`` (dropping any extra segments copied from a
    browser, plus stray query strings / fragments) so all of these resolve to
    the same URL:

    - ``https://canvas.school.edu``             → ``https://canvas.school.edu/api/v1``
    - ``https://canvas.school.edu/``            → ``https://canvas.school.edu/api/v1``
    - ``https://canvas.school.edu/api/v1``      → unchanged
    - ``https://canvas.school.edu/api/v1/``     → ``https://canvas.school.edu/api/v1``
    - ``https://canvas.school.edu/api/v1/foo``  → ``https://canvas.school.edu/api/v1``
    - ``https://canvas.school.edu/api/v1?x=1``  → ``https://canvas.school.edu/api/v1``

    An explicit ``/api/v<N>`` version segment is preserved (only trailing
    sub-paths after it are dropped), so a deliberately-set ``/api/v2`` is never
    silently downgraded to ``/api/v1``.

    A scheme-less input (e.g. ``canvas.school.edu``) is returned unchanged so
    ``validate_config()`` can flag the missing ``https://`` rather than this
    silently producing a relative-path URL.
    """
    url = raw.strip()
    if not url:
        return ""

    parsed = urlparse(url)
    # Without a scheme, urlparse puts the host in ``path`` and leaves
    # ``netloc`` empty — we can't reliably rebuild it, so leave it for the
    # validator to warn about.
    if not parsed.scheme or not parsed.netloc:
        return url

    # Preserve an existing ``/api/v<N>`` version segment (truncating any extra
    # path after it), matching only at a segment boundary so a real version
    # like ``/api/v2`` is kept rather than rewritten. When the path carries no
    # version segment, append the canonical ``/api/v1``.
    version = re.search(r"/api/v\d+(?=/|$)", parsed.path)
    path = parsed.path[: version.end()] if version else "/api/v1"
    return urlunparse(parsed._replace(path=path, params="", query="", fragment=""))


def _is_loopback(hostname: str | None) -> bool:
    """True for addresses that never leave the machine.

    The only place cleartext HTTP is defensible is a local development Canvas,
    where there is no network path to sniff.
    """
    if not hostname:
        return False
    host = hostname.strip().strip("[]").lower()
    if host in {"localhost", "::1"}:
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() == "true"


def validate_canvas_url_scheme() -> bool:
    """Reject a cleartext Canvas origin. Returns False when startup must abort.

    Every Canvas request carries the student's session cookie, so an http://
    origin puts a credential for their student records on the wire for anyone
    on the path. A warning is not proportionate to that; a loopback origin is
    the only cleartext exception.

    Called from BOTH startup paths. The Canvas URL is server-pinned, so a typo
    would expose the one session this server holds to the wrong host.
    """
    from urllib.parse import urlparse

    config = get_config()
    parsed = urlparse(config.canvas_api_url)
    if not parsed.scheme or parsed.scheme == "https" or not parsed.netloc:
        # Missing scheme / missing host are reported separately by
        # validate_config(); this function only owns the cleartext case.
        return True
    if parsed.scheme != "http":
        return True

    if _is_loopback(parsed.hostname) and _bool_env("CANVAS_ALLOW_INSECURE_HTTP", False):
        log_warning(
            "CANVAS_API_URL uses cleartext http:// to a loopback address; "
            "allowed because CANVAS_ALLOW_INSECURE_HTTP is set. Never use "
            "this against a real Canvas instance.",
            current_url=config.canvas_api_url,
        )
        return True

    log_error(
        "CANVAS_API_URL must use 'https://'. The Canvas session cookie is sent "
        "on every request, so a cleartext URL exposes the student's session on "
        "the network. "
        "For local development against a loopback address only, set "
        "CANVAS_ALLOW_INSECURE_HTTP=true.",
    )
    return False


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        _INVALID_INT_ENV_VARS[name] = value
        return default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        parsed = float(value)
    except ValueError:
        _INVALID_FLOAT_ENV_VARS[name] = value
        return default
    if parsed <= 0:
        _INVALID_FLOAT_ENV_VARS[name] = value
        return default
    return parsed


class Config:
    """Configuration class for Canvas MCP server."""

    def __init__(self) -> None:
        # Required configuration. Authentication is a browser session captured
        # from Chrome, not an API token (Canvas disables student tokens at many
        # schools).
        self.canvas_session_file = os.getenv("CANVAS_SESSION_FILE", "").strip()
        self.canvas_chrome_profile = os.getenv("CANVAS_CHROME_PROFILE", "").strip()
        self.canvas_chrome_path = os.getenv("CANVAS_CHROME_PATH", "").strip()
        self.canvas_login_timeout = _int_env("CANVAS_LOGIN_TIMEOUT", 180)
        self.canvas_auth_no_browser = _bool_env("CANVAS_AUTH_NO_BROWSER", False)
        # Keep the configured (pre-normalization) value so validate_config()
        # can report the normalization delta from the same read that produced
        # canvas_api_url. Whitespace-trimmed, matching the normalizer's input.
        self.canvas_api_url_configured = os.getenv("CANVAS_API_URL", "").strip()
        self.canvas_api_url = _normalize_canvas_url(self.canvas_api_url_configured)

        # Optional configuration with defaults
        self.mcp_server_name = os.getenv("MCP_SERVER_NAME", "canvas-api")
        self.debug = _bool_env("DEBUG", False)
        self.api_timeout = _int_env("API_TIMEOUT", 30)
        self.cache_ttl = _int_env("CACHE_TTL", 300)
        self.max_concurrent_requests = _int_env("MAX_CONCURRENT_REQUESTS", 10)
        self.read_file_max_size_mb = _float_env("READ_FILE_MAX_SIZE_MB", 100.0)

        # Local student workspace: extracted material, study guides, notes,
        # drafts and the work journal. Empty means ~/.canvas-mcp/student.
        self.student_dir = os.getenv("CANVAS_STUDENT_DIR", "").strip()

        # Development configuration
        self.log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        self.log_api_requests = _bool_env("LOG_API_REQUESTS", False)

        # Privacy and security configuration
        self.enable_data_anonymization = _bool_env("ENABLE_DATA_ANONYMIZATION", True)
        self.anonymization_debug = _bool_env("ANONYMIZATION_DEBUG", False)
        self.log_redact_pii = _bool_env("LOG_REDACT_PII", True)

        # Audit logging configuration
        self.log_access_events = _bool_env("LOG_ACCESS_EVENTS", False)
        self.log_execution_events = _bool_env("LOG_EXECUTION_EVENTS", False)
        self.audit_log_dir = os.getenv("AUDIT_LOG_DIR", "")

        # Optional metadata        # Optional metadata
        self.institution_name = os.getenv("INSTITUTION_NAME", "")
        self.timezone = os.getenv("TIMEZONE", "UTC")

        # Role-based tool filtering
        self.canvas_role = os.getenv("CANVAS_ROLE", "student").lower()

        # --- Student write tools (#170) ---
        # Operator ceiling for student write tools. This build is student-only,
        # so all three are enabled by default; an empty value disables them all.
        # Accepts comma- and/or space-separated tool names.
        self.student_write_tools = frozenset(
            name.strip()
            for name in os.getenv(
                "STUDENT_WRITE_TOOLS",
                "submit_assignment,comment_on_my_submission,mark_module_item_done",
            ).replace(",", " ").split()
            if name.strip()
        )
        # Per-course instructor policy. Can further restrict (never expand) the
        # operator ceiling above.
        self.course_agent_policy_enabled = _bool_env("COURSE_AGENT_POLICY_ENABLED", True)
        # Posture when a course states no policy. An instructor can still block
        # agent writes by putting "agent_writes: deny" in the syllabus.
        self.course_agent_policy_default = os.getenv(
            "COURSE_AGENT_POLICY_DEFAULT", "allow"
        ).strip().lower()
        # Denials cache longer than grants. A stale grant is a revocation window on
        # an attempt-consuming action, so it is deliberately short.
        self.course_agent_policy_allow_ttl = _int_env("COURSE_AGENT_POLICY_ALLOW_TTL", 30)
        self.course_agent_policy_deny_ttl = _int_env("COURSE_AGENT_POLICY_DENY_TTL", 300)


# Global configuration instance
_config: Config | None = None


def get_config() -> Config:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = Config()
    return _config


def reset_config() -> None:
    """Discard the cached configuration singleton.

    The next ``get_config()`` call rebuilds it from the current environment.
    Used by tests that patch environment variables so they don't read stale
    config captured at first access.

    Also clears the invalid-env-var caches, which are populated during
    ``Config.__init__`` and read by ``validate_config()``; otherwise a stale
    entry from a prior parse would produce a warning inconsistent with the
    rebuilt configuration's environment.

    Scope: this resets the config singleton only. The browser session is
    resolved lazily by ``core.session``, so a caller that changes
    ``CANVAS_SESSION_FILE`` must also call ``core.session.reset_session_cache()``.
    """
    global _config
    _config = None
    _INVALID_INT_ENV_VARS.clear()
    _INVALID_FLOAT_ENV_VARS.clear()


def validate_config() -> bool:
    """Validate that required configuration is present."""
    config = get_config()
    unimplemented_env_vars = {
        "TOKEN_STORAGE_BACKEND": "token storage backend selection is not enforced yet",
        "TOKEN_ENVELOPE_KEY_SOURCE": "token envelope encryption is not enforced yet",
        "MCP_CLIENT_AUTH_MODE": "MCP client authentication is not implemented for stdio transport",
        "MCP_CLIENT_API_KEY_REQUIRED": "MCP client authentication is not implemented for stdio transport",
        "MCP_CLIENT_CERT_AUTHORITY": "MCP client authentication is not implemented for stdio transport",
        "LOG_ROTATION_DAYS": "log rotation is not enforced yet",
        "LOG_RETENTION_DAYS": "log retention is not enforced yet",
        "LOG_DESTINATION": "log destinations are not configurable yet",
        "SIEM_FORWARDING_ENABLED": "SIEM forwarding is not implemented yet",
        "MCP_BIND_HOST": "MCP uses stdio transport and does not bind network sockets",
        "MCP_BIND_PORT": "MCP uses stdio transport and does not bind network sockets",
        "FIREWALL_HINT": "firewall hints are documentation-only",
    }

    if not config.canvas_api_url:
        log_error("CANVAS_API_URL environment variable is required")
        log_error("Please set CANVAS_API_URL in your .env file")
        return False

    # Diagnose a CANVAS_API_URL that can't reach Canvas. The triple-slash case
    # (e.g. 'https:///host') is the subtle one: it has a scheme but an empty
    # netloc, so the normalizer leaves it untouched. Report the specific defect
    # rather than a one-size-fits-all message.
    parsed_url = urlparse(config.canvas_api_url)
    if parsed_url.scheme not in ("http", "https"):
        log_warning(
            "CANVAS_API_URL should start with 'https://'",
            current_url=config.canvas_api_url,
        )
    elif not parsed_url.netloc:
        log_warning(
            "CANVAS_API_URL is missing a hostname",
            current_url=config.canvas_api_url,
        )
    elif not validate_canvas_url_scheme():
        return False

    if (
        config.canvas_api_url_configured
        and config.canvas_api_url_configured != config.canvas_api_url
    ):
        log_info(
            "CANVAS_API_URL normalized to canonical form",
            configured=config.canvas_api_url_configured,
            effective=config.canvas_api_url,
        )

    valid_roles = ("student",)

    if config.canvas_role not in valid_roles:
        log_warning(
            f"CANVAS_ROLE should be one of {', '.join(valid_roles)}; "
            f"defaulting to 'all' (got '{config.canvas_role}')"
        )
        config.canvas_role = "student"

    # Student write policy: an unrecognized posture must fail closed, not fall
    # through to something permissive.
    valid_postures = ("allow", "deny")
    if config.course_agent_policy_default not in valid_postures:
        log_warning(
            f"COURSE_AGENT_POLICY_DEFAULT should be one of {', '.join(valid_postures)}; "
            f"defaulting to 'deny' (got '{config.course_agent_policy_default}')"
        )
        config.course_agent_policy_default = "deny"

    unknown_write_tools = config.student_write_tools - STUDENT_WRITE_TOOL_NAMES
    if unknown_write_tools:
        log_warning(
            "STUDENT_WRITE_TOOLS names unknown tools; they will be ignored: "
            f"{', '.join(sorted(unknown_write_tools))}"
        )

    for env_name, env_value in _INVALID_INT_ENV_VARS.items():
        log_warning(
            f"{env_name} expects an integer; using default value "
            f"(got '{env_value}')"
        )

    for env_name, env_value in _INVALID_FLOAT_ENV_VARS.items():
        log_warning(
            f"{env_name} expects a positive number; using default value "
            f"(got '{env_value}')"
        )

    for env_name, note in unimplemented_env_vars.items():
        if os.getenv(env_name):
            log_warning(f"{env_name} is set but {note}.")

    return True
