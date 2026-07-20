"""Fail-closed launch guard for the claude-code OS sandbox (ADR-0034).

WHY A MANAGED LOCKDOWN, NOT AN AMBIENT PARSER. The write/egress *bounds* of the
sandbox cannot be owned by inspecting settings at launch, because Claude Code:

- **merges** settings across many sources — server-managed, macOS plist, Windows
  registry, ``managed-settings.d/*.json``, user, project, and a worktree-local
  file that resolves through the worktree to the MAIN checkout — several of which
  are not portably readable; and
- **hot-reloads** most keys (``permissions``, ``hooks``) mid-session, so a
  point-in-time snapshot is racy.

So a build-time parser of a subset of sources can neither be authoritative nor
durable. What IS both authoritative and durable is a **managed lockdown**: a
managed policy (delivered out-of-band, in the write-protected managed directory a
sandboxed command cannot modify) that sets ``allowManagedPermissionRulesOnly`` so
Claude ignores every non-managed ``allow``/``ask``/``deny`` rule regardless of
which source or reload produced it. Under that lock the managed policy IS the
effective permission policy, ambient sources are inert, and the agent cannot
widen itself.

This module is therefore the deployment owner that VERIFIES the lockdown: it
reads only the authoritative managed file scope and refuses to launch a
sandboxed agent unless (a) the permission-rules lock is set, (b) egress is
neutralized for a restricted-egress scope, and (c) the managed policy does not
itself widen the claimed bounds. It parses no ambient user/project/local source.

RESIDUAL (documented, not silently covered): ``sandbox.filesystem.allowWrite``,
``sandbox.excludedCommands``, and ``sandbox.allowUnixSockets`` have no
managed-only lock, so a *pre-existing* ambient entry can still widen Bash
subprocess writes/exec. A sandboxed command cannot ADD one (Claude denies writes
to settings.json at every scope), so the agent cannot self-widen; a durable bound
against an operator/external-placed entry requires the orchestrator-owned OS jail
(seatbelt/bubblewrap wrapping the process), tracked as the follow-up in #6859.

The DENY floor (secret ``credentials.files`` + ``permissions.deny``) is emitted
by the adapter and holds independently: deny is deny-first and merge-narrow. Note
that ``PreToolUse``/``PermissionRequest`` hooks are deny-first too, so they cannot
approve a denied tool — they are NOT a widening dimension.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from issue_orchestrator.domain.sandbox_scope import SandboxScope

__all__ = [
    "NATIVE_WRITE_TOOLS",
    "ManagedLockdown",
    "SandboxEnvironmentUnsafeError",
    "assert_claude_sandbox_environment_safe",
    "default_managed_settings_dirs",
    "default_managed_settings_paths",
    "evaluate_managed_lockdown",
    "read_managed_lockdown",
]

# Native write tools governed by the PERMISSION layer (not the Bash sandbox). A
# managed ``permissions.allow`` of any of these, not confined to a write root,
# grants host-wide native writes even under the lockdown, so we validate them.
NATIVE_WRITE_TOOLS: frozenset[str] = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit"}
)

# Sandbox booleans that WEAKEN isolation; a managed policy that sets any true is
# rejected (the adapter also pins them false in --settings for lower scopes).
_WEAKENER_BOOLEANS: tuple[str, ...] = (
    "allowAppleEvents",
    "enableWeakerNetworkIsolation",
    "enableWeakerNestedSandbox",
)

_GLOB_CHARS = ("*", "?", "[")


def default_managed_settings_paths() -> tuple[Path, ...]:
    """The base managed-settings.json locations (macOS, Linux/WSL)."""
    return (
        Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
        Path("/etc/claude-code/managed-settings.json"),
    )


def default_managed_settings_dirs() -> tuple[Path, ...]:
    """The managed-settings.d drop-in directories (merged over the base file)."""
    return (
        Path("/Library/Application Support/ClaudeCode/managed-settings.d"),
        Path("/etc/claude-code/managed-settings.d"),
    )


class SandboxEnvironmentUnsafeError(RuntimeError):
    """Raised when the managed lockdown required to bound the sandbox is absent
    or the managed policy itself widens the claimed bounds."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        super().__init__(
            "Refusing to launch a sandboxed agent: the write/egress bounds "
            "require an installed managed lockdown, and it is absent or "
            "insufficient. " + "; ".join(reasons) + ". Install a managed policy "
            "with allowManagedPermissionRulesOnly (and the domain lock for "
            "restricted egress) in the managed settings directory."
        )


