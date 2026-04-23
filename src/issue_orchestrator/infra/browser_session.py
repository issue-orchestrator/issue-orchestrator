"""Browser session + CSRF for the Control Center UI.

The Control API's bearer-token middleware (see ``control_api``) gates
programmatic clients, but the browser can't send an ``Authorization:
Bearer`` header on ``EventSource`` connections, and rendering the
admin token into the HTML would leak it to XSS. This module adds a
second auth path that's native to the browser:

- **Session cookie** (``io_session``, HttpOnly, SameSite=Strict,
  Path=/) — set by ``POST /login`` after the admin bearer token is
  verified. An anonymous ``GET /`` sees the login page and is
  issued no credentials. The cookie itself is opaque random data;
  server-side state tracks which session it maps to.
- **CSRF token** — a separate random value bound to the session.
  Rendered into the HTML (``<meta name="io-csrf-token" ...>``) so JS
  can read it and send it as ``X-CSRF-Token`` on every mutating
  fetch. ``SameSite=Strict`` already blocks cross-origin cookies;
  the CSRF token is defense-in-depth.
- **SSE query-string token** — ``EventSource`` can't send headers or
  forward cookies reliably in every browser, so the SSE endpoint
  accepts a short-lived HMAC-signed token in the query string that
  JS obtains via the (CSRF-protected) ``/api/sse-token`` endpoint.

All state is per-process: no persistence, no cross-restart sharing.
For a local-dev Control Center this is fine — restarts invalidate
sessions and the browser re-auths on next load.
"""

from __future__ import annotations

import hmac
import logging
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
SESSION_TTL_SECONDS = 8 * 3600
SSE_TOKEN_TTL_SECONDS = 60

_SECRET: bytes | None = None
_SESSIONS: dict[str, "_Session"] = {}
_LOCK = Lock()


@dataclass
class _Session:
    csrf_token: str
    created_at: float
    last_seen: float


def initialize(secret: bytes | None = None) -> None:
    """Initialize the process-wide HMAC secret.

    Called once at server startup (``ControlAPIServer.start`` /
    ``control_center.main``). ``secret`` is optional so tests can
    inject a known value.
    """
    global _SECRET
    _SECRET = secret or secrets.token_bytes(32)
    _SESSIONS.clear()


def is_initialized() -> bool:
    return _SECRET is not None


def shutdown() -> None:
    """Clear state. Used by tests between cases."""
    global _SECRET
    _SECRET = None
    _SESSIONS.clear()


def _expire_old(now: float) -> None:
    stale = [
        sid
        for sid, sess in _SESSIONS.items()
        if now - sess.last_seen > SESSION_TTL_SECONDS
    ]
    for sid in stale:
        _SESSIONS.pop(sid, None)


def create_session() -> tuple[str, str]:
    """Return ``(session_id, csrf_token)`` for a fresh session."""
    if _SECRET is None:
        raise RuntimeError("browser_session.initialize was never called")
    with _LOCK:
        now = time.time()
        _expire_old(now)
        session_id = secrets.token_hex(32)
        csrf_token = secrets.token_hex(32)
        _SESSIONS[session_id] = _Session(
            csrf_token=csrf_token, created_at=now, last_seen=now
        )
    return session_id, csrf_token


def get_csrf_token(session_id: str) -> str | None:
    """Return the CSRF token for ``session_id``, or ``None`` if unknown/expired.

    Touches the session's ``last_seen`` timestamp on hit so active
    sessions don't expire while they're being used.
    """
    if _SECRET is None:
        return None
    with _LOCK:
        sess = _SESSIONS.get(session_id)
        if sess is None:
            return None
        now = time.time()
        if now - sess.last_seen > SESSION_TTL_SECONDS:
            _SESSIONS.pop(session_id, None)
            return None
        sess.last_seen = now
        return sess.csrf_token


def verify_csrf(session_id: str, provided: str | None) -> bool:
    """Constant-time comparison of a submitted CSRF token."""
    if not provided:
        return False
    expected = get_csrf_token(session_id)
    if expected is None:
        return False
    return hmac.compare_digest(expected, provided)


def session_is_valid(session_id: str) -> bool:
    """Whether ``session_id`` is known and unexpired."""
    return get_csrf_token(session_id) is not None


def issue_sse_token(session_id: str) -> str | None:
    """Return a short-lived signed SSE token bound to ``session_id``.

    Format: ``"{session_id}:{timestamp}:{nonce}:{hex_signature}"``.
    """
    if _SECRET is None or not session_is_valid(session_id):
        return None
    now = int(time.time())
    nonce = secrets.token_hex(8)
    payload = f"{session_id}:{now}:{nonce}".encode("utf-8")
    sig = hmac.new(_SECRET, payload, sha256).hexdigest()
    return f"{session_id}:{now}:{nonce}:{sig}"


def verify_sse_token(token: str | None, expected_session_id: str) -> bool:
    """Validate an SSE query-string token for this session.

    Must match ``expected_session_id`` (so a token issued for session A
    can't reach session B's stream), must be signed with the current
    process secret, and must be less than ``SSE_TOKEN_TTL_SECONDS``
    old.
    """
    if not token or _SECRET is None:
        return False
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
    if time.time() - ts > SSE_TOKEN_TTL_SECONDS:
        return False
    if time.time() - ts < -5:  # clock skew tolerance
        return False
    payload = f"{sid}:{ts_str}:{nonce}".encode("utf-8")
    expected_sig = hmac.new(_SECRET, payload, sha256).hexdigest()
    return hmac.compare_digest(expected_sig, sig)
