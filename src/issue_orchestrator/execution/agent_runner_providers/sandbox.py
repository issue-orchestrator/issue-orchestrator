"""Provider adapters that translate a :class:`SandboxScope` into CLI flags.

A :class:`~issue_orchestrator.domain.sandbox_scope.SandboxScope` is
provider-agnostic. Each AI-agent CLI enforces a sandbox differently, so the
translation lives here, next to the providers, behind the
:class:`ProviderSandboxAdapter` port.

This slice ships the **claude-code** translation. It emits a Claude Code
settings object (passed inline via ``--settings '<json>'``, matching the
existing ``--mcp-config '<json>'`` pattern the provider already uses) plus
``--permission-mode dontAsk`` — a non-yolo, still-unattended mode that denies
tools by default instead of ``bypassPermissions``. The **codex** translation is
a documented follow-up (see :class:`CodexSandboxAdapter`).

Settings schema verified against Claude Code docs (docs.claude.com — Sandbox
settings / Permissions):
- ``sandbox.enabled`` / ``failIfUnavailable`` / ``allowUnsandboxedCommands``
- ``sandbox.filesystem.allowRead[]`` / ``allowWrite[]``
- ``sandbox.network.allowedDomains[]``
- ``sandbox.credentials.envVars[]`` — objects ``{"name": ..., "mode": "deny"}``
- ``permissions.deny[]`` — ``ToolName(pattern)`` entries (e.g. ``Bash(curl *)``)
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from issue_orchestrator.domain.sandbox_scope import SandboxEgress, SandboxScope

__all__ = [
    "MODEL_ONLY_ALLOWED_DOMAINS",
    "MODEL_ONLY_DENY_TOOLS",
    "ClaudeSandboxAdapter",
    "CodexSandboxAdapter",
    "ProviderSandboxAdapter",
    "build_claude_sandbox_argv",
    "build_claude_sandbox_settings",
]

# Domains a "model-only" agent may reach: the Anthropic model API plus the
# source host (git fetch, PR/issue reads). Everything else is blocked at the OS
# network layer. This is deliberately a small, named allowlist so the flip
# slice can tune it in one place.
MODEL_ONLY_ALLOWED_DOMAINS: tuple[str, ...] = (
    "api.anthropic.com",
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
)

# Anthropic model API only — the floor every non-"model+web" posture keeps so
# the agent can still reach the model it is driven by.
_MODEL_API_DOMAINS: tuple[str, ...] = ("api.anthropic.com",)

# Tools/commands denied for restricted egress. Belt-and-suspenders alongside the
# OS-level network allowlist: no web search, no ad-hoc HTTP fetchers. Entries
# use Claude Code's ``ToolName(pattern)`` permission syntax.
MODEL_ONLY_DENY_TOOLS: tuple[str, ...] = (
    "WebSearch",
    "WebFetch",
    "Bash(curl *)",
    "Bash(wget *)",
)


@runtime_checkable
class ProviderSandboxAdapter(Protocol):
    """Port: translate a provider-agnostic scope into that provider's CLI args.

    Implementations return the argv fragment to splice into the provider's
    launch command (before the positional prompt). A provider that cannot yet
    enforce a sandbox raises :class:`NotImplementedError`.
    """

    def apply_scope(self, scope: SandboxScope) -> list[str]:
        """Return the CLI argv fragment enforcing *scope* for this provider."""
        ...


def _allowed_domains_for_egress(egress: SandboxEgress) -> tuple[str, ...]:
    if egress == "model+web":
        return ()  # empty → provider adapter omits the network allowlist
    if egress == "model-only":
        return MODEL_ONLY_ALLOWED_DOMAINS
    return _MODEL_API_DOMAINS  # "none": model API only


def _deny_tools_for_egress(egress: SandboxEgress) -> tuple[str, ...]:
    if egress == "model+web":
        return ()
    return MODEL_ONLY_DENY_TOOLS  # "model-only" and "none"


def build_claude_sandbox_settings(scope: SandboxScope) -> dict[str, Any]:
    """Pure translation of a :class:`SandboxScope` into a Claude Code settings dict.

    Extracted as a pure function so the mapping is unit-testable without
    building a full command.
    """
    sandbox: dict[str, Any] = {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "filesystem": {
            "allowRead": [str(p) for p in scope.read_roots],
            "allowWrite": [str(p) for p in scope.write_roots],
        },
        "credentials": {
            "envVars": [{"name": name, "mode": "deny"} for name in scope.deny_env],
        },
    }
    allowed_domains = _allowed_domains_for_egress(scope.egress)
    if allowed_domains:
        sandbox["network"] = {"allowedDomains": list(allowed_domains)}

    settings: dict[str, Any] = {"sandbox": sandbox}
    deny_tools = _deny_tools_for_egress(scope.egress)
    if deny_tools:
        settings["permissions"] = {"deny": list(deny_tools)}
    return settings


def build_claude_sandbox_argv(scope: SandboxScope) -> list[str]:
    """Return the claude-code argv fragment that enforces *scope*.

    Emits ``--permission-mode dontAsk`` (non-yolo, unattended, deny-by-default)
    and the sandbox settings as an inline ``--settings`` JSON string. The JSON is
    serialized with sorted keys and compact separators for deterministic output
    (so the command is stable and testable).
    """
    settings_json = json.dumps(
        build_claude_sandbox_settings(scope),
        sort_keys=True,
        separators=(",", ":"),
    )
    return [
        "--permission-mode",
        "dontAsk",
        "--settings",
        settings_json,
    ]


class ClaudeSandboxAdapter:
    """Claude Code implementation of :class:`ProviderSandboxAdapter`."""

    def apply_scope(self, scope: SandboxScope) -> list[str]:
        return build_claude_sandbox_argv(scope)


class CodexSandboxAdapter:
    """Codex placeholder implementation of :class:`ProviderSandboxAdapter`.

    Codex expresses its sandbox through ``--sandbox`` / ``--ask-for-approval``
    policy flags rather than a settings file, so the translation differs from
    claude-code's. Implementing it is the second slice of ADR-0034; until then
    an opted-in codex agent fails loudly rather than silently launching yolo.
    """

    def apply_scope(self, scope: SandboxScope) -> list[str]:
        raise NotImplementedError(
            "Codex sandbox-scope translation is not implemented yet "
            "(ADR-0034 follow-up); do not set sandbox: true on a codex agent"
        )
