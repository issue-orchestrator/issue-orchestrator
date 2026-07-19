"""Fail-closed launch guard for the claude-code OS sandbox (ADR-0034).

The per-command ``--settings`` adapter (:mod:`.sandbox`) can only GUARANTEE the
**un-widenable deny floor** — the secret ``credentials.files`` deny (Bash) and
the ``permissions.deny`` native-tool secret rules — because Claude Code **merges
array-valued settings across scopes** (docs.claude.com — Settings, "Arrays merge
across settings sources"), and ``--settings`` is only the *command-line* scope.
Every ALLOW-direction field that affects the advertised bounds can therefore be
widened by a merged user ``~/.claude`` or target-repo ``.claude/settings.json``.

This module is the **single fail-closed deployment owner** for that invariant.
Before an opted-in agent launches it reads the settings scopes that merge with
our ``--settings`` and REFUSES (raises :class:`SandboxEnvironmentUnsafeError`)
when the *effective* environment can widen the policy past the deny floor.

COMPLETE WIDENING MODEL (every input that can breach a claimed bound):

===========================  =========================  ======================
field                        breaches                   managed-only lock
===========================  =========================  ======================
``permissions.allow`` Edit/  write bound (host native   allowManagedPermission
Write/MultiEdit/NotebookEdit  writes, sandbox binds       RulesOnly
not confined to a write root  Bash only)
``sandbox.filesystem.        write bound (Bash writes    none — always merges
allowWrite`` outside roots    outside the worktree)
``sandbox.network.           egress bound               allowManagedDomainsOnly
allowedDomains`` beyond the
egress policy
``sandbox.excludedCommands``  every bound (runs the      none — prohibited
                              command UNSANDBOXED)        outright
``sandbox.allowUnixSockets``  every bound (e.g. a        none — prohibited
                              docker.sock bridge)         outright
``hooks.PreToolUse`` /        deny floor (a hook can     none — prohibited from
``hooks.PermissionRequest``   approve otherwise-denied    a non-managed scope
                              tools under dontAsk)
===========================  =========================  ======================

PROVENANCE: when a dimension has a managed-only lock and it is set, Claude
IGNORES non-managed entries, so the *effective* set is the managed entries only.
We validate the EFFECTIVE set (not skip validation, and not blanket-trust
managed) — a managed ``allow: ["Write"]`` is still a host write grant and is
rejected the same as any other. Dimensions without a lock always take the union.

CONFINEMENT is fail-closed and path-component based (``Path.is_relative_to`` on
resolved absolute paths, not string prefixes): a sibling like ``/wt/issue-90`` is
NOT inside ``/wt/issue-9``, and a glob (``//wt/issue-9*/**``) is confined only by
its non-glob parent (``/wt``), which is outside the root — both rejected.

Booleans that WEAKEN the sandbox (``allowAppleEvents``, ``enableWeakerNetwork
Isolation``, ``enableWeakerNestedSandbox``) are set false by the adapter itself:
``--settings`` is a higher scope than user/project/local for scalars, so those
cannot be re-enabled except by a managed policy (an operator choice). MCP is
already locked off by the provider's ``--strict-mcp-config``.

NOT checked (deliberately): ``sandbox.filesystem.allowRead`` — reads are the
tech-lead god-view and are NOT a claimed bound, and a widening ``allowRead``
cannot re-expose a secret (an exact ``deny`` holds inside a wider allow, and
``credentials.files`` deny is un-widenable).

Provider scope: the reader is claude-code-specific (Claude's ``settings.json``
merge model). The pattern — resolve ambient scopes, validate the effective set,
fail closed — is the seam the codex sandbox will mirror with its own
``config.toml`` merge model (ADR-0034 follow-up, #6859).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from issue_orchestrator.domain.sandbox_scope import SandboxScope

__all__ = [
    "NATIVE_WRITE_TOOLS",
    "PERMISSION_HOOK_EVENTS",
    "AmbientClaudeSettings",
    "SandboxEnvironmentUnsafeError",
    "ScopedEntries",
    "assert_claude_sandbox_environment_safe",
    "default_managed_settings_paths",
    "evaluate_sandbox_environment",
    "read_ambient_claude_settings",
]

# Native write tools governed by the PERMISSION layer, NOT the OS sandbox (which
# binds Bash only). Each must be denied separately for a secret path; here they
# are the tools whose ambient ``allow`` grants host-wide writes.
NATIVE_WRITE_TOOLS: frozenset[str] = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit"}
)

# Hook events that can APPROVE a tool call under ``dontAsk`` (and so re-open a
# path the deny floor would otherwise block). Their mere presence in a
# non-managed scope is treated fail-closed as a widening.
PERMISSION_HOOK_EVENTS: tuple[str, ...] = ("PreToolUse", "PermissionRequest")

_GLOB_CHARS = ("*", "?", "[")


def default_managed_settings_paths() -> tuple[Path, ...]:
    """OS locations Claude Code reads managed (enterprise-policy) settings from.

    The un-overridable scope; only entries here can lock the merged arrays. They
    normally do not exist on a developer/CI host (no MDM), so the guard falls
    back to the "provably clean" acceptance path there.
    """
    return (
        Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
        Path("/etc/claude-code/managed-settings.json"),
    )


@dataclass(frozen=True)
class ScopedEntries:
    """String entries for one widening dimension, split by provenance."""

    managed: tuple[str, ...]
    nonmanaged: tuple[str, ...]

    def effective(self, managed_locked: bool) -> tuple[str, ...]:
        """Entries Claude actually applies: managed-only when locked, else union."""
        return self.managed if managed_locked else self.managed + self.nonmanaged

    def union(self) -> tuple[str, ...]:
        return self.managed + self.nonmanaged


@dataclass(frozen=True)
class AmbientClaudeSettings:
    """The merged, launch-relevant view of the ambient Claude Code settings."""

    permission_allow: ScopedEntries
    filesystem_allow_write: ScopedEntries
    network_allowed_domains: ScopedEntries
    excluded_commands: ScopedEntries
    allow_unix_sockets: ScopedEntries
    permission_hooks: ScopedEntries
    managed_permission_rules_locked: bool
    managed_domains_locked: bool


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
            "settings or remove the widening entries."
        )


# ---------------------------------------------------------------------------
# Path resolution + confinement (fail-closed, path-component based)
# ---------------------------------------------------------------------------


def _has_glob(component: str) -> bool:
    return any(ch in component for ch in _GLOB_CHARS)


def _nonglob_prefix(rest: str) -> list[str]:
    """Path components up to (excluding) the first one bearing a glob char."""
    out: list[str] = []
    for comp in rest.split("/"):
        if comp in ("", "."):
            continue
        if _has_glob(comp):
            break
        out.append(comp)
    return out


def _join(base: Path, components: list[str]) -> Path:
    resolved = base
    for comp in components:
        resolved = resolved.parent if comp == ".." else resolved / comp
    return resolved


def _resolve_permission_spec(
    spec: str, *, home: Path, project_dir: Path
) -> Path | None:
    """Resolve a Read/Edit permission specifier to its non-glob absolute parent.

    Read/Edit specifiers use ``//`` absolute, ``/`` project-relative, ``~/`` home
    (docs.claude.com — Settings). Returns ``None`` on an empty/unparseable spec
    so the caller fails closed.
    """
    spec = spec.strip()
    if not spec:
        return None
    if spec.startswith("//"):
        base, rest = Path("/"), spec[2:]
    elif spec.startswith("~/"):
        base, rest = home, spec[2:]
    elif spec == "~":
        return home
    elif spec.startswith("/"):
        base, rest = project_dir, spec[1:]  # project-relative
    else:
        base, rest = project_dir, spec
    return _join(base, _nonglob_prefix(rest))


def _resolve_fs_write_path(
    raw: str, *, home: Path, project_dir: Path
) -> Path | None:
    """Resolve a ``sandbox.filesystem`` path (``/`` absolute, ``~/`` home, ``./`` project)."""
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("~/"):
        base, rest = home, raw[2:]
    elif raw == "~":
        return home
    elif raw.startswith("/"):
        base, rest = Path("/"), raw[1:]  # absolute (single slash for fs paths)
    elif raw.startswith("./"):
        base, rest = project_dir, raw[2:]
    else:
        base, rest = project_dir, raw
    return _join(base, _nonglob_prefix(rest))


def _within_write_roots(path: Path | None, write_roots: tuple[Path, ...]) -> bool:
    """Path-component containment; fail-closed on a ``None`` (unparseable) path."""
    if path is None:
        return False
    for root in write_roots:
        try:
            if path == root or path.is_relative_to(root):
                return True
        except (ValueError, TypeError):
            continue
    return False


def _split_permission_entry(entry: str) -> tuple[str, str | None]:
    """``Edit(//x/**)`` -> ``("Edit", "//x/**")``; ``Edit`` -> ``("Edit", None)``."""
    open_paren = entry.find("(")
    if open_paren == -1:
        return entry.strip(), None
    close_paren = entry.rfind(")")
    if close_paren <= open_paren:
        return entry[:open_paren].strip(), None
    return entry[:open_paren].strip(), entry[open_paren + 1 : close_paren].strip()


def _native_write_escapes(
    entry: str, *, write_roots: tuple[Path, ...], home: Path, project_dir: Path
) -> bool:
    tool, spec = _split_permission_entry(entry)
    if tool not in NATIVE_WRITE_TOOLS:
        return False
    if not spec:
        return True  # bare Edit/Write -> unbounded host writes
    resolved = _resolve_permission_spec(spec, home=home, project_dir=project_dir)
    return not _within_write_roots(resolved, write_roots)


# ---------------------------------------------------------------------------
# Evaluation (pure)
# ---------------------------------------------------------------------------


def evaluate_sandbox_environment(
    scope: "SandboxScope",
    ambient: AmbientClaudeSettings,
    *,
    home: Path,
    project_dir: Path,
) -> None:
    """Raise :class:`SandboxEnvironmentUnsafeError` if *ambient* can widen *scope*.

    Pure decision function. Validates the EFFECTIVE set for each dimension (see
    the module docstring's provenance rule) against the scope's write roots and
    egress policy.
    """
    reasons: list[str] = []

    effective_allow = ambient.permission_allow.effective(
        ambient.managed_permission_rules_locked
    )
    write_escapes = [
        entry
        for entry in effective_allow
        if _native_write_escapes(
            entry, write_roots=scope.write_roots, home=home, project_dir=project_dir
        )
    ]
    if write_escapes:
        reasons.append(
            "permissions.allow grants native writes the OS sandbox cannot bound "
            f"(outside the write roots): {write_escapes}"
        )

    # allowWrite has no managed-only lock; the union always applies.
    bad_write_paths = [
        raw
        for raw in ambient.filesystem_allow_write.union()
        if not _within_write_roots(
            _resolve_fs_write_path(raw, home=home, project_dir=project_dir),
            scope.write_roots,
        )
    ]
    if bad_write_paths:
        reasons.append(
            "sandbox.filesystem.allowWrite grants Bash writes outside the "
            f"worktree: {bad_write_paths}"
        )

    if scope.egress != "model+web":
        from .sandbox import MODEL_API_DOMAINS

        permitted = set(MODEL_API_DOMAINS) if scope.egress == "model-only" else set()
        widening_domains = [
            domain
            for domain in ambient.network_allowed_domains.effective(
                ambient.managed_domains_locked
            )
            if domain not in permitted
        ]
        if widening_domains:
            reasons.append(
                "sandbox.network.allowedDomains widens egress beyond the "
                f"{scope.egress} policy: {widening_domains}"
            )

    if ambient.excluded_commands.union():
        reasons.append(
            "sandbox.excludedCommands lets commands run OUTSIDE the sandbox: "
            f"{list(ambient.excluded_commands.union())}"
        )

    if ambient.allow_unix_sockets.union():
        reasons.append(
            "sandbox.allowUnixSockets can bridge out of the sandbox (e.g. a "
            f"docker socket): {list(ambient.allow_unix_sockets.union())}"
        )

    if ambient.permission_hooks.union():
        reasons.append(
            "a PreToolUse/PermissionRequest hook can approve otherwise-denied "
            f"tools under dontAsk: {list(ambient.permission_hooks.union())}"
        )

    if reasons:
        raise SandboxEnvironmentUnsafeError(reasons)


# ---------------------------------------------------------------------------
# Reading + merging the ambient scopes (I/O)
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str)]


