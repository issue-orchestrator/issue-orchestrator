"""Provider adapters that translate a :class:`SandboxScope` into CLI flags.

A :class:`~issue_orchestrator.domain.sandbox_scope.SandboxScope` is
provider-agnostic. Each AI-agent CLI enforces a sandbox differently, so the
translation lives here, next to the providers, behind the
:class:`ProviderSandboxAdapter` port.

This slice ships the **claude-code** translation. It emits a Claude Code
settings object (passed inline via ``--settings '<json>'``, matching the
existing ``--mcp-config '<json>'`` pattern the provider already uses) plus
``--permission-mode dontAsk`` — a non-yolo, still-unattended mode that runs only
allow-listed tools and auto-denies the rest, instead of ``bypassPermissions``.

TWO ENFORCEMENT LAYERS (verified against docs.claude.com — Sandbox / Permission
modes). These are complementary and BOTH are required, because the OS sandbox
governs a different set of tools than the permission layer:

1. **OS sandbox — Bash and its child processes only.** ``sandbox.filesystem`` /
   ``credentials`` / ``network`` are enforced by the operating system (Seatbelt
   / bubblewrap) on sandboxed Bash subprocesses. They do NOT constrain Claude's
   built-in Read/Edit/Grep/Glob/Write tools ("Built-in file tools ... use the
   permission system directly rather than running through the sandbox").
2. **Permission rules — every tool, incl. the native file tools.**
   ``permissions.allow`` / ``permissions.deny`` are evaluated before any tool
   runs. Crucially, **deny rules apply in every permission mode** (even
   ``bypassPermissions``), so a ``permissions.deny`` entry is un-bypassable.

FAIL-CLOSED secret model — the same ``deny_read_files`` list is enforced on BOTH
layers so a secret is unreadable however the agent reaches for it:
- **Bash / OS layer: ``credentials.files`` deny.** ``deny`` entries are
  narrow-only and merged — any scope can add one, no scope can remove one — so a
  secret (``~/.ssh``, this tool's ``~/.issue-orchestrator`` api-token, ...) stays
  unreadable by a sandboxed ``cat`` even if a later scope widens ``allowRead``.
- **Native-tool layer: ``permissions.deny`` Read/Edit/Grep/Glob/Write.** The OS
  ``credentials.files`` deny does not touch the native ``Read`` tool, so we ALSO
  emit a permission deny for each secret path and each native file tool (each
  tool must be denied separately). This is the direct fix for a native ``Read``
  of ``~/.ssh`` / the api-token, and it holds in every permission mode.

TRUST BOUNDARY (ADR-0034 trusted-repository contract). The orchestrator's
operator selects and onboards the target repository, so its checked-in Claude
configuration (project/local ``.claude/settings.json``) plus the operator's own
user/managed settings are TRUSTED inputs — accepting workspace trust is
authorization to load them, exactly as Claude Code normally treats a trusted
workspace. ``sandbox: true`` therefore provides a provider-native, per-session
boundary *under that trusted configuration*; it is NOT an "open an arbitrary
hostile repository safely" mode (that is the separate untrusted-repository track,
optional hardening in #6861 via an external isolation substrate). This adapter
does not try to out-parse Claude's settings model; it translates the scope into
Claude's native sandbox and constrains the AGENT.

WHAT THE ADAPTER CONSTRAINS (against the agent, not the trusted repo):
- **Writes** — Bash and native ``Edit`` (which governs Edit/Write/MultiEdit) are
  allowed only within the worktree write roots; outside is denied.
- **Secrets** — denied on both layers (``credentials.files`` for Bash,
  ``permissions.deny`` for the native tools).
- **Egress** — restricted per :data:`SandboxEgress` (Bash ``allowedDomains`` +
  ``WebSearch``/``WebFetch``/``curl``/``wget`` denies).
- **Self-modification** — the agent may not rewrite its own policy: the worktree
  ``.claude/settings.json`` / ``settings.local.json`` are ``denyWrite`` (Bash) and
  ``Edit``-denied (native). Deny beats the worktree allow, so a session cannot
  hot-reload a wider policy after launch. This defends against the agent, which is
  distinct from distrusting the repository's *initial* contents.

READ POSTURE (deliberate): non-secret reads OUTSIDE the worktree remain possible
(``Read``/``Grep``/``Glob`` allowed; ``denyRead: ["~/"]`` bounds *Bash* reads as
defense-in-depth). This is not a read jail; a hard read boundary is the
whole-process track (#6861).

KNOWN LIMITATION: a GitHub *App* private key at an operator-configured absolute
path *outside* home is covered only if it is listed in ``deny_read_files``; the
static secret list lives in the domain (``DEFAULT_SANDBOX_DENY_READ_FILES``) and
a home-based key is additionally covered by the Bash-layer ``denyRead: ["~/"]``.

Settings schema (docs.claude.com — Sandbox settings / Permissions):
- ``sandbox.enabled`` / ``failIfUnavailable`` / ``allowUnsandboxedCommands``
- ``sandbox.filesystem.denyRead[]`` / ``allowRead[]`` / ``allowWrite[]`` /
  ``denyWrite[]`` (the policy files; ``denyWrite`` wins over ``allowWrite``)
- ``sandbox.network.allowedDomains[]`` (omitted for ``model+web``; explicit
  empty list for ``none`` to block Bash network entirely)
- ``sandbox.credentials.files[]`` — objects ``{"path": ..., "mode": "deny"}``
- ``sandbox.credentials.envVars[]`` — objects ``{"name": ..., "mode": "deny"}``
- ``permissions.allow[]`` — ``Read``/``Grep``/``Glob`` + worktree-scoped
  ``Edit(//worktree/**)`` (Edit governs the file-editing tools; no ``Write`` rule)
- ``permissions.deny[]`` — ``ToolName(pattern)`` (native secret denies + the
  policy-file ``Edit`` denies + egress). NOTE: Read/Edit permission specifiers use
  ``//abs`` / ``/rel`` / ``~/`` prefixes, which DIFFER from ``sandbox.filesystem``
  paths (``/abs`` absolute).
"""

