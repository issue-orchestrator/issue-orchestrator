"""Tests for the browser session + CSRF + SSE-token module.

See security #6017 re-review P3 on #6011. The Control Center UI
needs a browser-native auth path parallel to the bearer token;
``infra.browser_session`` owns the state and these tests pin its
invariants.
"""

from __future__ import annotations

import time

import pytest

from issue_orchestrator.infra import browser_session


@pytest.fixture(autouse=True)
def clean_session_module():
    browser_session.shutdown()
    browser_session.initialize(secret=b"test-secret")
    yield
    browser_session.shutdown()


def test_create_session_returns_distinct_values() -> None:
    sid_a, csrf_a = browser_session.create_session()
    sid_b, csrf_b = browser_session.create_session()

    assert sid_a != sid_b
    assert csrf_a != csrf_b
    assert len(sid_a) == 64
    assert len(csrf_a) == 64


def test_get_csrf_token_returns_none_for_unknown_session() -> None:
    assert browser_session.get_csrf_token("unknown") is None


def test_get_csrf_token_returns_the_created_value() -> None:
    sid, csrf = browser_session.create_session()

    assert browser_session.get_csrf_token(sid) == csrf


def test_verify_csrf_accepts_match() -> None:
    sid, csrf = browser_session.create_session()

    assert browser_session.verify_csrf(sid, csrf) is True


def test_verify_csrf_rejects_mismatch() -> None:
    sid, _ = browser_session.create_session()

    assert browser_session.verify_csrf(sid, "wrong-value") is False


def test_verify_csrf_rejects_missing() -> None:
    sid, _ = browser_session.create_session()

    assert browser_session.verify_csrf(sid, None) is False
    assert browser_session.verify_csrf(sid, "") is False


def test_session_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    sid, csrf = browser_session.create_session()
    # Simulate last_seen drifting past TTL without touching the session.
    real_now = time.time()
    monkeypatch.setattr(
        browser_session.time,
        "time",
        lambda: real_now + browser_session.SESSION_TTL_SECONDS + 1,
    )

    assert browser_session.get_csrf_token(sid) is None
    assert browser_session.verify_csrf(sid, csrf) is False


def test_issue_sse_token_verifies_on_same_session() -> None:
    sid, _ = browser_session.create_session()

    tok = browser_session.issue_sse_token(sid)

    assert tok is not None
    assert browser_session.verify_sse_token(tok, sid) is True


def test_sse_token_rejected_for_other_session() -> None:
    sid_a, _ = browser_session.create_session()
    sid_b, _ = browser_session.create_session()

    tok = browser_session.issue_sse_token(sid_a)

    assert tok is not None
    assert browser_session.verify_sse_token(tok, sid_b) is False


def test_sse_token_expires() -> None:
    sid, _ = browser_session.create_session()
    tok = browser_session.issue_sse_token(sid)
    assert tok is not None
    parts = tok.split(":")
    # Rewrite the timestamp to be well past TTL.
    stale_ts = int(time.time()) - browser_session.SSE_TOKEN_TTL_SECONDS - 10
    stale_parts = [parts[0], str(stale_ts), parts[2], parts[3]]
    stale_tok = ":".join(stale_parts)

    assert browser_session.verify_sse_token(stale_tok, sid) is False


def test_sse_token_tampered_signature_rejected() -> None:
    sid, _ = browser_session.create_session()
    tok = browser_session.issue_sse_token(sid)
    assert tok is not None
    tampered = tok[:-4] + "dead"

    assert browser_session.verify_sse_token(tampered, sid) is False


def test_sse_token_cannot_be_verified_after_module_reinit() -> None:
    """A process restart (new HMAC secret) invalidates outstanding tokens."""
    sid, _ = browser_session.create_session()
    tok = browser_session.issue_sse_token(sid)
    assert tok is not None

    # Simulate a process restart: new secret, no sessions.
    browser_session.shutdown()
    browser_session.initialize(secret=b"different-secret")

    assert browser_session.verify_sse_token(tok, sid) is False


def test_issue_sse_token_returns_none_for_unknown_session() -> None:
    assert browser_session.issue_sse_token("never-created") is None


def test_initialize_requires_explicit_call_for_token_issuance() -> None:
    browser_session.shutdown()
    with pytest.raises(RuntimeError):
        browser_session.create_session()


# ---------------------------------------------------------------------------
# SSE token single-use semantics — #6017 re-review-3 P2.
# ---------------------------------------------------------------------------


def test_sse_token_cannot_be_replayed() -> None:
    """A valid token is consumed on first verify; second use fails.

    Pins single-use semantics so a token leaked via access log,
    browser history, or ``Referer`` cannot be replayed within its
    TTL.
    """
    sid, _ = browser_session.create_session()
    tok = browser_session.issue_sse_token(sid)
    assert tok is not None

    assert browser_session.verify_sse_token(tok, sid) is True
    assert browser_session.verify_sse_token(tok, sid) is False


def test_consumed_nonce_does_not_affect_fresh_tokens() -> None:
    """Consuming one token does not block other tokens for the same session."""
    sid, _ = browser_session.create_session()
    tok_a = browser_session.issue_sse_token(sid)
    tok_b = browser_session.issue_sse_token(sid)
    assert tok_a and tok_b and tok_a != tok_b

    assert browser_session.verify_sse_token(tok_a, sid) is True
    assert browser_session.verify_sse_token(tok_b, sid) is True


def test_consumed_nonces_expire_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consumed-nonce table trims entries older than the TTL so the
    dict does not grow unbounded.
    """
    sid, _ = browser_session.create_session()
    tok = browser_session.issue_sse_token(sid)
    assert tok is not None

    assert browser_session.verify_sse_token(tok, sid) is True
    nonce = tok.split(":")[2]
    assert nonce in browser_session._CONSUMED_SSE_NONCES  # noqa: SLF001

    real_now = time.time()
    monkeypatch.setattr(
        browser_session.time,
        "time",
        lambda: real_now + browser_session.SSE_TOKEN_TTL_SECONDS + 10,
    )
    # A second verify (even on a new token) triggers the expiry sweep.
    other_sid, _ = browser_session.create_session()
    other_tok = browser_session.issue_sse_token(other_sid)
    assert other_tok is not None
    browser_session.verify_sse_token(other_tok, other_sid)

    assert nonce not in browser_session._CONSUMED_SSE_NONCES  # noqa: SLF001
