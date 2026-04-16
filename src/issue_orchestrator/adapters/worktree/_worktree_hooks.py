"""Git hook installation for worktrees."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from ._worktree_git import _git_run

logger = logging.getLogger(__name__)

# Path to bundled hooks (in issue_orchestrator/hooks/, 3 levels up from this module)
HOOKS_DIR = Path(__file__).parent.parent.parent / "hooks"


def install_hooks(worktree_path: Path, pre_push_hook: Path | None = None) -> None:
    """
    Install git hooks into a worktree.

    This function CHAINS hooks - if the project already has a pre-push hook,
    we preserve it and run it BEFORE the orchestrator's hook.

    Args:
        worktree_path: Path to the worktree
        pre_push_hook: Custom pre-push hook path (uses bundled if None)

    Note:
        Worktrees have a .git file (not directory) that points to the main repo.
        We need to find the actual hooks directory.

        If the project uses core.hooksPath (e.g., .githooks/), we override it
        for this worktree and copy the project hooks to the worktree's hooks dir.
    """
    worktree_path = Path(worktree_path)

    git_file = worktree_path / ".git"
    if not git_file.exists():
        return

    content = git_file.read_text().strip()
    if not content.startswith("gitdir:"):
        return

    gitdir = Path(content.split(":", 1)[1].strip())
    hooks_dir = gitdir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Read from main repo config, not worktree config. Worktree config may have
    # our override from a previous install.
    main_repo_root = gitdir.parent.parent
    hooks_path_result = _git_run(
        main_repo_root,
        ["config", "--local", "--get", "core.hooksPath"],
        check=False,
    )
    custom_hooks_path = hooks_path_result.stdout.strip() if hooks_path_result.returncode == 0 else None

    # Always set per-worktree hooksPath so hooks live with the worktree.
    # Explicit GIT_DIR prevents symlink resolution from writing to another worktree config.
    git_env = {
        **os.environ,
        "GIT_DIR": str(gitdir),
        "GIT_WORK_TREE": str(worktree_path),
    }
    _git_run(
        worktree_path,
        ["config", "extensions.worktreeConfig", "true"],
        check=False,
        env=git_env,
    )
    _git_run(
        worktree_path,
        ["config", "--worktree", "core.hooksPath", str(hooks_dir)],
        check=False,
        env=git_env,
    )
    _git_run(
        worktree_path,
        ["config", "--worktree", "core.worktree", str(worktree_path)],
        check=False,
        env=git_env,
    )
    _git_run(
        worktree_path,
        ["config", "--worktree", "core.bare", "false"],
        check=False,
        env=git_env,
    )
    logger.info("Overriding core.hooksPath to %s for this worktree only (gitdir=%s)", hooks_dir, gitdir)

    project_hook = _resolve_project_pre_push_hook(gitdir, custom_hooks_path)
    dst_hook = hooks_dir / "pre-push"
    orchestrator_hook = pre_push_hook if pre_push_hook else HOOKS_DIR / "pre-push"

    if project_hook.exists() and project_hook.is_file():
        _install_chained_hook(hooks_dir, dst_hook, project_hook, orchestrator_hook)
    elif orchestrator_hook.exists():
        shutil.copy2(orchestrator_hook, dst_hook)
        dst_hook.chmod(0o755)
        logger.info("Installed orchestrator pre-push hook")


def _resolve_project_pre_push_hook(gitdir: Path, custom_hooks_path: str | None) -> Path:
    main_repo_root = gitdir.parent.parent
    if custom_hooks_path:
        return main_repo_root.parent / custom_hooks_path / "pre-push"
    return main_repo_root / "hooks" / "pre-push"


def _install_chained_hook(
    hooks_dir: Path,
    dst_hook: Path,
    project_hook: Path,
    orchestrator_hook: Path,
) -> None:
    project_hook_copy = hooks_dir / "pre-push.project"
    shutil.copy2(project_hook, project_hook_copy)
    project_hook_copy.chmod(0o755)

    wrapper_content = _chained_hook_script()
    dst_hook.write_text(wrapper_content)
    dst_hook.chmod(0o755)

    if orchestrator_hook.exists():
        orch_hook_copy = hooks_dir / "pre-push.orchestrator"
        shutil.copy2(orchestrator_hook, orch_hook_copy)
        orch_hook_copy.chmod(0o755)

    logger.info("Installed chained pre-push hooks (project + orchestrator)")


def _chained_hook_script() -> str:
    return """#!/bin/bash
# Chained pre-push hook: runs project hook first, then orchestrator hook
set -e

HOOKS_DIR="$(dirname "$0")"
AUDIT_LOG="$HOOKS_DIR/pre-push.log"

# Audit logging function
audit() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$AUDIT_LOG"
    echo "[orchestrator] $1"
}

total_start=$(date +%s)
audit "Pre-push hook started (commit: $(git rev-parse --short HEAD))"

# Run project's pre-push hook first (lint, tests, etc.)
# Skip if ORCHESTRATOR_SKIP_PROJECT_HOOK=1 (used by e2e tests)
if [ "${ORCHESTRATOR_SKIP_PROJECT_HOOK:-0}" = "1" ]; then
    audit "Skipping project hook (ORCHESTRATOR_SKIP_PROJECT_HOOK=1)"
elif [ -x "$HOOKS_DIR/pre-push.project" ]; then
    audit "Running project pre-push hook..."
    project_start=$(date +%s)
    if "$HOOKS_DIR/pre-push.project" "$@"; then
        project_end=$(date +%s)
        project_duration=$((project_end - project_start))
        audit "Project hook PASSED (duration=${project_duration}s)"
    else
        project_exit=$?
        project_end=$(date +%s)
        project_duration=$((project_end - project_start))
        audit "Project hook FAILED (exit ${project_exit} duration=${project_duration}s)"
        exit 1
    fi
else
    audit "No project hook found"
fi

# Then run orchestrator's trailer validation
if [ -x "$HOOKS_DIR/pre-push.orchestrator" ]; then
    audit "Running orchestrator pre-push hook..."
    orch_start=$(date +%s)
    if "$HOOKS_DIR/pre-push.orchestrator" "$@"; then
        orch_end=$(date +%s)
        orch_duration=$((orch_end - orch_start))
        audit "Orchestrator hook PASSED (duration=${orch_duration}s)"
    else
        orch_exit=$?
        orch_end=$(date +%s)
        orch_duration=$((orch_end - orch_start))
        audit "Orchestrator hook FAILED (exit ${orch_exit} duration=${orch_duration}s)"
        exit 1
    fi
else
    audit "No orchestrator hook found"
fi

total_end=$(date +%s)
total_duration=$((total_end - total_start))
audit "Pre-push hook completed successfully (total_duration=${total_duration}s)"
"""