from __future__ import annotations

import json
from pathlib import Path
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

# Native file tools governed by the PERMISSION layer, not the OS sandbox (which
# binds only Bash). Each must be denied SEPARATELY for a secret path — denying
# ``Read`` does not imply ``Edit``/``Grep``/``Glob``. NOTE: ``Edit(path)`` rules
# govern ALL of Claude's file-editing tools (Edit/Write/MultiEdit); ``Write(path)``
# permission rules are ineffective (confirmed by the live CLI warning), so we
# never emit them.
NATIVE_FILE_TOOLS: tuple[str, ...] = ("Read", "Edit", "Grep", "Glob")

# Native read/enumeration capability allowed explicitly (``dontAsk`` runs only
# allow-listed tools). A worktree-scoped ``Edit(...)`` allow is added per session
# in :func:`build_claude_sandbox_settings` so the agent can edit its worktree;
# secret and settings-file ``Edit`` denies still win (deny beats allow).
NATIVE_READ_ALLOW_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")

# Config files an agent must not modify at session time — writing a wider policy
# here would hot-reload and escalate. Relative to each write root.
_SELF_CONFIG_FILES: tuple[str, ...] = (
    ".claude/settings.json",
    ".claude/settings.local.json",
)


def _self_config_paths(write_roots: tuple[Path, ...]) -> list[str]:
    """Absolute paths of the policy files the agent may not modify."""
    return [str(root / rel) for root in write_roots for rel in _SELF_CONFIG_FILES]


