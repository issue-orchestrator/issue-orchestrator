"""Provider adapters that translate a :class:`SandboxScope` into CLI flags.

A :class:`~issue_orchestrator.domain.sandbox_scope.SandboxScope` is
provider-agnostic. Each AI-agent CLI enforces a sandbox differently, so the
translation lives here, next to the providers, behind the
:class:`ProviderSandboxAdapter` port.

The **claude-code** translation emits a Claude Code
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

CODEX TRANSLATION (Codex 0.138+ permission profiles): ``--sandbox
workspace-write`` cannot represent :attr:`SandboxScope.deny_read_files` and,
empirically, permits reads outside the workspace. Current Codex also documents
that ``--sandbox`` selects the legacy policy and disables permission profiles.
The adapter therefore uses an invocation-scoped profile extending Codex's native
``:workspace`` profile instead of emitting the superficially similar but weaker
``-s workspace-write`` flag. Because a legacy ``sandbox_mode`` in any loaded
Codex config layer disables profiles, the adapter checks the documented system,
user, and project config files and fails loud with migration guidance when that
incompatible key is present. The profile:

- pins the worktree with ``-C`` and grants only explicitly scoped extra roots
  with repeated ``--add-dir``;
- writes workspace roots, carves every ``deny_read_files`` path to ``deny``, and
  keeps ``.codex`` read-only so the agent cannot rewrite its own project policy;
- resolves linked-worktree Git metadata before launch so staging and commits can
  update only that worktree's admin state and current branch; shared config,
  hooks, packs, other refs, and other worktrees stay read-only;
- denies temporary-directory writes outside the explicit roots;
- disables command network plus native web search for ``none`` / ``model-only``;
- excludes ``deny_env`` names from spawned-command environments; and
- uses ``-a never`` so a denied operation fails instead of prompting to escape.

Codex has no read-only form of ``--add-dir``. An additional ``read_root`` is
therefore also writable for Codex. This is the documented bounded-writable
residual from ADR-0034: the adapter widens only the exact orchestrator-computed
root, never ``$HOME`` or an unrelated repository implicitly.

Git commits have one further linked-worktree residual: they must create loose
objects in the base repository's shared object store. That object subtree is
writable, while ``objects/info`` (including alternates) and ``objects/pack`` are
carved back to read-only. The current branch ref/reflog is writable by exact
path; all other shared Git metadata remains read-only.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import tomllib
from typing import Any, Protocol, runtime_checkable

from issue_orchestrator.domain.sandbox_scope import (
    SandboxEgress,
    SandboxScope,
    SandboxUnsupportedError,
)

__all__ = [
    "MODEL_API_DOMAINS",
    "MODEL_ONLY_DENY_TOOLS",
    "CODEX_PERMISSION_PROFILE",
    "CodexGitWorktreeAccess",
    "ClaudeSandboxAdapter",
    "CodexSandboxAdapter",
    "ProviderSandboxAdapter",
    "build_claude_sandbox_argv",
    "build_claude_sandbox_settings",
    "build_codex_sandbox_argv",
    "resolve_codex_git_worktree_access",
    "validate_codex_permission_profile_compatibility",
]

CODEX_PERMISSION_PROFILE = "issue_orchestrator_scope"
_CODEX_CREDENTIAL_FILES: tuple[str, ...] = ("~/.codex",)
_CODEX_CREDENTIAL_ENV: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_HOME",
)

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


def _toml_string(value: str) -> str:
    """Encode a string accepted by Codex's TOML ``-c key=value`` parser."""
    return json.dumps(value, ensure_ascii=False)


def _toml_inline_table(entries: list[tuple[str, str]]) -> str:
    return "{ " + ", ".join(f"{_toml_string(k)} = {v}" for k, v in entries) + " }"


def _is_absolute_or_home_path(raw: str) -> bool:
    return (
        raw == "~"
        or raw.startswith("~/")
        or Path(raw).is_absolute()
        or PureWindowsPath(raw).is_absolute()
    )


