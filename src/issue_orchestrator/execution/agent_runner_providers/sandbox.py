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

CONTRACT — what ``--settings`` GUARANTEES vs what needs the launch guard. Claude
Code MERGES array-valued settings across scopes (docs.claude.com — Settings), and
``--settings`` is only the *command-line* scope, so it can lock only DENY-based
rules (deny always wins and merges narrow):
- **Guaranteed (un-widenable) here:** the secret floor — ``credentials.files``
  deny (Bash) + ``permissions.deny`` native-tool secret rules — and the sandbox
  booleans (``enabled``/``failIfUnavailable``/``allowUnsandboxedCommands``, which
  ``--settings`` wins for scalars over user/project).
- **NOT guaranteed by ``--settings`` alone:** the write/exec/read *bounds*. A
  merged ambient ``permissions.allow: ["Edit","Write"]`` grants host-wide native
  writes (the OS sandbox binds Bash only), and ``sandbox.excludedCommands`` runs
  a command unsandboxed. These arrays can only be locked by MANAGED settings
  (``allowManagedPermissionRulesOnly`` — delivered out-of-band, not ``--settings``).

So the write/exec bounds are an ENVIRONMENT property that ``--settings`` cannot
own (ambient sources merge from unreadable locations and hot-reload). The
fail-closed deployment owner :mod:`.sandbox_preflight` therefore requires an
installed **managed lockdown** — the authoritative, write-protected managed
policy that sets ``allowManagedPermissionRulesOnly`` (Claude then ignores every
non-managed allow rule) — and refuses to launch without it. This module stays
the pure ``--settings`` translator carrying the un-widenable deny floor; the
managed policy owns the effective permission bounds.

READ POSTURE (deliberate): non-secret reads OUTSIDE the worktree remain possible.
``permissions.allow`` grants the native read tools broadly (the tech-lead's
god-view is *wide reads*; a coder reads its worktree), and ``denyRead: ["~/"]``
only bounds *Bash* reads (defense-in-depth; merged, so not un-widenable). This is
NOT a "reads confined to the worktree" jail — a literal native read-lockdown is
not expressible via ``--settings`` under unattended ``dontAsk`` and would blind
the god-view; a hard read boundary requires managed ``allowManagedReadPathsOnly``.

KNOWN LIMITATION: a GitHub *App* private key at an operator-configured absolute
path *outside* home is covered only if it is listed in ``deny_read_files``; the
static secret list lives in the domain (``DEFAULT_SANDBOX_DENY_READ_FILES``) and
a home-based key is additionally covered by the Bash-layer ``denyRead: ["~/"]``.

Settings schema (docs.claude.com — Sandbox settings / Permissions):
- ``sandbox.enabled`` / ``failIfUnavailable`` / ``allowUnsandboxedCommands``
- ``sandbox.filesystem.denyRead[]`` / ``allowRead[]`` / ``allowWrite[]``
- ``sandbox.network.allowedDomains[]`` (omitted for ``model+web``; explicit
  empty list for ``none`` to block Bash network entirely)
- ``sandbox.credentials.files[]`` — objects ``{"path": ..., "mode": "deny"}``
- ``sandbox.credentials.envVars[]`` — objects ``{"name": ..., "mode": "deny"}``
- ``permissions.allow[]`` — ``ToolName`` / ``ToolName(pattern)`` (broad reads)
- ``permissions.deny[]`` — ``ToolName(pattern)`` (native secret denies + egress).
  NOTE: Read/Edit permission specifiers use ``//abs`` / ``/rel`` / ``~/`` path
  prefixes, which DIFFER from ``sandbox.filesystem`` paths (``/abs`` absolute).
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

# Native file tools governed by the PERMISSION layer, not the OS sandbox (which
# binds only Bash). Each must be denied SEPARATELY for a secret path — denying
# ``Read`` does not imply ``Edit``/``Grep``/``Glob``/``Write``. Read/Grep/Glob/
# Edit are content or enumeration reads; ``Write`` is denied too so a secret dir
# (``~/.ssh``) cannot be used for write-based persistence.
NATIVE_FILE_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Grep", "Glob")

# The god-view read capability. ``dontAsk`` runs only allow-listed tools, so the
# native read tools must be allowed explicitly or a sandboxed agent could not
# read at all. Secret paths in :data:`NATIVE_FILE_TOOLS` denies still win because
# deny beats allow in every permission mode.
NATIVE_READ_ALLOW_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")


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
    sandbox: dict[str, Any] = {
        "enabled": True,
        # Hard-fail rather than silently run unsandboxed if the sandbox can't
        # start, and refuse the ``dangerouslyDisableSandbox`` escape hatch.
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        # Sandbox-weakening booleans, pinned false. ``--settings`` is a higher
        # scope than user/project/local for scalar keys, so a merged ambient
        # scope cannot re-enable these (only a managed policy could). This closes
        # the boolean escapes that the launch guard (sandbox_preflight) would
        # otherwise have to reject; the guard covers the array/hook escapes that
        # ``--settings`` cannot lock.
        "allowAppleEvents": False,
        "enableWeakerNetworkIsolation": False,
        "enableWeakerNestedSandbox": False,
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

    # Permission layer — governs EVERY tool, incl. the native file tools the OS
    # sandbox does not touch. Always present: the native-tool secret denies and
    # the god-view read allow-list apply regardless of egress posture.
    settings: dict[str, Any] = {
        "sandbox": sandbox,
        "permissions": {
            # Broad reads so a sandboxed agent can actually read under dontAsk;
            # secret denies below still win (deny beats allow in every mode).
            "allow": list(NATIVE_READ_ALLOW_TOOLS),
            # Native-tool secret denies (mirror of credentials.files) FIRST, then
            # the egress tool denies (empty for model+web).
            "deny": _native_secret_deny_rules(scope.deny_read_files)
            + list(_deny_tools_for_egress(scope.egress)),
        },
    }
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
