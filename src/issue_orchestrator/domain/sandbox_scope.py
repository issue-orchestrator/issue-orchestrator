"""Per-session sandbox scope — a provider-agnostic value object + policy.

Every agent today launches with claude-code ``permission_mode:
bypassPermissions`` (yolo — full local access, no OS isolation). This module
introduces a first-class :class:`SandboxScope` that the orchestrator computes
per session from the agent's *role*, plus :func:`compute_session_scope`, the
pure policy that decides whether an agent is sandboxed and, if so, with what
read/write/egress bounds.

Layering: this is a pure domain value object with no I/O and no dependency on
any provider. Translating a :class:`SandboxScope` into a specific CLI's flags
is a *provider adapter* concern (see
``execution.agent_runner_providers.sandbox``), keeping the scope itself
provider-agnostic (ADR-0034).

Additive/opt-in: :func:`compute_session_scope` returns ``None`` for every agent
that has not opted in (``AgentConfig.sandbox`` is ``False`` by default), which
leaves the existing ``bypassPermissions`` launch path byte-for-byte unchanged.
Only an explicitly opted-in agent receives a bounded scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args

from .session_key import TaskKind

if TYPE_CHECKING:
    from .models import AgentConfig

__all__ = [
    "DEFAULT_SANDBOX_DENY_ENV",
    "DEFAULT_SANDBOX_DENY_READ_FILES",
    "REVIEW_EXCHANGE_CODER_TASK_KIND",
    "REVIEW_EXCHANGE_REVIEWER_TASK_KIND",
    "SandboxEgress",
    "SandboxRole",
    "SandboxScope",
    "SandboxScopeContext",
    "compute_session_scope",
]

# Egress posture for a sandboxed session. This governs a sandboxed agent's
# **Bash subprocess** network only — the claude-code sandbox isolates Bash
# child processes, not the agent process itself, so the agent always reaches
# the model API regardless of this setting.
# - "none":       Bash subprocesses reach no network at all.
# - "model-only": Bash subprocesses reach only the model API host — no source
#                 host, no web. (git-over-https to the source host is therefore
#                 blocked from Bash; the orchestrator, not the agent, performs
#                 pushes/PR creation, and a linked worktree's shared ``.git`` is
#                 reachable at the filesystem layer.)
# - "model+web":  no Bash network restriction (opt-in escape hatch).
SandboxEgress = Literal["none", "model-only", "model+web"]

# Credentials scrubbed from a sandboxed agent's process environment. This
# restates the hygiene denylist enforced at the process-env layer
# (``execution.agent_runner_env.DEFAULT_FORBIDDEN_ENV_VARS``) so the domain
# value object stays pure (no infra import). The sandbox enforces it a second
# time at the OS layer via the provider's credentials-deny translation, so a
# credential the orchestrator forgot to scrub from ``os.environ`` still cannot
# reach the sandboxed process.
DEFAULT_SANDBOX_DENY_ENV: tuple[str, ...] = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_OAUTH_TOKEN",
    "GH_APP_ID",
    "GH_APP_PRIVATE_KEY",
    "GH_INSTALLATION_ID",
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "SSH_AUTH_SOCK",
)

# Home-relative secret locations whose *reads* must be denied inside the
# sandbox. This is the fail-closed complement to :data:`DEFAULT_SANDBOX_DENY_ENV`
# (which scrubs credential *env vars*): the claude-code sandbox leaves reads
# open to the whole machine by default, so without an explicit deny a sandboxed
# agent could still ``cat ~/.ssh/id_ed25519`` or the orchestrator's own
# ``~/.issue-orchestrator/api-token``. The provider adapter translates these
# into narrow, un-widenable credential-deny entries (see
# ``execution.agent_runner_providers.sandbox.build_claude_sandbox_settings``).
#
# Paths are home-relative (``~/`` prefix) — a universal, provider-portable
# convention. They cover the well-known fixed secret stores:
#   - ``~/.ssh`` / ``~/.aws`` / ``~/.gnupg``     — key material
#   - ``~/.config/gh``                            — GitHub CLI hosts.yml token
#   - ``~/.issue-orchestrator`` /                 — this tool's Control API +
#     ``~/.config/issue-orchestrator``              agent-callback tokens
#   - ``~/.netrc`` / ``~/.npmrc`` / ``~/.pypirc`` — HTTP/registry credentials
# The GitHub *App* private key path is operator-configured
# (``github.app.private_key_path``) with no fixed default, so it is not a
# static entry here; a home-based key is still covered by the adapter's
# ``denyRead: ["~/"]`` boundary, and an absolute path outside home is a
# documented limitation (see the adapter module docstring).
DEFAULT_SANDBOX_DENY_READ_FILES: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gh",
    "~/.issue-orchestrator",
    "~/.config/issue-orchestrator",
    "~/.netrc",
    "~/.npmrc",
    "~/.pypirc",
)

# Review-exchange launches use per-role task kinds (built as
# ``review_exchange_{role}`` in ``execution.persistent_session_exchange`` and
# matched in ``resources.get_completion_instructions``). They are not
# :class:`TaskKind` enum values, but the sandbox role policy recognizes them so
# an opted-in exchange agent resolves to its true role instead of silently
# landing on the unknown-task CODER fail-safe below.
REVIEW_EXCHANGE_CODER_TASK_KIND = "review_exchange_coder"
REVIEW_EXCHANGE_REVIEWER_TASK_KIND = "review_exchange_reviewer"


class SandboxRole(Enum):
    """The sandbox-relevant role a session plays.

    Distinct from :class:`TaskKind`: several task kinds collapse to one sandbox
    role (a ``CODE`` and a ``REWORK`` session are both a ``CODER``). The role is
    the axis the scope policy branches on, and the seam future policies extend
    (e.g. a tech-lead's evidence-map-driven read scope).
    """

    CODER = "coder"
    REVIEWER = "reviewer"
    TECH_LEAD = "tech-lead"


@dataclass(frozen=True)
class SandboxScope:
    """A bounded, provider-agnostic sandbox for one agent session.

    A pure value object: it describes *what* the agent may touch, not *how* a
    given CLI enforces it.

    Attributes:
        read_roots: Filesystem roots to re-allow reads of. Non-empty. NOTE: the
            claude-code sandbox leaves reads OPEN to the whole machine by
            default; ``read_roots`` is not a read *boundary* on its own — the
            provider adapter denies a wider region (the home dir) and re-allows
            these roots within it, and layers a fail-closed credential deny on
            top (see the adapter module docstring).
        write_roots: Filesystem roots the agent may write. Writes are denied
            outside the cwd by default; every write root is expected to also be
            readable (a subset relationship the provider adapter preserves).
        egress: Network posture (see :data:`SandboxEgress`).
        deny_env: Environment variables that must be scrubbed from the
            sandboxed process (credentials).
        deny_read_files: Home-relative secret paths (``~/`` prefix) whose reads
            must be denied — the fail-closed secret layer (see
            :data:`DEFAULT_SANDBOX_DENY_READ_FILES`).
    """

    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    egress: SandboxEgress
    deny_env: tuple[str, ...]
    deny_read_files: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.read_roots:
            raise ValueError("SandboxScope.read_roots must not be empty")
        if self.egress not in get_args(SandboxEgress):
            allowed = ", ".join(get_args(SandboxEgress))
            raise ValueError(
                f"SandboxScope.egress must be one of {allowed}; got {self.egress!r}"
            )


@dataclass(frozen=True)
class SandboxScopeContext:
    """The per-launch facts the scope policy needs.

    Kept minimal and provider-agnostic: the role is derived from ``task_kind``
    and the bounds are anchored on the session's ``worktree``.
    """

    task_kind: str
    worktree: Path


_CODER_TASK_KINDS = frozenset(
    {TaskKind.CODE.value, TaskKind.REWORK.value, REVIEW_EXCHANGE_CODER_TASK_KIND}
)
_REVIEWER_TASK_KINDS = frozenset(
    {
        TaskKind.REVIEW.value,
        TaskKind.RETROSPECTIVE_REVIEW.value,
        REVIEW_EXCHANGE_REVIEWER_TASK_KIND,
    }
)
_TECH_LEAD_TASK_KINDS = frozenset({TaskKind.TRIAGE.value})


def _role_for_task_kind(task_kind: str) -> SandboxRole:
    """Map a task kind to its sandbox role.

    An unrecognized task kind fails safe to :attr:`SandboxRole.CODER` (the
    most bounded worktree-only policy) rather than leaving an opted-in agent
    unsandboxed — the sandbox is a security floor, so ambiguity must never
    widen it.
    """
    if task_kind in _REVIEWER_TASK_KINDS:
        return SandboxRole.REVIEWER
    if task_kind in _TECH_LEAD_TASK_KINDS:
        return SandboxRole.TECH_LEAD
    if task_kind in _CODER_TASK_KINDS:
        return SandboxRole.CODER
    return SandboxRole.CODER


def compute_session_scope(
    agent_config: "AgentConfig",
    context: SandboxScopeContext,
) -> SandboxScope | None:
    """Compute the sandbox scope for a session, or ``None`` when not opted in.

    Returns ``None`` unless the agent has explicitly opted in
    (``AgentConfig.sandbox``), so the default launch path (``bypassPermissions``)
    is left byte-for-byte unchanged. When opted in, the scope is derived from
    the session's role:

    - **coder / reviewer** (this slice): read + write the session's own
      worktree, ``model-only`` egress, credentials denied.
    - **tech-lead**: bounded to the worktree for now — the evidence-map-driven
      read scope is a documented follow-up (the evidence map is not yet on this
      branch). A tech-lead is therefore never left on yolo; it just does not yet
      receive the *wider* read set it will eventually need.

    TODO(ADR-0034 follow-up): once the triage evidence map lands, give
    :attr:`SandboxRole.TECH_LEAD` a read scope spanning the worktrees named by
    the evidence map (read-only), keeping write bounded to its own worktree.
    """
    if not getattr(agent_config, "sandbox", False):
        return None

    role = _role_for_task_kind(context.task_kind)
    worktree = context.worktree

    # This slice: coder, reviewer, and (provisionally) tech-lead all get a
    # worktree-bounded read+write scope with model-only egress and credentials
    # denied. The role is threaded so a later slice can widen TECH_LEAD's read
    # roots without touching the coder/reviewer policy.
    _ = role  # role currently uniform; retained as the extension seam.
    return SandboxScope(
        read_roots=(worktree,),
        write_roots=(worktree,),
        egress="model-only",
        deny_env=DEFAULT_SANDBOX_DENY_ENV,
        deny_read_files=DEFAULT_SANDBOX_DENY_READ_FILES,
    )