def _sandbox_of(data: dict[str, Any]) -> dict[str, Any]:
    sandbox = data.get("sandbox")
    return sandbox if isinstance(sandbox, dict) else {}


def _permission_allow_of(data: dict[str, Any]) -> list[str]:
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return []
    return _str_list(permissions.get("allow"))


def _allow_write_of(data: dict[str, Any]) -> list[str]:
    filesystem = _sandbox_of(data).get("filesystem")
    if not isinstance(filesystem, dict):
        return []
    return _str_list(filesystem.get("allowWrite"))


def _allowed_domains_of(data: dict[str, Any]) -> list[str]:
    network = _sandbox_of(data).get("network")
    if not isinstance(network, dict):
        return []
    return _str_list(network.get("allowedDomains"))


def _excluded_commands_of(data: dict[str, Any]) -> list[str]:
    return _str_list(_sandbox_of(data).get("excludedCommands"))


def _allow_unix_sockets_of(data: dict[str, Any]) -> list[str]:
    sandbox = _sandbox_of(data)
    entries = _str_list(sandbox.get("allowUnixSockets"))
    network = sandbox.get("network")
    if isinstance(network, dict):
        entries = entries + _str_list(network.get("allowUnixSockets"))
    return entries


def _permission_hooks_of(data: dict[str, Any]) -> list[str]:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    return [event for event in PERMISSION_HOOK_EVENTS if hooks.get(event)]


