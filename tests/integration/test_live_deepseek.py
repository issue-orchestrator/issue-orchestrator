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


@requires_key
@pytest.mark.skipif(
    shutil.which("claude") is None, reason="claude CLI not installed"
)
def test_the_cli_routes_to_deepseek_rather_than_an_existing_claude_login() -> None:
    """The failure mode that would make this mode a lie.

    DeepSeek runs THROUGH the Claude Code CLI. If the CLI prefers an operator's
    existing claude.ai subscription over the injected key and base URL, a
    "DeepSeek" session would silently bill Anthropic quota — passing every unit
    test, drawing down the wrong lane, and invalidating the whole reason
    ``deepseek`` is a separate provider.

    So this launches the real CLI with the real injected environment and asks
    the model which family it belongs to.
    """
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
            "Name the company that trained you. Reply with one word.",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )

    assert result.returncode == 0, (
        f"claude CLI failed against the DeepSeek endpoint "
        f"(exit {result.returncode}): {result.stderr[:600]}"
    )
    answer = result.stdout.lower()
    assert "deepseek" in answer, (
        "the CLI did not reach DeepSeek — it most likely used the operator's "
        f"claude.ai login instead. Model replied: {result.stdout[:300]!r}"
    )


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
