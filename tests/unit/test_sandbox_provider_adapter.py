"""Unit tests for the provider sandbox adapters (ADR-0034).

Covers the claude-code translation of a :class:`SandboxScope` into settings +
CLI flags, the codex fail-loud stub, and the ``build_command`` integration
including the byte-for-byte regression guard when no scope is supplied.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.domain.sandbox_scope import SandboxScope
from issue_orchestrator.execution.agent_runner_providers.claude import ClaudeCodeProvider
from issue_orchestrator.execution.agent_runner_providers.codex import CodexProvider
from issue_orchestrator.execution.agent_runner_providers.sandbox import (
    MODEL_ONLY_ALLOWED_DOMAINS,
    ClaudeSandboxAdapter,
    CodexSandboxAdapter,
    ProviderSandboxAdapter,
    build_claude_sandbox_argv,
    build_claude_sandbox_settings,
)


def _scope(egress: str = "model-only") -> SandboxScope:
    return SandboxScope(
        read_roots=(Path("/wt/issue-42"),),
        write_roots=(Path("/wt/issue-42"),),
        egress=egress,  # type: ignore[arg-type]
        deny_env=("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY"),
    )


# ---------------------------------------------------------------------------
# Claude settings translation
# ---------------------------------------------------------------------------


def test_claude_settings_enable_real_sandbox() -> None:
    settings = build_claude_sandbox_settings(_scope())
    sandbox = settings["sandbox"]
    assert sandbox["enabled"] is True
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False


def test_claude_settings_bound_read_and_write_roots() -> None:
    settings = build_claude_sandbox_settings(_scope())
    fs = settings["sandbox"]["filesystem"]
    assert fs["allowRead"] == ["/wt/issue-42"]
    assert fs["allowWrite"] == ["/wt/issue-42"]


def test_claude_settings_deny_credentials_as_objects() -> None:
    settings = build_claude_sandbox_settings(_scope())
    env_vars = settings["sandbox"]["credentials"]["envVars"]
    assert {"name": "GITHUB_TOKEN", "mode": "deny"} in env_vars
    assert {"name": "AWS_SECRET_ACCESS_KEY", "mode": "deny"} in env_vars


def test_model_only_egress_allows_model_and_github_domains() -> None:
    settings = build_claude_sandbox_settings(_scope("model-only"))
    assert settings["sandbox"]["network"]["allowedDomains"] == list(MODEL_ONLY_ALLOWED_DOMAINS)


def test_model_only_egress_denies_web_search_and_curl() -> None:
    settings = build_claude_sandbox_settings(_scope("model-only"))
    deny = settings["permissions"]["deny"]
    assert "WebSearch" in deny
    assert "WebFetch" in deny
    assert "Bash(curl *)" in deny


def test_model_web_egress_has_no_network_or_tool_restriction() -> None:
    settings = build_claude_sandbox_settings(_scope("model+web"))
    assert "network" not in settings["sandbox"]
    assert "permissions" not in settings


def test_none_egress_allows_only_model_api() -> None:
    settings = build_claude_sandbox_settings(_scope("none"))
    assert settings["sandbox"]["network"]["allowedDomains"] == ["api.anthropic.com"]
    assert "WebSearch" in settings["permissions"]["deny"]


# ---------------------------------------------------------------------------
# Claude argv translation
# ---------------------------------------------------------------------------


def test_claude_argv_uses_dontask_not_bypass() -> None:
    argv = build_claude_sandbox_argv(_scope())
    assert argv[:2] == ["--permission-mode", "dontAsk"]
    assert "bypassPermissions" not in argv


def test_claude_argv_carries_inline_settings_json() -> None:
    argv = build_claude_sandbox_argv(_scope())
    settings_idx = argv.index("--settings")
    payload = json.loads(argv[settings_idx + 1])
    assert payload["sandbox"]["enabled"] is True
    assert payload["sandbox"]["filesystem"]["allowWrite"] == ["/wt/issue-42"]


def test_claude_adapter_implements_port() -> None:
    adapter = ClaudeSandboxAdapter()
    assert isinstance(adapter, ProviderSandboxAdapter)
    assert adapter.apply_scope(_scope()) == build_claude_sandbox_argv(_scope())


# ---------------------------------------------------------------------------
# Codex stub — fail loud
# ---------------------------------------------------------------------------


def test_codex_adapter_is_a_stub() -> None:
    adapter = CodexSandboxAdapter()
    assert isinstance(adapter, ProviderSandboxAdapter)
    with pytest.raises(NotImplementedError, match="Codex sandbox-scope translation"):
        adapter.apply_scope(_scope())


def test_codex_build_command_fails_loud_when_scoped() -> None:
    with pytest.raises(NotImplementedError, match="Codex sandbox-scope translation"):
        CodexProvider().build_command(prompt="task", model=None, sandbox_scope=_scope())


# ---------------------------------------------------------------------------
# Claude build_command integration + byte-for-byte OFF regression guard
# ---------------------------------------------------------------------------


def test_build_command_none_scope_is_byte_for_byte_unchanged() -> None:
    provider = ClaudeCodeProvider()
    without_param = provider.build_command(
        prompt="task", model="sonnet", permission_mode="bypassPermissions", system_prompt="SP"
    )
    with_none = provider.build_command(
        prompt="task",
        model="sonnet",
        sandbox_scope=None,
        permission_mode="bypassPermissions",
        system_prompt="SP",
    )
    assert without_param == with_none
    assert "--permission-mode" in with_none
    assert with_none[with_none.index("--permission-mode") + 1] == "bypassPermissions"


def test_build_command_with_scope_replaces_bypass_with_dontask() -> None:
    cmd = ClaudeCodeProvider().build_command(
        prompt="task",
        model="sonnet",
        sandbox_scope=_scope(),
        permission_mode="bypassPermissions",  # must be overridden by the scope
    )
    assert "bypassPermissions" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "--settings" in cmd