@dataclass(frozen=True)
class CodexGitWorktreeAccess:
    """Resolved Git paths a sandboxed Codex session needs to make commits.

    A linked worktree's ``.git`` entry is a file pointing into the base
    repository's shared Git directory. Granting only the visible worktree is
    therefore insufficient: ``git add`` must update the worktree-specific
    index, while ``git commit`` must add objects and advance the current branch.

    The paths are resolved before Codex starts and translated into exact
    permission-profile entries. Shared config, hooks, other refs, and other
    worktrees remain read-only.
    """

    git_dir: Path
    common_dir: Path
    head_ref: Path | None

    def __post_init__(self) -> None:
        for name, path in (
            ("git_dir", self.git_dir),
            ("common_dir", self.common_dir),
        ):
            if not path.is_absolute():
                raise SandboxUnsupportedError(
                    f"Codex sandbox {name} must be absolute (got {path})"
                )
        if not self.git_dir.is_relative_to(self.common_dir):
            raise SandboxUnsupportedError(
                "Codex sandbox worktree Git directory must stay inside the "
                f"common Git directory (got {self.git_dir})"
            )
        if self.head_ref is not None:
            if not self.head_ref.is_absolute():
                raise SandboxUnsupportedError(
                    f"Codex sandbox head_ref must be absolute (got {self.head_ref})"
                )
            if not self.head_ref.is_relative_to(self.common_dir):
                raise SandboxUnsupportedError(
                    "Codex sandbox current branch ref must stay inside the "
                    f"common Git directory (got {self.head_ref})"
                )


