"""Repo-level guardrail installation for target repositories."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from pathlib import Path
import shlex
import shutil

from ..adapters.git.git_cli import GitCLI, SubprocessCommandRunner
from .config import Config
from .hooks.hooks import detect_agents_from_config, get_adapter, install_hooks_for_config

logger = logging.getLogger(__name__)

DEFAULT_HOOKS_PATH = ".githooks"
MANAGED_PRE_PUSH_MARKER = "Managed by issue-orchestrator harden-repo: pre-push"
MANAGED_VERIFY_MARKER = "Managed by issue-orchestrator harden-repo: verify-pr"
MANAGED_HELPER_MARKER = (
    "Managed by issue-orchestrator harden-repo: block-no-verify helper"
)
VERIFY_PR_RELATIVE_PATH = Path("scripts/verify-pr.sh")
HELPER_RELATIVE_PATH = Path("scripts/agent-hooks/block_no_verify.py")


@dataclass
class RepoHardeningStatus:
    """Observed hardening state for a repository."""

    repo_root: Path
    hooks_path_config: str | None
    hooks_dir: Path
    pre_push_hook: Path
    verify_script: Path
    helper_script: Path
    pre_push_exists: bool
    pre_push_executable: bool
    pre_push_managed: bool
    pre_push_calls_verify: bool
    verify_exists: bool
    verify_executable: bool
    verify_managed: bool
    helper_exists: bool
    helper_executable: bool
    helper_managed: bool
    agent_hooks: dict[str, "AgentHookStatus"] = field(default_factory=dict)


@dataclass
class ManagedAgentHookFileStatus:
    """Observed state for a managed AI-agent hook artifact."""

    path: Path
    exists: bool
    executable: bool
    matches_template: bool | None


@dataclass
class AgentHookStatus:
    """Observed hardening state for one configured AI agent."""

    agent_type: str
    installed: bool
    managed_files: list[ManagedAgentHookFileStatus] = field(default_factory=list)


@dataclass
class RepoHardeningInstallResult:
    """Files written while hardening a repository."""

    repo_root: Path
    hooks_path_config: str
    hooks_dir: Path
    pre_push_hook: Path
    verify_script: Path
    helper_script: Path
    installed_files: list[Path] = field(default_factory=list)
    preserved_files: list[Path] = field(default_factory=list)
    quarantined_files: list[Path] = field(default_factory=list)
    agent_hook_files: dict[str, list[Path]] = field(default_factory=dict)


class RepoHardeningError(RuntimeError):
    """Raised when repo hardening cannot be applied safely."""


def inspect_repo_hardening(
    repo_root: Path,
    *,
    config: Config | None = None,
) -> RepoHardeningStatus:
    """Return the current repo-hardening status for *repo_root*."""
    repo_root = repo_root.resolve()
    hooks_path_config = _get_local_hooks_path(repo_root)
    hooks_dir = _resolve_active_hooks_dir(repo_root, hooks_path_config)

    pre_push_hook = hooks_dir / "pre-push"
    verify_script = repo_root / VERIFY_PR_RELATIVE_PATH
    helper_script = repo_root / HELPER_RELATIVE_PATH

    pre_push_content = _safe_read_text(pre_push_hook)
    verify_content = _safe_read_text(verify_script)
    helper_content = _safe_read_text(helper_script)
    agent_hooks = _inspect_agent_hooks(config, repo_root) if config is not None else {}

    return RepoHardeningStatus(
        repo_root=repo_root,
        hooks_path_config=hooks_path_config,
        hooks_dir=hooks_dir,
        pre_push_hook=pre_push_hook,
        verify_script=verify_script,
        helper_script=helper_script,
        pre_push_exists=pre_push_hook.exists(),
        pre_push_executable=_is_executable(pre_push_hook),
        pre_push_managed=MANAGED_PRE_PUSH_MARKER in pre_push_content,
        pre_push_calls_verify="scripts/verify-pr.sh" in pre_push_content,
        verify_exists=verify_script.exists(),
        verify_executable=_is_executable(verify_script),
        verify_managed=MANAGED_VERIFY_MARKER in verify_content,
        helper_exists=helper_script.exists(),
        helper_executable=_is_executable(helper_script),
        helper_managed=MANAGED_HELPER_MARKER in helper_content,
        agent_hooks=agent_hooks,
    )


def harden_repo(
    config: Config,
    *,
    target_root: Path | None = None,
    validation_cmd: str | None = None,
    hooks_path: str | None = None,
) -> RepoHardeningInstallResult:
    """Install repo-level guardrails and agent hooks for a target repository."""
    repo_root = (target_root or config.repo_root).resolve()
    git = _new_git_cli()
    local_hooks_path = _get_local_hooks_path(repo_root, git)
    resolved_validation_cmd = (validation_cmd or config.validation.cmd or "").strip()
    if not resolved_validation_cmd:
        raise RepoHardeningError(
            "validation.cmd is not configured. Set it in YAML or pass --validation-cmd."
        )

    hooks_path_value, hooks_dir = _resolve_repo_hooks_dir(
        repo_root,
        requested=hooks_path,
        local_hooks_path=local_hooks_path,
    )
    if local_hooks_path != hooks_path_value:
        _set_local_hooks_path(repo_root, hooks_path_value, git)

    hooks_dir.mkdir(parents=True, exist_ok=True)

    result = RepoHardeningInstallResult(
        repo_root=repo_root,
        hooks_path_config=hooks_path_value,
        hooks_dir=hooks_dir,
        pre_push_hook=hooks_dir / "pre-push",
        verify_script=repo_root / VERIFY_PR_RELATIVE_PATH,
        helper_script=repo_root / HELPER_RELATIVE_PATH,
    )

    _install_verify_script(result.verify_script, resolved_validation_cmd, result)
    _install_helper_script(result.helper_script, result)
    _install_repo_pre_push_hook(result.pre_push_hook, result.verify_script, result)

    installed = install_hooks_for_config(config, repo_root)
    result.agent_hook_files = {
        agent_type.value: paths for agent_type, paths in installed.items()
    }

    return result


def _resolve_repo_hooks_dir(
    repo_root: Path,
    requested: str | None = None,
    local_hooks_path: str | None = None,
) -> tuple[str, Path]:
    requested_value = (requested or "").strip()
    if requested_value:
        return _resolve_hooks_dir_value(repo_root, requested_value)

    local_hooks_path_value = (local_hooks_path or "").strip()
    if local_hooks_path_value:
        try:
            return _resolve_hooks_dir_value(repo_root, local_hooks_path_value)
        except RepoHardeningError:
            # Recover from inherited worktree/common-config drift by resetting to
            # the managed repo-local hooks path instead of preserving it.
            pass

    return _resolve_hooks_dir_value(repo_root, DEFAULT_HOOKS_PATH)


def _resolve_hooks_dir_value(repo_root: Path, hooks_path_value: str) -> tuple[str, Path]:
    hooks_dir = Path(hooks_path_value)
    if hooks_dir.is_absolute():
        resolved = hooks_dir.resolve()
    else:
        resolved = (repo_root / hooks_dir).resolve()

    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise RepoHardeningError(
            "core.hooksPath must resolve inside the repository. "
            f"Current value: {hooks_path_value}"
        ) from exc

    return hooks_path_value, resolved


def _inspect_agent_hooks(
    config: Config,
    repo_root: Path,
) -> dict[str, AgentHookStatus]:
    statuses: dict[str, AgentHookStatus] = {}
    unique_types = set(detect_agents_from_config(config).values())

    for agent_type in sorted(unique_types, key=lambda value: value.value):
        adapter = get_adapter(agent_type)
        layout = adapter.installation_layout(repo_root)
        managed_files: list[ManagedAgentHookFileStatus] = []
        for artifact in layout.managed_files:
            matches_template = None
            if artifact.template_path is not None:
                matches_template = _safe_read_text(artifact.path) == _safe_read_text(
                    artifact.template_path
                )
            managed_files.append(
                ManagedAgentHookFileStatus(
                    path=artifact.path,
                    exists=artifact.path.exists(),
                    executable=_is_executable(artifact.path),
                    matches_template=matches_template,
                )
            )

        statuses[agent_type.value] = AgentHookStatus(
            agent_type=agent_type.value,
            installed=adapter.is_installed(repo_root),
            managed_files=managed_files,
        )

    return statuses


def _resolve_active_hooks_dir(repo_root: Path, hooks_path_config: str | None) -> Path:
    if hooks_path_config:
        hooks_dir = Path(hooks_path_config)
        if hooks_dir.is_absolute():
            return hooks_dir.resolve()
        return (repo_root / hooks_dir).resolve()
    return (repo_root / ".git" / "hooks").resolve()


def _new_git_cli() -> GitCLI:
    return GitCLI(runner=SubprocessCommandRunner(), default_timeout_s=30)


def _get_local_hooks_path(repo_root: Path, git: GitCLI | None = None) -> str | None:
    if git is None:
        git = _new_git_cli()
    result = git.run(
        repo_root,
        ["config", "--local", "--get", "core.hooksPath"],
        check=False,
    )
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def _set_local_hooks_path(
    repo_root: Path,
    hooks_path: str,
    git: GitCLI | None = None,
) -> None:
    if git is None:
        git = _new_git_cli()
    result = git.run(
        repo_root,
        ["config", "--local", "core.hooksPath", hooks_path],
        check=False,
    )
    if result.returncode != 0:
        raise RepoHardeningError(
            f"Failed to set core.hooksPath to {hooks_path}: {(result.stderr or '').strip()}"
        )


def _install_verify_script(
    verify_script: Path,
    validation_cmd: str,
    result: RepoHardeningInstallResult,
) -> None:
    verify_script.parent.mkdir(parents=True, exist_ok=True)
    rendered = _render_verify_pr_script(validation_cmd)
    _write_executable_file(verify_script, rendered, result)


def _install_helper_script(
    helper_script: Path,
    result: RepoHardeningInstallResult,
) -> None:
    helper_script.parent.mkdir(parents=True, exist_ok=True)
    source_path = Path(__file__).parent / "hooks" / "block_no_verify.py"
    rendered = _render_helper_script(source_path)
    _write_executable_file(helper_script, rendered, result)


def _install_repo_pre_push_hook(
    pre_push_hook: Path,
    verify_script: Path,
    result: RepoHardeningInstallResult,
) -> None:
    pre_push_hook.parent.mkdir(parents=True, exist_ok=True)
    project_hook = pre_push_hook.parent / "pre-push.project"

    quarantine_managed_hook_file(project_hook, result.quarantined_files)

    if pre_push_hook.exists():
        current = _safe_read_text(pre_push_hook)
        if MANAGED_PRE_PUSH_MARKER not in current:
            shutil.copy2(pre_push_hook, project_hook)
            project_hook.chmod(0o755)
            result.preserved_files.append(project_hook)

    rendered = _render_repo_pre_push_hook(verify_script, result.repo_root)
    _write_executable_file(pre_push_hook, rendered, result)


def quarantine_managed_hook_file(
    target: Path,
    quarantined: list[Path] | None = None,
) -> Path | None:
    """Rename *target* aside if it contains the managed pre-push marker.

    A ``pre-push.project`` that itself contains the managed wrapper marker is
    corruption: the wrapper executes ``pre-push.project`` by path, so if that
    path resolves to the wrapper itself the push forkbombs. Any file whose role
    is "non-managed delegate" but whose content is the managed wrapper is, by
    definition, corrupt — rename it out of the way so it can never run.

    Returns the new path when a file was quarantined, else ``None``. Appends to
    *quarantined* when provided (for install-result reporting).
    """
    if not target.exists():
        return None
    content = _safe_read_text(target)
    if MANAGED_PRE_PUSH_MARKER not in content:
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine_path = target.with_name(f"{target.name}.quarantined-{timestamp}")
    # Defensive: if the timestamp collides with an existing file, suffix a counter.
    counter = 1
    while quarantine_path.exists():
        quarantine_path = target.with_name(
            f"{target.name}.quarantined-{timestamp}-{counter}"
        )
        counter += 1
    target.rename(quarantine_path)
    logger.warning(
        "Quarantined corrupt hook file: %s -> %s (contained managed wrapper marker; "
        "would have caused pre-push recursion)",
        target,
        quarantine_path,
    )
    if quarantined is not None:
        quarantined.append(quarantine_path)
    return quarantine_path


def _render_verify_pr_script(validation_cmd: str) -> str:
    quoted = shlex.quote(validation_cmd)
    return f"""#!/usr/bin/env bash
