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
    cookie_a, csrf_a = browser_session.create_session()
    cookie_b, csrf_b = browser_session.create_session()

    # Distinct sessions get distinct cookies and distinct CSRFs (the
    # underlying random ``session_id`` differs).
    assert cookie_a != cookie_b
    assert csrf_a != csrf_b
    # Cookie format: ``{session_id}.{issued_at}.{hmac}``. Each piece
    # is 32+ hex chars except the unix timestamp.
    assert cookie_a.count(".") == 2
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


class TestInitializeConfig:
    """Config + env-var knobs (#6017 re-review-3 ttl config)."""

    def test_yaml_config_overrides_defaults(self) -> None:
        browser_session.shutdown()
        browser_session.initialize(
            secret=b"s",
            session_ttl_seconds=1234,
            sse_token_ttl_seconds=17,
            max_sessions=42,
        )

        assert browser_session.SESSION_TTL_SECONDS == 1234
        assert browser_session.SSE_TOKEN_TTL_SECONDS == 17
        assert browser_session.MAX_SESSIONS == 42

    def test_env_var_overrides_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        browser_session.shutdown()
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_SESSION_TTL_SECONDS", "900")
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_SSE_TOKEN_TTL_SECONDS", "10")
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_MAX_SESSIONS", "7")

        browser_session.initialize(
            secret=b"s",
            session_ttl_seconds=1234,
            sse_token_ttl_seconds=17,
            max_sessions=42,
        )

        assert browser_session.SESSION_TTL_SECONDS == 900
        assert browser_session.SSE_TOKEN_TTL_SECONDS == 10
        assert browser_session.MAX_SESSIONS == 7

    def test_invalid_env_value_falls_back_to_yaml(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        browser_session.shutdown()
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_SESSION_TTL_SECONDS", "not-a-number")

        with caplog.at_level(
            "WARNING", logger="issue_orchestrator.infra.browser_session"
        ):
            browser_session.initialize(
                secret=b"s", session_ttl_seconds=555
            )

        assert browser_session.SESSION_TTL_SECONDS == 555
        assert any(
            "Ignoring invalid" in record.getMessage()
            for record in caplog.records
        )


def test_sessions_have_no_in_memory_cap_with_stateless_cookies() -> None:
    """Stateless cookies replaced the in-memory ``_SESSIONS`` table, so
    ``MAX_SESSIONS`` is no longer an enforced cap — there's nothing
    in memory to bound. The constant is still exposed for
    back-compat with operator YAML and the settings dashboard, but
    creating many sessions cannot evict earlier ones.
    """
    cookies = [browser_session.create_session()[0] for _ in range(8)]
    # All eight remain valid — none were evicted.
    assert all(browser_session.session_is_valid(c) for c in cookies)


def test_cross_process_validating_process_owns_ttl_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for #6065 re-review-1 P2.

    A cookie minted by a process configured with a long TTL must
    still expire on a process that's configured with a shorter TTL.
    Otherwise an operator that locks the dashboard down to a 60-second
    session can be defeated by logging in on the Control Center first
    (which defaults to 8h) — the same ``ui.browser_session.ttl_seconds``
    rule enforced differently by path.
    """
    admin = "shared-admin-cross-process-ttl-token"

    # CC-like process: 1-hour session.
    browser_session.shutdown()
    browser_session.initialize(admin_token=admin, session_ttl_seconds=3600)
    cookie, _ = browser_session.create_session()
    assert browser_session.session_is_valid(cookie)

    # Dashboard-like process: 60-second session, same admin token.
    browser_session.shutdown()
    browser_session.initialize(admin_token=admin, session_ttl_seconds=60)
    # Right after the simulated mint the cookie is still inside the
    # dashboard's TTL window.
    assert browser_session.session_is_valid(cookie)

    # Advance time 61 seconds. Local TTL applies — cookie rejected.
    real_now = time.time()
    monkeypatch.setattr(time, "time", lambda: real_now + 61)
    assert not browser_session.session_is_valid(cookie)


def test_cross_process_cookie_validates_with_same_admin_token() -> None:
    """Two ``initialize`` calls with the same admin token derive the
    same secret, so a cookie minted by one accepts in the other.

    This is the contract the Control Center and Web Dashboard rely
    on — they live in separate processes but load the same
    ``~/.issue-orchestrator/api-token``.
    """
    admin = "shared-admin-token-of-decent-length"

    browser_session.shutdown()
    browser_session.initialize(admin_token=admin)
    cookie, csrf = browser_session.create_session()
    assert browser_session.session_is_valid(cookie)
    assert browser_session.verify_csrf(cookie, csrf) is True

    # Simulate a second process: re-initialize from scratch with the
    # same admin token. The cookie minted before must still validate.
    browser_session.shutdown()
    browser_session.initialize(admin_token=admin)
    assert browser_session.session_is_valid(cookie)
    assert browser_session.get_csrf_token(cookie) == csrf
    assert browser_session.verify_csrf(cookie, csrf) is True


def test_cross_process_cookie_rejected_after_admin_token_rotation() -> None:
    """Rotating the admin token rotates the derived secret, which
    invalidates every existing cookie at once. That's the operator
    kill-switch that replaces server-side revocation.
    """
    browser_session.shutdown()
    browser_session.initialize(admin_token="original-admin-token")
    cookie, _ = browser_session.create_session()

    browser_session.shutdown()
    browser_session.initialize(admin_token="rotated-admin-token")
    assert not browser_session.session_is_valid(cookie)


def test_cookie_with_tampered_signature_is_rejected() -> None:
    cookie, _ = browser_session.create_session()
    session_id, expiry, sig = cookie.split(".")
    # Flip the last hex char of the signature.
    flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    tampered = ".".join([session_id, expiry, flipped])
    assert not browser_session.session_is_valid(tampered)


def test_cookie_with_tampered_issued_at_is_rejected() -> None:
    cookie, _ = browser_session.create_session()
    session_id, issued_at, sig = cookie.split(".")
    # Forward-date the issue time without re-signing.
    extended = ".".join(
        [session_id, str(int(issued_at) + 10_000_000), sig]
    )
    assert not browser_session.session_is_valid(extended)


def test_cookie_expires_when_timestamp_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cookie, _ = browser_session.create_session()
    real_now = time.time()
    # Push current time past the cookie's expiry.
    monkeypatch.setattr(
        time,
        "time",
        lambda: real_now + browser_session.SESSION_TTL_SECONDS + 1,
    )
    assert not browser_session.session_is_valid(cookie)


def test_derive_secret_is_domain_separated() -> None:
    """The secret derived for browser sessions must not be the same as
    the raw admin token bytes — domain separation prevents the secret
    from colliding with any other derivative use.
    """
    admin = "some-admin-token"
    derived = browser_session.derive_secret(admin)
    assert derived != admin.encode("utf-8")
    assert len(derived) == 32
    # Same input → same output (deterministic).
    assert browser_session.derive_secret(admin) == derived


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