def _user_settings_path(home: Path, config_dir: str | None) -> Path:
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

    Each widening dimension keeps its managed vs non-managed provenance so the
    evaluator can validate the *effective* set. Managed lock flags are read only
    from the managed scope.
    """
    if managed_settings_paths is None:
        managed_settings_paths = default_managed_settings_paths()

    managed_datas = [_load_json(path) for path in managed_settings_paths]
    nonmanaged_datas = [
        _load_json(_user_settings_path(home, config_dir)),
        _load_json(project_dir / ".claude" / "settings.json"),
        _load_json(project_dir / ".claude" / "settings.local.json"),
    ]

    def scoped(getter: Callable[[dict[str, Any]], list[str]]) -> ScopedEntries:
        managed = [entry for data in managed_datas for entry in getter(data)]
        nonmanaged = [entry for data in nonmanaged_datas for entry in getter(data)]
        return ScopedEntries(tuple(managed), tuple(nonmanaged))

    managed_permission_rules_locked = any(
        data.get("allowManagedPermissionRulesOnly") is True for data in managed_datas
    )
    managed_domains_locked = any(
        data.get("allowManagedDomainsOnly") is True for data in managed_datas
    )

    return AmbientClaudeSettings(
        permission_allow=scoped(_permission_allow_of),
        filesystem_allow_write=scoped(_allow_write_of),
        network_allowed_domains=scoped(_allowed_domains_of),
        excluded_commands=scoped(_excluded_commands_of),
        allow_unix_sockets=scoped(_allow_unix_sockets_of),
        permission_hooks=scoped(_permission_hooks_of),
        managed_permission_rules_locked=managed_permission_rules_locked,
        managed_domains_locked=managed_domains_locked,
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
    scope from the session's write root (the worktree) when not supplied.
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
    evaluate_sandbox_environment(scope, ambient, home=home, project_dir=project_dir)