set -euo pipefail

# {MANAGED_VERIFY_MARKER}

repo_root="$(cd "$(dirname "${{BASH_SOURCE[0]}}")/.." && pwd)"
cd "$repo_root"

validation_cmd={quoted}

if ! git diff --quiet --exit-code -- . || ! git diff --cached --quiet --exit-code -- .; then
  echo >&2 "verify-pr: requires a clean tracked worktree."
  echo >&2 "Commit or stash tracked changes, then rerun scripts/verify-pr.sh."
  exit 1
fi

echo "verify-pr: running $validation_cmd"
bash -lc "$validation_cmd"
"""


def _render_helper_script(source_path: Path) -> str:
    helper_body = source_path.read_text()
    return f"#!/usr/bin/env python3\n# {MANAGED_HELPER_MARKER}\n\n{helper_body}"


def _render_repo_pre_push_hook(verify_script: Path, repo_root: Path) -> str:
    verify_rel = verify_script.relative_to(repo_root)
    return f"""#!/usr/bin/env bash
set -euo pipefail

# {MANAGED_PRE_PUSH_MARKER}

HOOK_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel)"
LOG_FILE="$HOOK_DIR/pre-push.log"
PROJECT_HOOK="$HOOK_DIR/pre-push.project"
VERIFY_SCRIPT="$REPO_ROOT/{verify_rel.as_posix()}"
MANAGED_MARKER='{MANAGED_PRE_PUSH_MARKER}'