def _read_git_path_file(path: Path, *, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SandboxUnsupportedError(
            f"Codex sandbox could not read {label} at {path}: {exc}"
        ) from exc


def _codex_loaded_config_files(worktree: Path) -> tuple[Path, ...]:
    """Return documented Codex config layers that can disable profiles."""
    candidates = (
        Path("/etc/codex/config.toml"),
        _codex_home() / "config.toml",
        worktree / ".codex" / "config.toml",
    )
    return tuple(path for path in candidates if path.is_file())


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser().resolve() if raw else Path.home() / ".codex"


def _codex_credential_files() -> tuple[str, ...]:
    """Credential stores to deny, including an operator-set Codex home."""
    paths = list(_CODEX_CREDENTIAL_FILES)
    if os.environ.get("CODEX_HOME"):
        paths.append(str(_codex_home()))
    return tuple(dict.fromkeys(paths))


def validate_codex_permission_profile_compatibility(worktree: Path) -> None:
    """Fail if a loaded legacy setting would silently disable the profile.

    Codex documents that the presence of ``sandbox_mode`` in *any* loaded
    config layer selects its legacy sandbox and disables permission profiles,
    even when ``default_permissions`` is supplied at higher precedence. There
    is no invocation-level unset. Detect the documented system, user, and
    project layers before launch so ``sandbox: true`` cannot silently degrade.
    """
    for path in _codex_loaded_config_files(worktree):
        try:
            config = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SandboxUnsupportedError(
                f"Codex sandbox could not validate config layer {path}: {exc}"
            ) from exc
        if "sandbox_mode" in config:
            raise SandboxUnsupportedError(
                "Codex sandbox permission profiles are disabled by legacy "
                f"sandbox_mode in {path}; remove that key before using agent.sandbox"
            )


def _resolve_git_directory(worktree: Path) -> Path:
    marker = worktree / ".git"
    if marker.is_dir():
        return marker.resolve()
    if not marker.is_file():
        raise SandboxUnsupportedError(
            f"Codex sandbox working directory is not a Git worktree: {worktree}"
        )

    raw = _read_git_path_file(marker, label="linked-worktree .git pointer")
    prefix = "gitdir:"
    if not raw.lower().startswith(prefix):
        raise SandboxUnsupportedError(
            f"Codex sandbox found a malformed .git pointer at {marker}"
        )
    target = raw[len(prefix) :].strip()
    if not target:
        raise SandboxUnsupportedError(
            f"Codex sandbox found an empty .git pointer at {marker}"
        )
    git_dir = Path(target)
    if not git_dir.is_absolute():
        git_dir = marker.parent / git_dir
    git_dir = git_dir.resolve()
    if not git_dir.is_dir():
        raise SandboxUnsupportedError(
            f"Codex sandbox linked-worktree Git directory does not exist: {git_dir}"
        )
    return git_dir


def resolve_codex_git_worktree_access(worktree: Path) -> CodexGitWorktreeAccess:
    """Resolve the minimal Git metadata paths required by the current worktree.

    This filesystem discovery belongs in the provider adapter rather than the
    domain scope computation: :class:`SandboxScope` stays provider-agnostic and
    pure, while Codex receives the concrete paths its OS profile must expose.
    """
    git_dir = _resolve_git_directory(worktree)
    common_marker = git_dir / "commondir"
    if common_marker.exists():
        raw_common = _read_git_path_file(
            common_marker, label="linked-worktree commondir pointer"
        )
        if not raw_common:
            raise SandboxUnsupportedError(
                f"Codex sandbox found an empty commondir pointer at {common_marker}"
            )
        common_dir = Path(raw_common)
        if not common_dir.is_absolute():
            common_dir = git_dir / common_dir
        common_dir = common_dir.resolve()
    else:
        common_dir = git_dir
    if not common_dir.is_dir():
        raise SandboxUnsupportedError(
            f"Codex sandbox common Git directory does not exist: {common_dir}"
        )

    raw_head = _read_git_path_file(git_dir / "HEAD", label="worktree HEAD")
    head_ref: Path | None = None
    if raw_head.startswith("ref:"):
        raw_ref = raw_head.removeprefix("ref:").strip()
        ref = PurePosixPath(raw_ref)
        if (
            not raw_ref
            or ref.is_absolute()
            or ".." in ref.parts
            or ref.parts[:2] != ("refs", "heads")
        ):
            raise SandboxUnsupportedError(
                "Codex sandbox only supports symbolic HEAD refs under refs/heads "
                f"(got {raw_ref!r})"
            )
        head_ref = common_dir.joinpath(*ref.parts)

    return CodexGitWorktreeAccess(
        git_dir=git_dir,
        common_dir=common_dir,
        head_ref=head_ref,
    )


def _with_lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _codex_git_filesystem_entries(
    access: CodexGitWorktreeAccess,
) -> list[tuple[str, str]]:
    """Return least-privilege Git entries for status, staging, and commits."""
    read = _toml_string("read")
    write = _toml_string("write")
    entries: list[tuple[str, str]] = [(str(access.common_dir), read)]

    if access.git_dir != access.common_dir:
        # A linked worktree's admin directory contains its index, HEAD reflog,
        # merge/rebase state, and COMMIT_EDITMSG. It is private to this
        # worktree, so it can be writable as a unit. Pointer/config files are
        # carved back to read-only so the agent cannot redirect or reconfigure
        # later Git operations.
        entries.append((str(access.git_dir), write))
        protected_names = ["commondir", "gitdir", "config", "config.worktree"]
        if access.head_ref is not None:
            # A symbolic HEAD pins the orchestrator-owned issue branch. Detached
            # reviewer worktrees instead need their private HEAD writable.
            protected_names.insert(0, "HEAD")
        for name in protected_names:
            entries.append((str(access.git_dir / name), read))
    else:
        # A non-linked checkout has no private admin directory. Keep the shared
        # directory read-only and open only the files used by a normal add +
        # commit sequence.
        for path in (
            access.git_dir / "index",
            access.git_dir / "COMMIT_EDITMSG",
            access.git_dir / "logs" / "HEAD",
        ):
            entries.extend(((str(path), write), (str(_with_lock_path(path)), write)))

    # New commit/tree/blob objects necessarily enter the shared object store.
    # Existing packs and the alternates/configuration area remain read-only,
    # preventing a session from replacing packs or redirecting object lookup.
    objects = access.common_dir / "objects"
    entries.extend(
        (
            (str(objects), write),
            (str(objects / "info"), read),
            (str(objects / "pack"), read),
        )
    )

    if access.head_ref is None:
        # Detached linked worktrees advance their private HEAD directly.
        head = access.git_dir / "HEAD"
        entries.extend(((str(head), write), (str(_with_lock_path(head)), write)))
        return entries

    branch_log = (
        access.common_dir / "logs" / access.head_ref.relative_to(access.common_dir)
    )
    for path in (access.head_ref, branch_log):
        entries.extend(((str(path), write), (str(_with_lock_path(path)), write)))
    return entries


def _codex_permission_profile(
    scope: SandboxScope,
    git_access: CodexGitWorktreeAccess,
) -> str:
    """Serialize the invocation-scoped Codex permission profile for *scope*."""
    workspace_rules = _toml_inline_table(
        [
            (".", _toml_string("write")),
            # Codex project policy/config is trusted at launch but immutable to
            # the running agent, preventing a session-time self-widen.
            (".codex", _toml_string("read")),
        ]
    )
    filesystem_entries = [
        # Built-in :workspace historically grants temp writes. Reduce both
        # temp aliases to read-only, then reopen only explicit workspace/Git
        # roots below. Read (rather than deny) is necessary because Git
        # canonicalizes linked-worktree paths through their temp ancestors on
        # macOS; exact secret-path denies still override this broad read.
        (":tmpdir", _toml_string("read")),
        (":slash_tmp", _toml_string("read")),
        (":workspace_roots", workspace_rules),
    ]
    filesystem_entries.extend(_codex_git_filesystem_entries(git_access))
    seen_denies: set[str] = set()
    for raw in (*scope.deny_read_files, *_codex_credential_files()):
        if not _is_absolute_or_home_path(raw):
            raise SandboxUnsupportedError(
                "Codex sandbox deny_read_files entries must be absolute or "
                f"home-relative (got {raw!r})"
            )
        if raw not in seen_denies:
            filesystem_entries.append((raw, _toml_string("deny")))
            seen_denies.add(raw)

    filesystem = _toml_inline_table(filesystem_entries)
    network_enabled = "true" if scope.egress == "model+web" else "false"
    return (
        "{ "
        f"extends = {_toml_string(':workspace')}, "
        f"filesystem = {filesystem}, "
        f"network = {{ enabled = {network_enabled} }} "
        "}"
    )


def _codex_workspace_roots(scope: SandboxScope) -> list[Path]:
    """Return unique additional roots, preserving orchestrator policy order."""
    additional: list[Path] = []
    for root in (*scope.read_roots, *scope.write_roots):
        if root != scope.working_directory and root not in additional:
            additional.append(root)
    return additional


def build_codex_sandbox_argv(
    scope: SandboxScope,
    *,
    git_access: CodexGitWorktreeAccess | None = None,
) -> list[str]:
    """Return Codex global argv enforcing *scope* with a native profile.

    These arguments must appear before the ``exec`` subcommand because ``-a``
    is a root-command option in current Codex releases. Unknown permission-
    profile keys fail under ``--strict-config``; this makes pre-0.138 Codex
    versions fail loud instead of silently dropping the boundary.
    """
    if git_access is None:
        validate_codex_permission_profile_compatibility(scope.working_directory)
        resolved_git_access = resolve_codex_git_worktree_access(scope.working_directory)
    else:
        resolved_git_access = git_access
    argv = [
        "--strict-config",
        "-a",
        "never",
        "-C",
        str(scope.working_directory),
    ]
    for root in _codex_workspace_roots(scope):
        argv.extend(["--add-dir", str(root)])

    profile = _codex_permission_profile(scope, resolved_git_access)
    deny_env_names = list(dict.fromkeys((*scope.deny_env, *_CODEX_CREDENTIAL_ENV)))
    deny_env = json.dumps(deny_env_names, ensure_ascii=False, separators=(",", ":"))
    web_search = "live" if scope.egress == "model+web" else "disabled"
    argv.extend(
        [
            "-c",
            f"default_permissions={_toml_string(CODEX_PERMISSION_PROFILE)}",
            "-c",
            f"permissions.{CODEX_PERMISSION_PROFILE}={profile}",
            "-c",
            f"shell_environment_policy.exclude={deny_env}",
            "-c",
            "shell_environment_policy.ignore_default_excludes=false",
            "-c",
            f"web_search={_toml_string(web_search)}",
        ]
    )
    return argv


class CodexSandboxAdapter:
    """Codex permission-profile implementation of :class:`ProviderSandboxAdapter`."""

    def apply_scope(self, scope: SandboxScope) -> list[str]:
        return build_codex_sandbox_argv(scope)
