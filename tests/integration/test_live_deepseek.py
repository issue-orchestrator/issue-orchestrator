"""LIVE DeepSeek smoke tests — real API, real spend (#7253).

Excluded from every PR validation lane by the ``live_deepseek`` marker, exactly
as ``live_agent`` and ``live_codex`` are, and collected by the budgeted
live-agents lane instead. Nothing here runs on the gate.

These exist because the DeepSeek integration rests on assumptions that only a
real call can settle, and each one is a silent failure if wrong:

* that DeepSeek's Anthropic-compatible endpoint accepts the environment our
  provider builds (base URL + ``ANTHROPIC_API_KEY``),
* that the model slugs we ship in ``modes/deepseek/main.yaml`` are real,
* that the Claude Code CLI actually routes to DeepSeek when pointed at it
  rather than silently preferring an existing claude.ai login — the failure
  that would make a "DeepSeek" mode quietly bill Anthropic instead.

Cost: a handful of tokens per test. DeepSeek is pay-as-you-go and the balance
is the only spend cap, so the prompts here are deliberately tiny.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

import pytest

from issue_orchestrator.execution.agent_runner_providers import DeepSeekProvider
from issue_orchestrator.infra.ai_keys import read_ai_key

pytestmark = [pytest.mark.live_deepseek, pytest.mark.live_agent]

SMOKE_MODEL = "deepseek-flash"  # the cheaper of the two shipped slugs


def _api_key() -> str | None:
    return read_ai_key(DeepSeekProvider.API_KEY_NAME)


requires_key = pytest.mark.skipif(
    _api_key() is None,
    reason=(
        f"no {DeepSeekProvider.API_KEY_NAME} configured — "
        "run `issue-orchestrator keys set deepseek`"
    ),
)


@requires_key
def test_the_endpoint_accepts_the_environment_our_provider_builds() -> None:
    """The base URL, auth header and model slug we ship actually work.

    Asserts the contract at its narrowest point: if DeepSeek renames a model or
    moves the Anthropic-compatible path, this fails with their message rather
    than as a mysterious dead agent session twenty minutes into a run.
    """
    provider = DeepSeekProvider()
    env = provider.session_env(secrets={provider.API_KEY_NAME: _api_key() or ""})

    request = urllib.request.Request(
        f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
        data=json.dumps(
            {
                "model": SMOKE_MODEL,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with the word OK."}],
            }
        ).encode(),
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": env["ANTHROPIC_API_KEY"],
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:  # pragma: no cover - live failure path
        pytest.fail(
            f"DeepSeek rejected the configuration this repo ships: "
            f"HTTP {exc.code} {exc.read()[:400]!r}"
        )

    assert payload.get("content"), f"no content in DeepSeek reply: {payload}"
    # The reply is the model's, so its wording is NOT pinned. What must hold is
    # that DeepSeek served it: a response echoing an Anthropic model name would
    # mean the request never left for DeepSeek at all.
    assert "deepseek" in str(payload.get("model", "")).lower(), (
        f"expected a DeepSeek model in the reply, got {payload.get('model')!r} — "
        "the request may have been routed to Anthropic instead"
    )


@pytest.mark.skipif(
    shutil.which("claude") is None, reason="claude CLI not installed"
)
def test_the_cli_sends_the_request_where_the_provider_points_it() -> None:
    """The failure mode that would make this mode a lie, proved hermetically.

    DeepSeek runs THROUGH the Claude Code CLI. If the CLI preferred an
    operator's existing claude.ai subscription over the injected base URL, a
    "DeepSeek" session would silently bill Anthropic quota while passing every
    unit test.

    Two oracles were tried and rejected before this one:

    * Asking the model who trained it. Claude Code injects a system prompt
      telling the model it is Claude Code, so it answers "Anthropic" no matter
      who served the request. That reported a routing bug that did not exist.
    * Sending a deliberately invalid credential and reading the rejection.
      Without a TTY the CLI retries an auth failure instead of returning, so the
      test hung for its full timeout rather than failing.

    So the request is pointed at a local server this test owns. If the CLI
    honours the provider's base URL the server receives the call; if it falls
    back to the claude.ai login the server hears nothing. No spend, no external
    dependency, and no reliance on how a failure is reported.
    """
    import http.server
    import threading

    received: list[tuple[str, str]] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server's required spelling
            received.append((self.path, self.headers.get("x-api-key", "")))
            body = b'{"error":{"type":"invalid_request_error","message":"stub"}}'
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep pytest output clean
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    provider = DeepSeekProvider()
    env = dict(os.environ)
    env.update(provider.session_env(secrets={provider.API_KEY_NAME: "sk-routing-probe"}))
    # Same shape the provider builds, pointed at this test's server.
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"

    try:
        subprocess.run(
            [
                provider.executable,
                "--model",
                SMOKE_MODEL,
                "--permission-mode",
                "bypassPermissions",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--strict-mcp-config",
                "-p",
                "Say OK.",
            ],
            capture_output=True,
            text=True,
            timeout=90,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired:
        pass  # the CLI may retry the stub's 400; arrival is what is asserted
    finally:
        server.shutdown()

    assert received, (
        "the CLI never called the base URL the provider set, so a DeepSeek "
        "session would not reach DeepSeek — it most likely used the operator's "
        "claude.ai login instead"
    )
    assert any(key == "sk-routing-probe" for _, key in received), (
        f"the CLI reached the endpoint but did not send the injected "
        f"credential: {received!r}"
    )


@requires_key
@pytest.mark.skipif(
    shutil.which("claude") is None, reason="claude CLI not installed"
)
def test_a_real_deepseek_session_completes_through_the_cli() -> None:
    """End to end with the real key: the configuration actually does work."""
    provider = DeepSeekProvider()
    env = dict(os.environ)
    env.update(provider.session_env(secrets={provider.API_KEY_NAME: _api_key() or ""}))

    result = subprocess.run(
        [
            provider.executable,
            "--model",
            SMOKE_MODEL,
            "--permission-mode",
            "bypassPermissions",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--strict-mcp-config",
            "-p",
            "Reply with exactly: READY",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )

    assert result.returncode == 0, (
        f"claude CLI failed against DeepSeek (exit {result.returncode}): "
        f"{result.stderr[:600]}"
    )
    assert result.stdout.strip(), "DeepSeek session produced no output"
    assert "authentication fails" not in result.stdout.lower()


def test_the_context_window_is_declared_to_the_cli() -> None:
    """Claude Code assumes 200k for slugs it does not know; DeepSeek serves 1M.

    Without this the agent auto-compacts at a fifth of its real window, silently
    discarding context part-way through every long session. Asserted here rather
    than only in unit tests because the live suite is where the CLI's own
    "unrecognized_model" warning is observable.
    """
    env = DeepSeekProvider().session_env(secrets={})

    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1000000"


@requires_key
def test_an_invalid_key_is_classified_rather_than_reported_as_a_timeout() -> None:
    """A rejected credential must surface as auth, not as a dead session.

    #7096's lesson at the DeepSeek boundary: a provider failure that is not
    classified gets recorded as TIMED_OUT, which sends the retry ladder and the
    circuit chasing an outage that is really a bad credential.
    """
    from issue_orchestrator.execution.agent_runner_errors import (
        classify_provider_output,
    )
    from issue_orchestrator.ports.provider_resilience import ProviderErrorType

    provider = DeepSeekProvider()
    request = urllib.request.Request(
        f"{provider.BASE_URL}/v1/messages",
        data=json.dumps(
            {
                "model": SMOKE_MODEL,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode(),
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": "sk-deliberately-invalid",
        },
    )

    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=60)

    body = caught.value.read().decode(errors="replace")
    assert caught.value.code in (401, 402, 403), (
        f"expected an auth/billing rejection, got HTTP {caught.value.code}: {body[:300]}"
    )
    assert classify_provider_output(body) is ProviderErrorType.AUTH, (
        "DeepSeek's rejection text is not recognised by the shared "
        f"classification table, so it would be recorded as a timeout: {body[:300]!r}"
    )