log() {{
  printf "%s %s\\n" "$(date -Iseconds)" "$1" >> "$LOG_FILE"
}}

log "repo-pre-push-started"

# Recursion guard: never exec pre-push.project if it contains the managed
# marker. That means it is a copy of this wrapper (corruption) and executing
# it would forkbomb the push.
#
# MAIN-REPO POLICY: log + skip the project hook, continue to verify-pr.
# Stranding the operator (unable to push from the main checkout) is worse
# than pushing with the repo's lint/test gate temporarily bypassed — they
# can still run verify-pr, and doctor will flag the corruption for repair.
# The worktree wrapper takes the opposite stance (hard-fail) because
# worktrees are disposable — see _chained_hook_script in _worktree_hooks.py.
if [ -x "$PROJECT_HOOK" ] && grep -qF "$MANAGED_MARKER" "$PROJECT_HOOK" 2>/dev/null; then
  log "project-hook-skipped reason=managed-marker-detected path=$PROJECT_HOOK"
  echo "pre-push: refusing to exec managed wrapper as project hook: $PROJECT_HOOK" >&2
elif [ -x "$PROJECT_HOOK" ]; then
  log "project-hook-starting"
  if "$PROJECT_HOOK" "$@"; then
    log "project-hook exit=0"
  else
    project_exit=$?
    log "project-hook exit=$project_exit"
    exit "$project_exit"
  fi
else
  log "project-hook-skipped"
fi

if [ ! -x "$VERIFY_SCRIPT" ]; then
  log "verify-script-missing"
  echo "pre-push: missing executable $VERIFY_SCRIPT" >&2
  exit 1
fi

log "verify-pr-starting"
if "$VERIFY_SCRIPT"; then
  log "verify-pr exit=0"
else
  verify_exit=$?
  log "verify-pr exit=$verify_exit"
  exit "$verify_exit"
fi

log "repo-pre-push-completed"
"""


def _write_executable_file(
    path: Path,
    content: str,
    result: RepoHardeningInstallResult,
) -> None:
    path.write_text(content)
    path.chmod(0o755)
    result.installed_files.append(path)


def _safe_read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text()
    except OSError:
        return ""


def _is_executable(path: Path) -> bool:
    return path.exists() and path.is_file() and bool(path.stat().st_mode & 0o111)
