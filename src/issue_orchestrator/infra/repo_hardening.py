"""Repo-level guardrail installation for target repositories."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shlex
import shutil

from ..adapters.git.git_cli import GitCLI, SubprocessCommandRunner
from .config import Config
from .hooks.hooks import install_hooks_for_config

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
    helper_managed: bool


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
    agent_hook_files: dict[str, list[Path]] = field(default_factory=dict)


class RepoHardeningError(RuntimeError):
    """Raised when repo hardening cannot be applied safely."""


def inspect_repo_hardening(repo_root: Path) -> RepoHardeningStatus:
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
        helper_managed=MANAGED_HELPER_MARKER in helper_content,
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
    hooks_path_value = (
        requested or local_hooks_path or DEFAULT_HOOKS_PATH
    ).strip()
    if not hooks_path_value:
        hooks_path_value = DEFAULT_HOOKS_PATH

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
    helper_body = source_path.read_text()
    rendered = f"#!/usr/bin/env python3\n# {MANAGED_HELPER_MARKER}\n\n{helper_body}"
    _write_executable_file(helper_script, rendered, result)


def _install_repo_pre_push_hook(
    pre_push_hook: Path,
    verify_script: Path,
    result: RepoHardeningInstallResult,
) -> None:
    pre_push_hook.parent.mkdir(parents=True, exist_ok=True)
    project_hook = pre_push_hook.parent / "pre-push.project"

    if pre_push_hook.exists():
        current = _safe_read_text(pre_push_hook)
        if MANAGED_PRE_PUSH_MARKER not in current:
            shutil.copy2(pre_push_hook, project_hook)
            project_hook.chmod(0o755)
            result.preserved_files.append(project_hook)

    rendered = _render_repo_pre_push_hook(verify_script, result.repo_root)
    _write_executable_file(pre_push_hook, rendered, result)


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

log() {{
  printf "%s %s\\n" "$(date -Iseconds)" "$1" >> "$LOG_FILE"
}}

log "repo-pre-push-started"

if [ -x "$PROJECT_HOOK" ]; then
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
