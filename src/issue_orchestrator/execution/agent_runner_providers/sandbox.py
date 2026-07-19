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

FAIL-CLOSED filesystem model (verified against docs.claude.com — Sandbox):
- **Reads are OPEN to the whole machine by default.** ``filesystem.allowRead``
  does NOT bound reads; it only *re-allows* paths inside a ``denyRead`` region.
  So we DENY the home directory (``denyRead: ["~/"]``) and re-allow only the
  session's read roots within it. ``denyRead``/``allowRead`` arrays MERGE across
  settings scopes, so this is defense-in-depth, not an un-widenable boundary.
- **The fail-closed secret layer is ``credentials.files``.** ``deny`` entries
  are narrow-only and merged — any scope can add one, no scope can remove one —
  so a home-relative secret (``~/.ssh``, this tool's ``~/.issue-orchestrator``
  api-token, ...) stays unreadable even if a later scope widens ``allowRead``.
- **Writes are cwd-bounded by default;** ``allowWrite`` grants the worktree
  write roots explicitly. We do not widen it.

KNOWN LIMITATION: a GitHub *App* private key at an operator-configured absolute
path *outside* home is covered by neither ``denyRead: ["~/"]`` nor the static
``credentials.files`` list; a home-based key is covered by ``denyRead``. The
static secret list lives in the domain (``DEFAULT_SANDBOX_DENY_READ_FILES``).

Settings schema (docs.claude.com — Sandbox settings / Permissions):
- ``sandbox.enabled`` / ``failIfUnavailable`` / ``allowUnsandboxedCommands``
- ``sandbox.filesystem.denyRead[]`` / ``allowRead[]`` / ``allowWrite[]``
- ``sandbox.network.allowedDomains[]`` (omitted for ``model+web``; explicit
  empty list for ``none`` to block Bash network entirely)
- ``sandbox.credentials.files[]`` — objects ``{"path": ..., "mode": "deny"}``
- ``sandbox.credentials.envVars[]`` — objects ``{"name": ..., "mode": "deny"}``
- ``permissions.deny[]`` — ``ToolName(pattern)`` entries (e.g. ``Bash(curl *)``)
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from issue_orchestrator.domain.sandbox_scope import SandboxEgress, SandboxScope

__all__ = [
    "MODEL_API_DOMAINS",
    "MODEL_ONLY_DENY_TOOLS",
    "ClaudeSandboxAdapter",
    "CodexSandboxAdapter",
    "ProviderSandboxAdapter",
    "build_claude_sandbox_argv",
    "build_claude_sandbox_settings",
]

# The Anthropic model API host. This is the ONLY domain a restricted-egress
# ("model-only") sandbox pre-allows for Bash subprocesses. A broad ``github.com``
# entry is deliberately NOT allowed: the docs warn it is a data-exfiltration /
# domain-fronting path, and in this architecture the orchestrator (not the
# sandboxed agent) performs git pushes and PR creation, so the agent's Bash does
# not need source-host egress. NOTE: the model API is reached by the *agent
# process*, which is unsandboxed; listing it here only affects Bash subprocess
# egress (harmless, and documents the "model" floor explicitly).
MODEL_API_DOMAINS: tuple[str, ...] = ("api.anthropic.com",)

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


def _allowed_domains_for_egress(egress: SandboxEgress) -> tuple[str, ...] | None:
    """Bash-subprocess network allowlist for an egress posture.

    Returns ``None`` for ``model+web`` — the adapter OMITS the network key so the
    sandbox adds no OS-level domain restriction. Otherwise returns an explicit
    (possibly empty) tuple: ``model-only`` pre-allows just the model API host,
    and ``none`` returns ``()`` so the adapter emits an EXPLICIT empty allowlist
    (Bash reaches no network) rather than omitting the key.
    """
    if egress == "model+web":
        return None
    if egress == "model-only":
        return MODEL_API_DOMAINS
    return ()  # "none": explicit empty allowlist — no Bash network at all


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
        # Hard-fail rather than silently run unsandboxed if the sandbox can't
        # start, and refuse the ``dangerouslyDisableSandbox`` escape hatch.
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "filesystem": {
            # Reads are OPEN by default: deny the home dir and re-allow only the
            # read roots within it. This array MERGES across settings scopes, so
            # it is defense-in-depth — the fail-closed secret layer is
            # ``credentials.files`` below (deny is narrow-only, un-widenable).
            "denyRead": ["~/"],
            "allowRead": [str(p) for p in scope.read_roots],
            "allowWrite": [str(p) for p in scope.write_roots],
        },
        "credentials": {
            # Fail-closed secret protection: deny reads of known credential
            # stores AND unset credential env vars for sandboxed commands.
            "files": [
                {"path": path, "mode": "deny"} for path in scope.deny_read_files
            ],
            "envVars": [{"name": name, "mode": "deny"} for name in scope.deny_env],
        },
    }
    allowed_domains = _allowed_domains_for_egress(scope.egress)
    if allowed_domains is not None:
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
