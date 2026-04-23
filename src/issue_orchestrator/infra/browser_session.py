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
from collections import OrderedDict
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

# Upper bound on concurrent browser sessions. Practically the operator
# sees this cap only in pathological cases (a script hammering
# ``POST /login`` with the admin token to create sessions). Cheap to
# enforce and prevents the in-memory dict from growing without limit
# over a long-running process. See #6017 re-review-3 P4.
MAX_SESSIONS = 1024

_SECRET: bytes | None = None
_SESSIONS: "OrderedDict[str, _Session]" = OrderedDict()
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
    _CONSUMED_SSE_NONCES.clear()


def is_initialized() -> bool:
    return _SECRET is not None


def shutdown() -> None:
    """Clear state. Used by tests between cases."""
    global _SECRET
    _SECRET = None
    _SESSIONS.clear()
    _CONSUMED_SSE_NONCES.clear()


def _expire_old(now: float) -> None:
    stale = [
        sid
        for sid, sess in _SESSIONS.items()
        if now - sess.last_seen > SESSION_TTL_SECONDS
    ]
    for sid in stale:
        _SESSIONS.pop(sid, None)


def create_session() -> tuple[str, str]:
    """Return ``(session_id, csrf_token)`` for a fresh session.

    Maintains ``len(_SESSIONS) <= MAX_SESSIONS`` by expiring TTL-stale
    entries and, if still over the cap, evicting the
    least-recently-used entries in insertion order. ``_SESSIONS`` is
    an ``OrderedDict`` and ``get_csrf_token`` moves touched entries
    to the back, so "least recently used" is a cheap pop from the
    front.
    """
    if _SECRET is None:
        raise RuntimeError("browser_session.initialize was never called")
    with _LOCK:
        now = time.time()
        _expire_old(now)
        while len(_SESSIONS) >= MAX_SESSIONS:
            evicted_id, _ = _SESSIONS.popitem(last=False)
            logger.info(
                "browser_session: evicted LRU session %s… to stay under cap %d",
                evicted_id[:8],
                MAX_SESSIONS,
            )
        session_id = secrets.token_hex(32)
        csrf_token = secrets.token_hex(32)
        _SESSIONS[session_id] = _Session(
            csrf_token=csrf_token, created_at=now, last_seen=now
        )
    return session_id, csrf_token


def get_csrf_token(session_id: str) -> str | None:
    """Return the CSRF token for ``session_id``, or ``None`` if unknown/expired.

    Touches the session's ``last_seen`` timestamp on hit and moves the
    entry to the back of the LRU ordering so active sessions are
    never evicted while they're in use.
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
        _SESSIONS.move_to_end(session_id)
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


# Nonces from successfully-verified SSE tokens. Because each SSE token
# is single-use (#6017 re-review-3 P2), we remember the consumed
# nonces within their TTL window so a leaked ``sse_token`` in a log /
# proxy / browser history cannot be replayed later. Nonces are 8 hex
# chars, expire alongside their token, and live in a bounded dict
# trimmed on every use.
_CONSUMED_SSE_NONCES: dict[str, float] = {}


def _expire_consumed_nonces(now: float) -> None:
    stale = [
        nonce
        for nonce, seen_at in _CONSUMED_SSE_NONCES.items()
        if now - seen_at > SSE_TOKEN_TTL_SECONDS
    ]
    for nonce in stale:
        _CONSUMED_SSE_NONCES.pop(nonce, None)


def verify_sse_token(token: str | None, expected_session_id: str) -> bool:
    """Validate an SSE query-string token for this session.

    Single-use semantics: a successful verify records the token's
    nonce in ``_CONSUMED_SSE_NONCES`` for ``SSE_TOKEN_TTL_SECONDS``.
    A second call with the same token returns ``False`` — so a token
    leaked via server access logs, browser history, or a
    ``Referer`` header cannot be replayed within its TTL.

    The token must also match ``expected_session_id`` (a token for
    session A can't reach session B's stream), be signed with the
    current process secret, and be less than
    ``SSE_TOKEN_TTL_SECONDS`` old.
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
            # Replay attempt — the signature is valid but the nonce
            # has already been accepted once. Refuse.
            return False
        _CONSUMED_SSE_NONCES[nonce] = now
    return True