@dataclass(frozen=True)
class ManagedLockdown:
    """The authoritative managed policy view relevant to the sandbox bounds."""

    present: bool
    permission_rules_locked: bool
    domains_locked: bool
    widening_findings: tuple[str, ...] = field(default=())


# ---------------------------------------------------------------------------
# Path resolution + confinement (fail-closed, path-component based)
# ---------------------------------------------------------------------------


def _has_glob(component: str) -> bool:
    return any(ch in component for ch in _GLOB_CHARS)


def _nonglob_prefix(rest: str) -> list[str]:
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


def _resolve_permission_spec(spec: str, *, home: Path, project_dir: Path) -> Path | None:
    """Read/Edit specifier -> non-glob absolute parent (``//`` abs, ``/`` proj, ``~/`` home)."""
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
        base, rest = project_dir, spec[1:]
    else:
        base, rest = project_dir, spec
    return _join(base, _nonglob_prefix(rest))


def _resolve_fs_write_path(raw: str, *, home: Path, project_dir: Path) -> Path | None:
    """sandbox.filesystem path -> non-glob absolute parent (``/`` abs, ``~/`` home, ``./`` proj)."""
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("~/"):
        base, rest = home, raw[2:]
    elif raw == "~":
        return home
    elif raw.startswith("/"):
        base, rest = Path("/"), raw[1:]
    elif raw.startswith("./"):
        base, rest = project_dir, raw[2:]
    else:
        base, rest = project_dir, raw
    return _join(base, _nonglob_prefix(rest))


def _within_write_roots(path: Path | None, write_roots: tuple[Path, ...]) -> bool:
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
        return True
    resolved = _resolve_permission_spec(spec, home=home, project_dir=project_dir)
    return not _within_write_roots(resolved, write_roots)


# ---------------------------------------------------------------------------
# Reading the managed scope
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _managed_documents(
    managed_settings_paths: tuple[Path, ...], managed_settings_dirs: tuple[Path, ...]
) -> list[dict[str, Any]]:
    """Base managed-settings.json files, then sorted managed-settings.d/*.json."""
    docs = [_load_json(path) for path in managed_settings_paths]
    for directory in managed_settings_dirs:
        try:
            drop_ins = sorted(directory.glob("*.json"))
        except OSError:
            drop_ins = []
        docs.extend(_load_json(path) for path in drop_ins)
    return docs


def _bool_key(docs: list[dict[str, Any]], *paths: tuple[str, ...]) -> bool:
    """True if any managed document sets any of the given nested key paths to true.

    Checked in multiple locations because the domain lock's exact nesting is
    ambiguous across doc versions (top-level vs sandbox.network); requiring it at
    ANY documented location keeps the check fail-closed (only PASS on a real True).
    """
    for doc in docs:
        for path in paths:
            node: Any = doc
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            if node is True:
                return True
    return False


