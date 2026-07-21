"""Launch-wiring tests for the sandbox scope (ADR-0034).

Exercises the real command-building seam every launcher funnels through
(``AgentConfig.get_command`` / ``get_command_for_prompt``): opted-out sessions
build the exact command as before; opted-in sessions apply the sandbox.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from issue_orchestrator.domain.models import AgentConfig


def _agent(*, sandbox: bool) -> AgentConfig:
    return AgentConfig(
        prompt_path=Path(".prompts/backend.md"),
        prompt_relative=".prompts/backend.md",
        provider="claude-code",
        model="sonnet",
        provider_args={"permission_mode": "bypassPermissions"},
        sandbox=sandbox,
    )


def _settings_from_command(command: str) -> dict:
    tokens = shlex.split(command)
    assert "--settings" in tokens, command
    return json.loads(tokens[tokens.index("--settings") + 1])


# ---------------------------------------------------------------------------
# Opted-out: byte-for-byte unchanged
# ---------------------------------------------------------------------------


def test_opted_out_command_keeps_bypass_and_no_sandbox() -> None:
    cmd = _agent(sandbox=False).get_command_for_prompt(
        "do the work",
        worktree=Path("/wt/issue-42"),
        task_kind="code",
        prompt_file=".prompts/backend.md",
    )
    assert "--permission-mode bypassPermissions" in cmd
    assert "dontAsk" not in cmd
    assert "--settings" not in cmd


def test_opted_out_command_is_stable_across_calls() -> None:
    agent = _agent(sandbox=False)
    kwargs = dict(worktree=Path("/wt/issue-42"), task_kind="code", prompt_file=".prompts/backend.md")
    first = agent.get_command_for_prompt("do the work", **kwargs)
    second = agent.get_command_for_prompt("do the work", **kwargs)
    assert first == second
    # The sandbox machinery is fully inert on the opt-out path.
    assert "sandbox" not in first.lower()


def test_opted_out_get_command_review_path_unchanged() -> None:
    # The review launcher uses get_command(); it must be unaffected when off.
    cmd = _agent(sandbox=False).get_command(
        issue_number=9,
        issue_title="Review PR #5",
        worktree=Path("/wt/issue-9"),
        pr_number=5,
        task_kind="review",
    )
    assert "--permission-mode bypassPermissions" in cmd
    assert "dontAsk" not in cmd


# ---------------------------------------------------------------------------
# Opted-in: sandbox applied
# ---------------------------------------------------------------------------


def test_opted_in_coder_command_applies_sandbox() -> None:
    worktree = Path("/wt/issue-42")
    cmd = _agent(sandbox=True).get_command_for_prompt(
        "do the work",
        worktree=worktree,
        task_kind="code",
        prompt_file=".prompts/backend.md",
    )
    assert "--permission-mode dontAsk" in cmd
    assert "bypassPermissions" not in cmd

    settings = _settings_from_command(cmd)
    assert settings["sandbox"]["filesystem"]["allowWrite"] == [str(worktree)]
    assert settings["sandbox"]["enabled"] is True
    assert {"name": "GITHUB_TOKEN", "mode": "deny"} in settings["sandbox"]["credentials"]["envVars"]
    assert "WebSearch" in settings["permissions"]["deny"]


def test_opted_in_reviewer_command_applies_sandbox() -> None:
    worktree = Path("/wt/issue-9")
    cmd = _agent(sandbox=True).get_command(
        issue_number=9,
        issue_title="Review PR #5",
        worktree=worktree,
        pr_number=5,
        task_kind="review",
    )
    assert "--permission-mode dontAsk" in cmd
    settings = _settings_from_command(cmd)
    assert settings["sandbox"]["filesystem"]["allowRead"] == [str(worktree)]


def test_opted_in_worktree_path_flows_into_settings() -> None:
    worktree = Path("/tmp/wt/issue-777-abc")
    cmd = _agent(sandbox=True).get_command_for_prompt(
        "do the work",
        worktree=worktree,
        task_kind="code",
        prompt_file=".prompts/backend.md",
    )
    # The exact worktree the launcher created is the sandbox boundary.
    assert re.search(re.escape(str(worktree)), cmd)
    settings = _settings_from_command(cmd)
    assert str(worktree) in settings["sandbox"]["filesystem"]["allowWrite"]


def test_provider_less_sandbox_optin_fails_closed() -> None:
    # sandbox: true on a provider-less / custom-command agent cannot be enforced,
    # so the command seam must RAISE rather than silently render an unsandboxed
    # command (the security opt-in must fail loud).
    from issue_orchestrator.domain.sandbox_scope import SandboxUnsupportedError

    agent = AgentConfig(
        prompt_path=Path(".prompts/backend.md"),
        prompt_relative=".prompts/backend.md",
        provider=None,
        sandbox=True,
    )
    with pytest.raises(SandboxUnsupportedError):
        agent.get_command_for_prompt(
            "do the work",
            worktree=Path("/wt/issue-1"),
            task_kind="code",
            prompt_file=".prompts/backend.md",
        )


def test_provider_less_without_sandbox_is_unchanged() -> None:
    # The legacy-template path is byte-for-byte unchanged when not opted in.
    agent = AgentConfig(
        prompt_path=Path(".prompts/backend.md"),
        prompt_relative=".prompts/backend.md",
        provider=None,
        sandbox=False,
    )
    cmd = agent.get_command_for_prompt(
        "do the work",
        worktree=Path("/wt/issue-1"),
        task_kind="code",
        prompt_file=".prompts/backend.md",
    )
    assert "--settings" not in cmd
    assert "dontAsk" not in cmd


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