def _permission_rule_path(path: str) -> str:
    """Render a sandbox-style secret path as a Read/Edit permission specifier.

    Read/Edit permission rules use a DIFFERENT prefix convention from
    ``sandbox.filesystem.*`` paths: ``//abs`` for an absolute path, ``~/`` for
    home-relative, ``/rel`` for project-relative (docs.claude.com — Sandbox,
    "This syntax differs from Read and Edit permission rules"). ``deny_read_files``
    entries are home-relative (``~/.ssh``) or absolute (an operator/test path):
    a tilde path passes through, and an absolute path gets its leading slash
    doubled so it is read as absolute rather than project-relative.
    """
    if path.startswith("~"):
        return path
    if path.startswith("/"):
        return "/" + path
    return path


def _native_secret_deny_rules(deny_read_files: tuple[str, ...]) -> list[str]:
    """Permission-layer denies mirroring the OS credential denies onto native tools.

    The OS sandbox (``credentials.files`` / ``denyRead``) binds only Bash and its
    children; the built-in Read/Edit/Grep/Glob/Write tools are governed by
    permission rules and must each be denied separately. Deny rules hold in every
    permission mode, so these are the un-bypassable native-tool secret layer. For
    each path we deny both the path itself and everything beneath it (``/**``).
    """
    rules: list[str] = []
    for path in deny_read_files:
        spec = _permission_rule_path(path)
        for tool in NATIVE_FILE_TOOLS:
            rules.append(f"{tool}({spec})")
            rules.append(f"{tool}({spec}/**)")
    return rules


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
    self_config_paths = _self_config_paths(scope.write_roots)
    sandbox: dict[str, Any] = {
        "enabled": True,
        # Hard-fail rather than silently run unsandboxed if the sandbox can't
        # start, and refuse the ``dangerouslyDisableSandbox`` escape hatch.
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        # Sandbox-weakening booleans, pinned false so the session cannot be
        # softened out from under the boundary.
        "allowAppleEvents": False,
        "enableWeakerNetworkIsolation": False,
        "enableWeakerNestedSandbox": False,
        "filesystem": {
            # Reads are OPEN by default: deny the home dir and re-allow only the
            # read roots within it (defense-in-depth; the fail-closed secret layer
            # is ``credentials.files`` below).
            "denyRead": ["~/"],
            "allowRead": [str(p) for p in scope.read_roots],
            "allowWrite": [str(p) for p in scope.write_roots],
            # Anti-self-modification (Bash layer): the agent may write its worktree
            # but NOT its own policy files. ``denyWrite`` wins over ``allowWrite``,
            # so a sandboxed command cannot rewrite settings to hot-reload a wider
            # policy after launch.
            "denyWrite": self_config_paths,
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

    # Permission layer — governs EVERY tool, incl. the native file tools the OS
    # sandbox does not touch.
    #  - allow: native reads + a worktree-scoped Edit (governs Edit/Write/
    #    MultiEdit) so the agent can edit its worktree, nothing broader.
    #  - deny: secret files (native mirror of credentials.files), the agent's own
    #    policy files (anti-self-modification), then egress tools. Deny beats allow,
    #    so the worktree Edit allow cannot reach a secret or a settings file.
    worktree_edit_allows = [
        f"Edit({_permission_rule_path(str(root))}/**)" for root in scope.write_roots
    ]
    self_config_denies = [
        f"Edit({_permission_rule_path(path)})" for path in self_config_paths
    ]
    settings: dict[str, Any] = {
        "sandbox": sandbox,
        "permissions": {
            "allow": list(NATIVE_READ_ALLOW_TOOLS) + worktree_edit_allows,
            "deny": _native_secret_deny_rules(scope.deny_read_files)
            + self_config_denies
            + list(_deny_tools_for_egress(scope.egress)),
        },
    }
    return settings


def build_claude_sandbox_argv(scope: SandboxScope) -> list[str]:
    """Return the claude-code argv fragment that enforces *scope*.

    Emits ``--permission-mode dontAsk`` (non-yolo, unattended, deny-by-default)
    and the sandbox settings as an inline ``--settings`` JSON string, serialized
    with sorted keys and compact separators for deterministic, testable output.
    The trusted target-repo configuration is loaded normally (ADR-0034 trusted-
    repository contract); the policy this adds constrains the agent, not the repo.
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