def _str_list(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _sandbox_of(doc: dict[str, Any]) -> dict[str, Any]:
    sandbox = doc.get("sandbox")
    return sandbox if isinstance(sandbox, dict) else {}


def _network_findings(
    network: dict[str, Any], scope: "SandboxScope"
) -> list[str]:
    from .sandbox import MODEL_API_DOMAINS

    findings: list[str] = []
    if scope.egress != "model+web":
        permitted = set(MODEL_API_DOMAINS) if scope.egress == "model-only" else set()
        bad_dom = [d for d in _str_list(network.get("allowedDomains")) if d not in permitted]
        if bad_dom:
            findings.append(f"managed sandbox.network.allowedDomains beyond {scope.egress}: {bad_dom}")
    if network.get("allowAllUnixSockets") is True:
        findings.append("managed sandbox.network.allowAllUnixSockets is true")
    return findings


def _managed_document_findings(
    doc: dict[str, Any], scope: "SandboxScope", *, home: Path, project_dir: Path
) -> list[str]:
    """Ways a single managed document itself widens the claimed bounds."""
    findings: list[str] = []
    permissions = doc.get("permissions")
    allow = _str_list(permissions.get("allow")) if isinstance(permissions, dict) else []
    write_escapes = [
        e
        for e in allow
        if _native_write_escapes(
            e, write_roots=scope.write_roots, home=home, project_dir=project_dir
        )
    ]
    if write_escapes:
        findings.append(f"managed permissions.allow native writes outside roots: {write_escapes}")

    sandbox = _sandbox_of(doc)
    filesystem = sandbox.get("filesystem")
    allow_write = _str_list(filesystem.get("allowWrite")) if isinstance(filesystem, dict) else []
    bad_write = [
        p
        for p in allow_write
        if not _within_write_roots(
            _resolve_fs_write_path(p, home=home, project_dir=project_dir), scope.write_roots
        )
    ]
    if bad_write:
        findings.append(f"managed sandbox.filesystem.allowWrite outside worktree: {bad_write}")

    network = sandbox.get("network")
    if isinstance(network, dict):
        findings.extend(_network_findings(network, scope))

    if _str_list(sandbox.get("excludedCommands")):
        findings.append("managed sandbox.excludedCommands present")
    if _str_list(sandbox.get("allowUnixSockets")):
        findings.append("managed sandbox.allowUnixSockets present")
    findings.extend(
        f"managed sandbox.{name} is true"
        for name in _WEAKENER_BOOLEANS
        if sandbox.get(name) is True
    )
    return findings


def read_managed_lockdown(
    scope: "SandboxScope",
    *,
    home: Path,
    project_dir: Path,
    managed_settings_paths: tuple[Path, ...] | None = None,
    managed_settings_dirs: tuple[Path, ...] | None = None,
) -> ManagedLockdown:
    """Read the authoritative managed scope and summarize the lockdown."""
    if managed_settings_paths is None:
        managed_settings_paths = default_managed_settings_paths()
    if managed_settings_dirs is None:
        managed_settings_dirs = default_managed_settings_dirs()

    docs = _managed_documents(managed_settings_paths, managed_settings_dirs)
    findings: list[str] = []
    for doc in docs:
        findings.extend(
            _managed_document_findings(doc, scope, home=home, project_dir=project_dir)
        )

    return ManagedLockdown(
        present=any(docs),
        permission_rules_locked=_bool_key(docs, ("allowManagedPermissionRulesOnly",)),
        domains_locked=_bool_key(
            docs,
            ("allowManagedDomainsOnly",),
            ("sandbox", "network", "allowManagedDomainsOnly"),
        ),
        widening_findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# Evaluation (pure)
# ---------------------------------------------------------------------------


def evaluate_managed_lockdown(scope: "SandboxScope", lockdown: ManagedLockdown) -> None:
    """Raise unless the managed lockdown durably bounds *scope*."""
    reasons: list[str] = []
    if not lockdown.present:
        reasons.append("no managed settings policy is installed")
    if not lockdown.permission_rules_locked:
        reasons.append(
            "allowManagedPermissionRulesOnly is not set, so ambient allow rules "
            "(native writes, WebFetch domains) are not neutralized"
        )
    if scope.egress != "model+web" and not lockdown.domains_locked:
        reasons.append(
            "allowManagedDomainsOnly is not set, so ambient network.allowedDomains "
            "can widen egress"
        )
    reasons.extend(lockdown.widening_findings)
    if reasons:
        raise SandboxEnvironmentUnsafeError(reasons)


def assert_claude_sandbox_environment_safe(
    scope: "SandboxScope",
    *,
    home: Path | None = None,
    project_dir: Path | None = None,
    managed_settings_paths: tuple[Path, ...] | None = None,
    managed_settings_dirs: tuple[Path, ...] | None = None,
) -> None:
    """Verify an installed managed lockdown bounds *scope*, or fail closed."""
    if home is None:
        home = Path(os.environ.get("HOME") or Path.home())
    if project_dir is None:
        project_dir = scope.write_roots[0] if scope.write_roots else Path.cwd()

    lockdown = read_managed_lockdown(
        scope,
        home=home,
        project_dir=project_dir,
        managed_settings_paths=managed_settings_paths,
        managed_settings_dirs=managed_settings_dirs,
    )
    evaluate_managed_lockdown(scope, lockdown)
