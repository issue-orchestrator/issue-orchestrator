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
    MODEL_API_DOMAINS,
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
        deny_read_files=("~/.ssh", "~/.issue-orchestrator"),
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


def test_claude_settings_deny_read_home_and_reallow_roots() -> None:
    # Reads are OPEN by default; the boundary is denyRead ~/ + re-allow roots.
    fs = build_claude_sandbox_settings(_scope())["sandbox"]["filesystem"]
    assert fs["denyRead"] == ["~/"]
    assert fs["allowRead"] == ["/wt/issue-42"]


def test_claude_settings_fail_closed_deny_secret_files() -> None:
    # The narrow, un-widenable credential deny is the fail-closed secret layer.
    files = build_claude_sandbox_settings(_scope())["sandbox"]["credentials"]["files"]
    assert {"path": "~/.ssh", "mode": "deny"} in files
    assert {"path": "~/.issue-orchestrator", "mode": "deny"} in files


def test_claude_settings_deny_credentials_as_objects() -> None:
    settings = build_claude_sandbox_settings(_scope())
    env_vars = settings["sandbox"]["credentials"]["envVars"]
    assert {"name": "GITHUB_TOKEN", "mode": "deny"} in env_vars
    assert {"name": "AWS_SECRET_ACCESS_KEY", "mode": "deny"} in env_vars


def test_model_only_egress_allows_only_model_api_not_github() -> None:
    # A broad github wildcard is an exfil risk; model-only pre-allows only the
    # model API host for Bash. (Bash reaching it is moot; it documents the floor.)
    domains = build_claude_sandbox_settings(_scope("model-only"))["sandbox"]["network"][
        "allowedDomains"
    ]
    assert domains == list(MODEL_API_DOMAINS)
    assert not any("github" in d for d in domains)


def test_model_only_egress_denies_web_search_and_curl() -> None:
    settings = build_claude_sandbox_settings(_scope("model-only"))
    deny = settings["permissions"]["deny"]
    assert "WebSearch" in deny
    assert "WebFetch" in deny
    assert "Bash(curl *)" in deny


def test_model_web_egress_has_no_network_restriction_but_keeps_secret_denies() -> None:
    # model+web drops the OS network allowlist and the WEB tool denies, but the
    # permission block still carries the read allow-list and the native secret
    # denies (secrets are fail-closed regardless of egress posture).
    settings = build_claude_sandbox_settings(_scope("model+web"))
    assert "network" not in settings["sandbox"]
    permissions = settings["permissions"]
    assert permissions["allow"] == ["Read", "Grep", "Glob"]
    assert "Read(~/.ssh/**)" in permissions["deny"]
    assert "WebSearch" not in permissions["deny"]
    assert "WebFetch" not in permissions["deny"]


def test_none_egress_emits_explicit_empty_allowlist() -> None:
    # "none" blocks Bash network entirely: the key is PRESENT with an empty
    # list (an omitted key would instead add no restriction).
    settings = build_claude_sandbox_settings(_scope("none"))
    assert settings["sandbox"]["network"]["allowedDomains"] == []
    assert "WebSearch" in settings["permissions"]["deny"]


# ---------------------------------------------------------------------------
# Native file-tool permission layer (the OS sandbox binds Bash only)
# ---------------------------------------------------------------------------


def test_native_read_tools_allowed_for_god_view() -> None:
    # dontAsk runs only allow-listed tools; the native read tools must be
    # allowed or a sandboxed agent could not read anything.
    allow = build_claude_sandbox_settings(_scope())["permissions"]["allow"]
    assert allow == ["Read", "Grep", "Glob"]


def test_native_file_tools_deny_each_secret_path() -> None:
    # The OS credentials.files deny does not touch the native Read/Edit/... tools,
    # so every secret path is ALSO denied on the permission layer, per tool, for
    # both the path and everything under it.
    deny = build_claude_sandbox_settings(_scope())["permissions"]["deny"]
    for path in ("~/.ssh", "~/.issue-orchestrator"):
        for tool in ("Read", "Edit", "Write", "Grep", "Glob"):
            assert f"{tool}({path})" in deny, f"missing {tool}({path})"
            assert f"{tool}({path}/**)" in deny, f"missing {tool}({path}/**)"


def test_native_deny_precedes_egress_deny() -> None:
    # Deterministic ordering: native secret denies first, egress tool denies last.
    deny = build_claude_sandbox_settings(_scope("model-only"))["permissions"]["deny"]
    assert deny.index("Read(~/.ssh)") < deny.index("WebSearch")


def test_absolute_secret_path_uses_double_slash_permission_spec() -> None:
    # Read/Edit permission specifiers use // for absolute paths, which differs
    # from the single-slash sandbox.filesystem convention. An operator/test path
    # outside home must be doubled or it would read as project-relative.
    scope = SandboxScope(
        read_roots=(Path("/wt/x"),),
        write_roots=(Path("/wt/x"),),
        egress="model-only",
        deny_env=(),
        deny_read_files=("/var/folders/abc/planted-secret.txt",),
    )
    deny = build_claude_sandbox_settings(scope)["permissions"]["deny"]
    assert "Read(//var/folders/abc/planted-secret.txt)" in deny
    # The single-slash sandbox.filesystem credentials.files keeps the raw path.
    files = build_claude_sandbox_settings(scope)["sandbox"]["credentials"]["files"]
    assert {"path": "/var/folders/abc/planted-secret.txt", "mode": "deny"} in files


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
