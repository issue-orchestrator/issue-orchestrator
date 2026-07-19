"""Fail-closed launch guard for the claude-code OS sandbox (ADR-0034).

The per-command ``--settings`` adapter (:mod:`.sandbox`) can only GUARANTEE the
**un-widenable deny floor** — the secret ``credentials.files`` deny (for Bash)
and the ``permissions.deny`` native-tool secret rules — because Claude Code
**merges array-valued settings across scopes** (docs.claude.com — Settings,
"Arrays merge across settings sources"). A user ``~/.claude/settings.json`` or a
target-repo ``.claude/settings.json`` can therefore ADD:

- ``permissions.allow: ["Edit", "Write"]`` — native writes the OS sandbox does
  NOT bound (the sandbox governs Bash only), granting host-wide writes; or
- ``sandbox.excludedCommands: [...]`` — commands Claude Code runs OUTSIDE the
  sandbox entirely (bypassing the filesystem/network/credentials boundary).

``--settings`` is only the *command-line* scope; it cannot lock these arrays.
Only MANAGED settings (``allowManagedPermissionRulesOnly``, delivered via
MDM/OS/file — NOT ``--settings``) can. So the write/exec **bounds** are an
ENVIRONMENT property, not a per-command one.

This module is the **single fail-closed deployment owner** the invariant needs.
Before an opted-in agent launches, it reads the settings scopes that will merge
with our ``--settings`` and REFUSES (raises :class:`SandboxEnvironmentUnsafeError`)
when the environment can widen the policy beyond the deny floor. Either the
environment is provably locked (managed lockdown) or provably clean (no widening
entries), or a sandboxed agent does not launch at all.

Provider scope: the *reader* here is claude-code-specific (Claude's
``settings.json`` merge model). The pattern — resolve ambient config scopes,
detect widening, fail closed — is the seam the codex sandbox will mirror with its
own ``config.toml`` merge model (ADR-0034 follow-up, #6859).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from issue_orchestrator.domain.sandbox_scope import SandboxScope

__all__ = [
    "NATIVE_WRITE_TOOLS",
    "AmbientClaudeSettings",
    "SandboxEnvironmentUnsafeError",
    "assert_claude_sandbox_environment_safe",
    "default_managed_settings_paths",
    "evaluate_sandbox_environment",
    "read_ambient_claude_settings",
]

# Native write tools governed by the PERMISSION layer, NOT the OS sandbox (which
# binds Bash only). An ambient ``permissions.allow`` of any of these grants
# host-wide writes unless it is confined to one of the session's write roots.
NATIVE_WRITE_TOOLS: frozenset[str] = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit"}
)


def default_managed_settings_paths() -> tuple[Path, ...]:
    """OS locations Claude Code reads managed (enterprise-policy) settings from.

    These are the un-overridable scope; only entries here can lock the merged
    arrays. They normally do not exist on a developer/CI host (no MDM), so the
    guard falls back to the "provably clean" acceptance path there.
    """
    return (
        Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
        Path("/etc/claude-code/managed-settings.json"),
    )


@dataclass(frozen=True)
class AmbientClaudeSettings:
    """The merged, launch-relevant view of the ambient Claude Code settings.

    Attributes:
        permission_allow: Union of ``permissions.allow`` across every scope.
        excluded_commands: Union of ``sandbox.excludedCommands`` across scopes.
        managed_permission_rules_locked: ``allowManagedPermissionRulesOnly`` is
            set in a MANAGED scope, so user/project ``permissions.allow`` rules
            are ignored by Claude Code (the allow-based escape is closed).
    """

    permission_allow: tuple[str, ...]
    excluded_commands: tuple[str, ...]
    managed_permission_rules_locked: bool


class SandboxEnvironmentUnsafeError(RuntimeError):
    """Raised when the ambient environment can widen the sandbox policy.

    Carries the concrete widening reasons so the launch path (and tests) can
    assert on them.
    """

    def __init__(self, reasons: list[str]) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        super().__init__(
            "Refusing to launch a sandboxed agent: the ambient Claude Code "
            "settings can widen the sandbox policy beyond its guaranteed deny "
            "floor. " + "; ".join(reasons) + ". Lock the policy via managed "
            "settings (allowManagedPermissionRulesOnly, and no "
            "sandbox.excludedCommands) or remove the widening entries."
        )


def _tool_of(entry: str) -> str:
    """Return the tool name of a permission rule (``Edit(//x/**)`` -> ``Edit``)."""
    return entry.split("(", 1)[0].strip()


def _allow_confined_to_write_roots(
    entry: str, write_roots: tuple[Path, ...]
) -> bool:
    """Whether a native-write allow rule is bounded to a session write root.

    Fail-closed: a bare ``Edit`` (no path specifier) is unbounded, and any
    specifier that does not resolve under a write root is treated as an escape.
    Read/Edit specifiers use ``//`` for absolute paths, which we normalize to a
    single leading slash before comparing.
    """
    open_paren = entry.find("(")
    close_paren = entry.rfind(")")
    if open_paren == -1 or close_paren <= open_paren:
        return False  # bare tool name -> unbounded
    spec = entry[open_paren + 1 : close_paren].strip()
    if not spec:
        return False
    normalized = spec[1:] if spec.startswith("//") else spec
    normalized = normalized.split("*", 1)[0]  # drop trailing glob
    return any(normalized.startswith(str(root)) for root in write_roots)


def evaluate_sandbox_environment(
    scope: "SandboxScope", ambient: AmbientClaudeSettings
) -> None:
    """Raise :class:`SandboxEnvironmentUnsafeError` if *ambient* can widen *scope*.

    Pure decision function (no I/O). Two independent escapes are checked:

    - ``sandbox.excludedCommands`` — has no managed-only lock, so ANY entry lets
      a command run unsandboxed. Prohibited outright.
    - ``permissions.allow`` of a native write tool not confined to a write root —
      grants host-wide native writes, UNLESS a managed lock ignores ambient
      permission rules.
    """
    reasons: list[str] = []

    if ambient.excluded_commands:
        reasons.append(
            "sandbox.excludedCommands lets commands run OUTSIDE the sandbox: "
            f"{list(ambient.excluded_commands)}"
        )

    if not ambient.managed_permission_rules_locked:
        escaping = [
            entry
            for entry in ambient.permission_allow
            if _tool_of(entry) in NATIVE_WRITE_TOOLS
            and not _allow_confined_to_write_roots(entry, scope.write_roots)
        ]
        if escaping:
            reasons.append(
                "permissions.allow grants native writes the OS sandbox cannot "
                f"bound: {escaping}"
            )

    if reasons:
        raise SandboxEnvironmentUnsafeError(reasons)


def _load_json(path: Path) -> dict[str, Any]:
    """Best-effort JSON load; a missing/invalid file contributes nothing."""
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _permission_allow_of(data: dict[str, Any]) -> list[str]:
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return []
    allow = permissions.get("allow")
    if not isinstance(allow, list):
        return []
    return [entry for entry in allow if isinstance(entry, str)]


def _excluded_commands_of(data: dict[str, Any]) -> list[str]:
    sandbox = data.get("sandbox")
    if not isinstance(sandbox, dict):
        return []
    excluded = sandbox.get("excludedCommands")
    if not isinstance(excluded, list):
        return []
    return [entry for entry in excluded if isinstance(entry, str)]


def _user_settings_path(home: Path, config_dir: str | None) -> Path:
    # CLAUDE_CONFIG_DIR overrides ~/.claude when set (Claude Code convention).
    if config_dir:
        return Path(config_dir) / "settings.json"
    return home / ".claude" / "settings.json"


def read_ambient_claude_settings(
    *,
    home: Path,
    project_dir: Path,
    config_dir: str | None = None,
    managed_settings_paths: tuple[Path, ...] | None = None,
) -> AmbientClaudeSettings:
    """Read and merge the settings scopes that combine with our ``--settings``.

    Union the array keys across managed, user, project, and local scopes (Claude
    merges arrays across scopes). The managed lock flag is read only from the
    managed scope (only a managed scope can set it).
    """
    if managed_settings_paths is None:
        managed_settings_paths = default_managed_settings_paths()

    scope_paths: list[Path] = [
        *managed_settings_paths,
        _user_settings_path(home, config_dir),
        project_dir / ".claude" / "settings.json",
        project_dir / ".claude" / "settings.local.json",
    ]

    allow: list[str] = []
    excluded: list[str] = []
    for path in scope_paths:
        data = _load_json(path)
        allow.extend(_permission_allow_of(data))
        excluded.extend(_excluded_commands_of(data))

    managed_locked = any(
        _load_json(path).get("allowManagedPermissionRulesOnly") is True
        for path in managed_settings_paths
    )

    return AmbientClaudeSettings(
        permission_allow=tuple(allow),
        excluded_commands=tuple(excluded),
        managed_permission_rules_locked=managed_locked,
    )


def assert_claude_sandbox_environment_safe(
    scope: "SandboxScope",
    *,
    home: Path | None = None,
    project_dir: Path | None = None,
    config_dir: str | None = None,
    managed_settings_paths: tuple[Path, ...] | None = None,
) -> None:
    """Read the ambient settings and fail closed if they can widen *scope*.

    Resolves the user scope from ``$CLAUDE_CONFIG_DIR``/``$HOME`` and the project
    scope from the session's write root (the worktree) when not supplied. Raises
    :class:`SandboxEnvironmentUnsafeError` on a detected widening.
    """
    if home is None:
        home = Path(os.environ.get("HOME") or Path.home())
    if config_dir is None:
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if project_dir is None:
        project_dir = scope.write_roots[0] if scope.write_roots else Path.cwd()

    ambient = read_ambient_claude_settings(
        home=home,
        project_dir=project_dir,
        config_dir=config_dir,
        managed_settings_paths=managed_settings_paths,
    )
    evaluate_sandbox_environment(scope, ambient)
