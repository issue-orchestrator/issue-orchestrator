"""Browser session + CSRF for the Control Center UI and Web Dashboard.

The Control API's bearer-token middleware gates programmatic clients,
but the browser can't send an ``Authorization: Bearer`` header on
``EventSource`` connections, and rendering the admin token into the
HTML would leak it to XSS. This module adds a second auth path that's
native to the browser:

- **Session cookie** (``io_session``, HttpOnly, SameSite=Strict,
  Path=/) — set by ``POST /login`` after the admin bearer token is
  verified. The cookie value is **stateless**: it carries its own
  session id, expiry, and an HMAC over both. Verifying a cookie
  requires only the shared secret and the current time — no
  server-side state.
- **CSRF token** — derived deterministically from the session id and
  the secret, then rendered into the HTML (``<meta
  name="io-csrf-token" ...>``) so JS can read it and send it as
  ``X-CSRF-Token`` on every mutating fetch. ``SameSite=Strict``
  already blocks cross-origin cookies; the CSRF token is
  defense-in-depth.
- **SSE query-string token** — ``EventSource`` can't send headers, so
  the SSE endpoint accepts a short-lived HMAC-signed token in the
  query string that JS obtains via the (CSRF-protected)
  ``/api/sse-token`` endpoint. The token is single-use within its
  process (verifying consumes its nonce) and valid for
  ``SSE_TOKEN_TTL_SECONDS``.

## Cross-process sharing

Why stateless? The Control Center (port 19080) and the Web Dashboard
(port 8080) are separate processes. Before this change each process
generated its own random HMAC secret and kept its own in-memory
``_SESSIONS`` dict, so a cookie minted by one was unverifiable by
the other — operators had to log in twice. By deriving the secret
deterministically from the shared admin token (``derive_secret``)
and validating cookies purely from their HMAC, both processes accept
the same cookie without any IPC.

Trade-off: there is no server-side revocation. Sessions only
invalidate by TTL. Rotating the admin token rotates the derived
secret, which invalidates every existing cookie at once (an
operational kill switch).

The single-use SSE-nonce store is still per-process. A leaked SSE
token that's been consumed in CC's process can in principle be
replayed once in the dashboard's process within its 60-second TTL.
Acceptable: SSE tokens only open the matching session's stream, the
attack window is brief, and the alternative — a shared on-disk
nonce store — is a lot of complexity for a narrow gap.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock

logger = logging.getLogger(__name__)

SESSION_COOKIE = "io_session"
CSRF_HEADER = "X-CSRF-Token"
CSRF_META_NAME = "io-csrf-token"
SSE_TOKEN_QUERY = "sse_token"

# Domain-separation tags so the derived secrets / tokens cannot
# collide with any other use of the admin token or the session id.
_SECRET_DERIVATION_TAG = b"issue-orchestrator/browser-session/v1"
_CSRF_DERIVATION_TAG = b"csrf"

_DEFAULT_SESSION_TTL_SECONDS = 8 * 3600
_DEFAULT_SSE_TOKEN_TTL_SECONDS = 60
# Kept as a module attribute for back-compat with operator YAML and
# the existing settings schema. With stateless cookies the value is
# no longer enforced — there is no in-memory session table to cap.
_DEFAULT_MAX_SESSIONS = 1024

SESSION_TTL_SECONDS = _DEFAULT_SESSION_TTL_SECONDS
SSE_TOKEN_TTL_SECONDS = _DEFAULT_SSE_TOKEN_TTL_SECONDS
MAX_SESSIONS = _DEFAULT_MAX_SESSIONS

_ENV_SESSION_TTL = "ISSUE_ORCHESTRATOR_SESSION_TTL_SECONDS"
_ENV_SSE_TOKEN_TTL = "ISSUE_ORCHESTRATOR_SSE_TOKEN_TTL_SECONDS"
_ENV_MAX_SESSIONS = "ISSUE_ORCHESTRATOR_MAX_SESSIONS"

_SECRET: bytes | None = None
_LOCK = Lock()


@dataclass(frozen=True)
class _ParsedCookie:
    session_id: str
    issued_at: int


def _resolve_int(env_name: str, config_value: int | None, default: int) -> int:
    """Env var wins; config falls through; default is the last resort."""
    raw = os.environ.get(env_name)
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            logger.warning(
                "Ignoring invalid %s=%r; not an integer", env_name, raw
            )
    if config_value is not None:
        return int(config_value)
    return default


def derive_secret(admin_token: str) -> bytes:
    """Derive a deterministic 32-byte secret from the admin token.

    Both the Control Center and the Web Dashboard call this with the
    same admin token (loaded from ``~/.issue-orchestrator/api-token``)
    so they end up with the same HMAC key without sharing storage.
    The domain-separation tag keeps this output distinct from any
    other use of the admin token.
    """
    return hmac.new(
        _SECRET_DERIVATION_TAG, admin_token.encode("utf-8"), sha256
    ).digest()


def initialize(
    secret: bytes | None = None,
    *,
    admin_token: str | None = None,
    session_ttl_seconds: int | None = None,
    sse_token_ttl_seconds: int | None = None,
    max_sessions: int | None = None,
) -> None:
    """Initialize the process-wide HMAC secret and tunable knobs.

    The secret is resolved in this priority:

    1. ``secret=`` — explicit bytes (used by tests).
    2. ``admin_token=`` — derive deterministically. Production path.
    3. neither — generate a random secret. Single-process fallback.

    Each tunable falls through env var → passed-in value → default.

    ``max_sessions`` is accepted for back-compat with operator YAML
    but no longer enforced — there is no in-memory session table to
    cap with stateless cookies. The value is stored on
    ``MAX_SESSIONS`` so existing tests / dashboards can still read it.
    """
    global _SECRET, SESSION_TTL_SECONDS, SSE_TOKEN_TTL_SECONDS, MAX_SESSIONS
    if secret is not None:
        _SECRET = secret
    elif admin_token is not None:
        _SECRET = derive_secret(admin_token)
    else:
        _SECRET = secrets.token_bytes(32)
    SESSION_TTL_SECONDS = _resolve_int(
        _ENV_SESSION_TTL, session_ttl_seconds, _DEFAULT_SESSION_TTL_SECONDS
    )
    SSE_TOKEN_TTL_SECONDS = _resolve_int(
        _ENV_SSE_TOKEN_TTL,
        sse_token_ttl_seconds,
        _DEFAULT_SSE_TOKEN_TTL_SECONDS,
    )
    MAX_SESSIONS = _resolve_int(
        _ENV_MAX_SESSIONS, max_sessions, _DEFAULT_MAX_SESSIONS
    )
    _CONSUMED_SSE_NONCES.clear()


def is_initialized() -> bool:
    return _SECRET is not None


def shutdown() -> None:
    """Clear state. Used by tests between cases."""
    global _SECRET, SESSION_TTL_SECONDS, SSE_TOKEN_TTL_SECONDS, MAX_SESSIONS
    _SECRET = None
    _CONSUMED_SSE_NONCES.clear()
    SESSION_TTL_SECONDS = _DEFAULT_SESSION_TTL_SECONDS
    SSE_TOKEN_TTL_SECONDS = _DEFAULT_SSE_TOKEN_TTL_SECONDS
    MAX_SESSIONS = _DEFAULT_MAX_SESSIONS


# ---------------------------------------------------------------------------
# Cookie sign / parse
# ---------------------------------------------------------------------------


def _sign_cookie(session_id: str, issued_at: int) -> str:
    assert _SECRET is not None
    payload = f"{session_id}.{issued_at}"
    sig = hmac.new(_SECRET, payload.encode("utf-8"), sha256).hexdigest()
    return f"{payload}.{sig}"


def _parse_cookie(cookie_value: str | None) -> _ParsedCookie | None:
    """Verify and parse a cookie value. Returns ``None`` on rejection.

    Rejects on any of: missing secret, malformed structure, bad HMAC,
    unparseable timestamp, or age greater than the validating process's
    ``SESSION_TTL_SECONDS``. The cookie carries the **issue time**, not
    a baked-in expiry, so the validating process owns the TTL policy.
    Without that split a Control Center configured for 8h would let a
    cookie outlive a Web Dashboard configured for 60s — the same
    ``ui.browser_session.ttl_seconds`` rule enforced differently by
    path (#6065 re-review-1 P2).
    """
    if not cookie_value or _SECRET is None:
        return None
    parts = cookie_value.rsplit(".", 1)
    if len(parts) != 2:
        return None
    payload, sig = parts
    payload_parts = payload.split(".")
    if len(payload_parts) != 2:
        return None
    session_id, issued_at_str = payload_parts
    if not session_id or not issued_at_str:
        return None
    try:
        issued_at = int(issued_at_str)
    except ValueError:
        return None
    expected_sig = hmac.new(
        _SECRET, payload.encode("utf-8"), sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return None
    now = int(time.time())
    # ``issued_at`` from the future (clock skew or a forged cookie that
    # somehow passed the HMAC) is rejected with a small tolerance.
    if issued_at - now > 5:
        return None
    if now - issued_at > SESSION_TTL_SECONDS:
        return None
    return _ParsedCookie(session_id=session_id, issued_at=issued_at)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_session() -> tuple[str, str]:
    """Mint a fresh session. Returns ``(cookie_value, csrf_token)``.

    The cookie value carries its own expiry and HMAC, so any process
    holding the matching secret can validate it without any
    server-side state.
    """
    if _SECRET is None:
        raise RuntimeError("browser_session.initialize was never called")
    session_id = secrets.token_hex(16)
    # Encode issue time, not a baked-in expiry — the validating
    # process applies its own ``SESSION_TTL_SECONDS`` so operator
    # hardening on either surface takes effect uniformly.
    issued_at = int(time.time())
    cookie = _sign_cookie(session_id, issued_at)
    csrf = _derive_csrf(session_id)
    return cookie, csrf


def session_is_valid(cookie_value: str | None) -> bool:
    """Whether the cookie is well-formed, signed, and unexpired."""
    return _parse_cookie(cookie_value) is not None


def _derive_csrf(session_id: str) -> str:
    """Deterministic CSRF token for ``session_id``.

    Returning the same token across processes is safe: the secret
    only lives on the server, and the CSRF token is rendered into
    HTML where JS reads it. The cookie itself remains HttpOnly.
    """
    assert _SECRET is not None
    payload = session_id.encode("utf-8") + b"." + _CSRF_DERIVATION_TAG
    return hmac.new(_SECRET, payload, sha256).hexdigest()


def get_csrf_token(cookie_value: str | None) -> str | None:
    """Return the CSRF token bound to ``cookie_value``, or ``None``.

    A valid cookie always yields the same CSRF for its session id, so
    callers can render the result into HTML without first issuing a
    new session.
    """
    parsed = _parse_cookie(cookie_value)
    if parsed is None:
        return None
    return _derive_csrf(parsed.session_id)


def verify_csrf(cookie_value: str | None, provided: str | None) -> bool:
    """Constant-time comparison of a submitted CSRF token."""
    if not provided:
        return False
    expected = get_csrf_token(cookie_value)
    if expected is None:
        return False
    return hmac.compare_digest(expected, provided)


def issue_sse_token(cookie_value: str | None) -> str | None:
    """Return a short-lived signed SSE token bound to the session.

    Format: ``"{session_id}:{timestamp}:{nonce}:{hex_signature}"``.
    The session id is extracted from the cookie's signed payload, so
    a forged cookie cannot mint a valid SSE token.
    """
    parsed = _parse_cookie(cookie_value)
    if parsed is None:
        return None
    assert _SECRET is not None
    now = int(time.time())
    nonce = secrets.token_hex(8)
    payload = f"{parsed.session_id}:{now}:{nonce}".encode("utf-8")
    sig = hmac.new(_SECRET, payload, sha256).hexdigest()
    return f"{parsed.session_id}:{now}:{nonce}:{sig}"


# Per-process replay guard for SSE tokens. Each successfully verified
# token's nonce is stored here for ``SSE_TOKEN_TTL_SECONDS`` so the
# same token cannot be replayed within its window.
_CONSUMED_SSE_NONCES: dict[str, float] = {}


def _expire_consumed_nonces(now: float) -> None:
    stale = [
        nonce
        for nonce, seen_at in _CONSUMED_SSE_NONCES.items()
        if now - seen_at > SSE_TOKEN_TTL_SECONDS
    ]
    for nonce in stale:
        _CONSUMED_SSE_NONCES.pop(nonce, None)


def verify_sse_token(token: str | None, expected_cookie: str | None) -> bool:
    """Validate an SSE query-string token for the session in ``expected_cookie``.

    Single-use semantics: a successful verify records the token's
    nonce in ``_CONSUMED_SSE_NONCES`` for ``SSE_TOKEN_TTL_SECONDS``.

    The token must match the session id encoded in the cookie, be
    signed with the current process secret, and be less than
    ``SSE_TOKEN_TTL_SECONDS`` old.
    """
    if not token or _SECRET is None:
        return False
    cookie_parsed = _parse_cookie(expected_cookie)
    if cookie_parsed is None:
        return False
    expected_session_id = cookie_parsed.session_id
    parts = token.split(":")
    if len(parts) != 4:
        return False
    sid, ts_str, nonce, sig = parts
    if sid != expected_session_id:
        return False
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    now = time.time()
    if now - ts > SSE_TOKEN_TTL_SECONDS:
        return False
    if now - ts < -5:  # clock skew tolerance
        return False
    payload = f"{sid}:{ts_str}:{nonce}".encode("utf-8")
    expected_sig = hmac.new(_SECRET, payload, sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return False
    with _LOCK:
        _expire_consumed_nonces(now)
        if nonce in _CONSUMED_SSE_NONCES:
            return False
        _CONSUMED_SSE_NONCES[nonce] = now
    return True
