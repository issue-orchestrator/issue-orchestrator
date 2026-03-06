"""Git worktree management module."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from ...infra.logging_config import issue_log
from ...ports.git import GitResult
from ...ports.worktree_policy import WorktreePolicy
from ...ports.worktree_manager import WorktreeReuseOptions
from ...infra.worktree_base import resolve_base_branch
from ..git.git_cli import GitCLI, SubprocessCommandRunner

# Marker file name for worktree identity (must match job_store.py)
WORKTREE_ID_MARKER = ".issue-orchestrator/worktree-id"


@dataclass
class ResetInfo:
    """Information about work discarded during worktree reset.

    This is returned by _update_worktree_onto_main and propagated
    to WorktreeInfo for event emission.
    """
    success: bool
    uncommitted_discarded: int = 0  # Count of uncommitted changes discarded
    commits_discarded: int = 0  # Count of commits discarded (on rebase failure)
    reason: str | None = None


@dataclass
class _WorktreeReuseResult:
    """Result of attempting to reuse an existing worktree."""
    success: bool  # True if worktree was successfully reused
    worktree_path: Path | None = None
    branch_name: str | None = None
    reset_info: ResetInfo | None = None
    recreated_reason: str | None = None  # Reason if worktree was deleted/recreated


logger = logging.getLogger(__name__)

_git = GitCLI(runner=SubprocessCommandRunner())

# Git writes index.lock during operations; treat short-lived locks as in-flight.
STALE_GIT_LOCK_SECONDS = 5
STALE_GIT_LOCK_RECHECK_SECONDS = 2
_BRANCH_IN_USE_BY_WORKTREE_RE = re.compile(
    r"fatal:\s+'([^']+)'\s+is already used by worktree at '([^']+)'"
)


def _git_run(
    repo: Path,
    argv: list[str],
    *,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> GitResult:
    return _git.run(repo=repo, argv=argv, check=check, env=env)


def _git_env_no_prompt() -> dict[str, str]:
    env = _git.clean_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _ensure_origin_branch(repo_root: Path, branch: str) -> None:
    fetch_result = _git_run(
        repo_root,
        ["fetch", "origin", branch],
        check=False,
        env=_git_env_no_prompt(),
    )
    if fetch_result.returncode != 0:
        raise WorktreeError(
            f"Failed to fetch origin/{branch}: {fetch_result.stderr.strip()}"
        )
    ref_result = _git_run(
        repo_root,
        ["rev-parse", "--verify", f"origin/{branch}"],
        check=False,
    )
    if ref_result.returncode != 0:
        raise WorktreeError(f"origin/{branch} does not exist after fetch")


# Path to bundled hooks (in issue_orchestrator/hooks/, 3 levels up from this module)
HOOKS_DIR = Path(__file__).parent.parent.parent / "hooks"

# Claude Code settings to enforce completion command usage on exit
# The Stop hook checks for a marker file that coding-done/reviewer-done creates
CLAUDE_SETTINGS_FOR_AGENTS = {
    "hooks": {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "test -f .agent-done-marker || echo '⚠️  WARNING: Session ending without completion command! Run: coding-done completed/blocked/needs_human'",
                        "timeout": 5
                    }
                ]
            }
        ]
    }
}

ALLOW_NO_VERIFY_DRY_RUN_PATH = Path(".issue-orchestrator") / "allow-no-verify-dry-run"


def _configure_no_verify_dry_run(worktree_path: Path, allow: bool) -> None:
    flag_path = worktree_path / ALLOW_NO_VERIFY_DRY_RUN_PATH
    try:
        if allow:
            flag_path.parent.mkdir(parents=True, exist_ok=True)
            flag_path.write_text("allow\n")
        elif flag_path.exists():
            flag_path.unlink()
    except OSError:
        logger.debug("Failed to update dry-run allow flag: %s", flag_path)


class WorktreeError(Exception):
    """Raised when a worktree operation fails."""

    pass


def get_default_branch(repo_root: Path) -> str:
    """
    Get the repository's default branch name (main, master, etc.).

    Attempts to detect from remote HEAD, falls back to 'main'.

    Args:
        repo_root: Path to the repository root

    Returns:
        The default branch name (e.g., 'main', 'master')
    """
    branch = _git.default_branch(repo_root)
    logger.debug("Detected default branch: %s", branch)
    return branch


def _resolve_base_branch(repo_root: Path, base_branch_override: str | None) -> str:
    """Resolve the base branch used for worktree creation/reset."""
    resolved = resolve_base_branch(
        repo_root,
        config_override=base_branch_override,
        default_branch_resolver=_git.default_branch,
        log=logger,
    )
    return resolved.branch


def _update_worktree_onto_main(
    worktree_path: Path,
    repo_root: Path,
    base_branch: str | None = None,
) -> ResetInfo:
    """
    Update a worktree's branch onto the latest base branch.

    This is crucial for reruns - branches created from old base need to be
    rebased onto current base to get the latest code changes.

    Strategy: Success > preserving agent work. If rebase fails for any reason
    (conflicts, stale branch, etc.), discard all local work and reset to main.
    This ensures agents always work with the latest code.

    The sequence:
    1. Fetch origin to get latest base branch
    2. Discard any uncommitted changes
    3. Try to rebase current branch onto origin/<base>
    4. If rebase fails, hard reset to origin/<base> (discards all local commits)

    Args:
        worktree_path: Path to the worktree to update
        repo_root: Path to the main repository
        base_branch: Branch name to rebase onto (e.g., main, master)

    Returns:
        ResetInfo with success status and counts of discarded work
    """
    uncommitted_discarded = 0
    commits_discarded = 0
    try:
        base = _resolve_base_branch(repo_root, base_branch)

        # Step 1: Fetch origin to get latest base branch
        fetch_result = _git_run(
            worktree_path,
            ["fetch", "origin", base],
            check=False,
            env=_git_env_no_prompt(),
        )
        if fetch_result.returncode != 0:
            return ResetInfo(
                success=False,
                reason=f"fetch_failed: {fetch_result.stderr.strip()}",
            )

        ref_result = _git_run(
            worktree_path,
            ["rev-parse", "--verify", f"origin/{base}"],
            check=False,
        )
        if ref_result.returncode != 0:
            return ResetInfo(
                success=False,
                reason=f"origin_ref_missing: origin/{base}",
            )

        # Step 2: Get current branch
        branch_result = _git_run(
            worktree_path,
            ["rev-parse", "--abbrev-ref", "HEAD"],
            check=False,
        )
        if branch_result.returncode != 0:
            logger.warning("Could not determine current branch in %s", worktree_path)
            return ResetInfo(success=False, reason="branch_unknown")

        current_branch = branch_result.stdout.strip()
        if current_branch == base:
            # Already on base branch, just pull
            _git_run(
                worktree_path,
                ["pull", "--ff-only"],
                check=False,
            )
            return ResetInfo(success=True)

        # Step 3: Discard any uncommitted changes
        # We prioritize success over preserving uncommitted work
        status_result = _git_run(
            worktree_path,
            ["status", "--porcelain"],
            check=False,
        )
        if status_result.returncode == 0 and status_result.stdout.strip():
            uncommitted_discarded = len(status_result.stdout.strip().split("\n"))
            logger.warning(
                "[WORKTREE_RESET] Discarding %d uncommitted changes in %s (branch: %s)",
                uncommitted_discarded,
                worktree_path,
                current_branch,
            )
        _git_run(
            worktree_path,
            ["reset", "--hard", "HEAD"],
            check=False,
        )
        _git_run(
            worktree_path,
            ["clean", "-fd"],
            check=False,
        )

        # Step 4: Try to rebase onto origin/<base>
        logger.info(
            "Rebasing branch %s onto origin/%s in %s",
            current_branch,
            base,
            worktree_path,
        )
        rebase_result = _git_run(
            worktree_path,
            ["rebase", f"origin/{base}"],
            check=False,
        )

        if rebase_result.returncode != 0:
            # Rebase failed - discard everything and reset to main
            # Abort the rebase first
            _git_run(
                worktree_path,
                ["rebase", "--abort"],
                check=False,
            )

            # Count how many commits we're discarding
            commits_result = _git_run(
                worktree_path,
                ["rev-list", "--count", f"origin/{base}..HEAD"],
                check=False,
            )
            if commits_result.returncode == 0 and commits_result.stdout.strip():
                try:
                    commits_discarded = int(commits_result.stdout.strip())
                except ValueError:
                    pass

            logger.warning(
                "[WORKTREE_RESET] Rebase failed, discarding %d commits and resetting to %s "
                "(branch: %s, path: %s, error: %s)",
                commits_discarded,
                base,
                current_branch,
                worktree_path,
                rebase_result.stderr.strip(),
            )

            # Hard reset to origin/<base> - discards all local commits
            _git_run(
                worktree_path,
                ["reset", "--hard", f"origin/{base}"],
                check=False,
            )
            return ResetInfo(
                success=True,
                uncommitted_discarded=uncommitted_discarded,
                commits_discarded=commits_discarded,
            )

        logger.info(
            "Successfully rebased branch %s onto origin/%s in %s",
            current_branch,
            base,
            worktree_path,
        )
        return ResetInfo(success=True, uncommitted_discarded=uncommitted_discarded)

    except Exception as e:
        logger.exception("Error updating worktree onto main: %s", e)
        return ResetInfo(success=False, reason=str(e))


def _push_dry_run_preflight(
    worktree_path: Path,
    branch_name: str,
    *,
    allow_no_verify: bool,
) -> tuple[bool, str]:
    """Check if a dry-run push would succeed for a reused worktree."""
    import time

    cmd = ["push", "--dry-run", "--force-with-lease"]
    if allow_no_verify:
        cmd.append("--no-verify")
    cmd += ["-u", "origin", branch_name]
    last_error = ""
    for attempt in range(1, 4):
        result = _git_run(worktree_path, cmd, check=False)
        if result.returncode == 0:
            return True, ""
        stderr = (result.stderr or "").strip()
        last_error = stderr
        if attempt < 3:
            time.sleep(0.5 * (2 ** (attempt - 1)))
    return False, f"push dry-run failed: {last_error}"


def sync_cli_tools(worktree_path: Path) -> None:
    """
    Sync CLI tools from the orchestrator package to worktree.

    This ensures the worktree has the latest orchestrator tools (especially
    coding-done/reviewer-done) regardless of when the worktree was created or what branch
    it's on.

    Uses package-relative paths so this works even when the target repo is
    a foreign (non-orchestrator) repository.

    Args:
        worktree_path: Path to the worktree
    """
    # Find cli_tools from the orchestrator's own package, not from repo_root.
    # This ensures tools are found even when targeting a foreign repo.
    # __file__ = .../adapters/worktree/_worktree.py
    # parents[2] = .../issue_orchestrator/
    src_cli_tools = Path(__file__).resolve().parents[2] / "entrypoints" / "cli_tools"
    dst_cli_tools = worktree_path / "src" / "issue_orchestrator" / "entrypoints" / "cli_tools"

    if not src_cli_tools.exists():
        logger.debug("No cli_tools in orchestrator package at %s, skipping sync", src_cli_tools)
        return

    # Create destination directory tree if it doesn't exist (foreign repos
    # won't have src/issue_orchestrator/).
    dst_cli_tools.mkdir(parents=True, exist_ok=True)

    # Copy each .py file from source to destination
    for src_file in src_cli_tools.glob("*.py"):
        dst_file = dst_cli_tools / src_file.name
        try:
            shutil.copy2(src_file, dst_file)
            logger.debug("Synced cli tool: %s -> %s", src_file.name, dst_file)
        except OSError as e:
            logger.warning("Failed to sync cli tool %s: %s", src_file.name, e)

    logger.info("Synced cli_tools from orchestrator package to worktree")


def _install_worktree_identity(worktree_path: Path) -> str:
    """
    Install a unique identity marker in the worktree.

    This identity is used to detect path reuse - if a worktree is deleted
    and recreated at the same path, it gets a new identity. Jobs store
    the worktree_id and can detect when their worktree has been replaced.

    The identity is only created once - subsequent calls are idempotent.

    Args:
        worktree_path: Path to the worktree

    Returns:
        The worktree identity (existing or newly created)
    """
    marker_path = worktree_path / WORKTREE_ID_MARKER

    # Check for existing identity
    if marker_path.exists():
        try:
            existing_id = marker_path.read_text().strip()
            if existing_id:
                logger.debug("Worktree identity exists: %s", existing_id)
                return existing_id
        except Exception:
            pass

    # Generate new identity
    worktree_id = f"wt-{uuid.uuid4().hex[:12]}"
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(worktree_id)
        logger.info("Installed worktree identity: %s", worktree_id)
    except Exception as e:
        logger.warning("Failed to install worktree identity: %s", e)
        # Return the ID anyway - identity is best-effort

    return worktree_id


def install_claude_settings(worktree_path: Path) -> None:
    """
    Install Claude Code settings to enforce completion command usage on exit.

    Creates .claude/settings.json in the worktree with a Stop hook
    that checks if a completion command was called before allowing exit.

    Args:
        worktree_path: Path to the worktree
    """
    worktree_path = Path(worktree_path)
    claude_dir = worktree_path / ".claude"
    settings_file = claude_dir / "settings.json"

    # Create .claude directory if it doesn't exist
    claude_dir.mkdir(parents=True, exist_ok=True)

    # If settings.json already exists, merge our hooks with existing
    if settings_file.exists():
        try:
            existing = json.loads(settings_file.read_text())
            # Merge hooks - add our Stop hook
            if "hooks" not in existing:
                existing["hooks"] = {}
            if "Stop" not in existing["hooks"]:
                existing["hooks"]["Stop"] = []
            # Add our hook if not already present
            our_hook = CLAUDE_SETTINGS_FOR_AGENTS["hooks"]["Stop"][0]
            if our_hook not in existing["hooks"]["Stop"]:
                existing["hooks"]["Stop"].append(our_hook)
            settings_file.write_text(json.dumps(existing, indent=2))
        except (json.JSONDecodeError, KeyError):
            # If existing file is invalid, overwrite
            settings_file.write_text(json.dumps(CLAUDE_SETTINGS_FOR_AGENTS, indent=2))
    else:
        settings_file.write_text(json.dumps(CLAUDE_SETTINGS_FOR_AGENTS, indent=2))

    logger.debug("Installed Claude settings at %s", settings_file)


def slugify(text: str, max_length: int = 40) -> str:
    """
    Convert text to a branch-friendly slug.

    Args:
        text: Text to slugify
        max_length: Maximum length of the resulting slug

    Returns:
        Slugified text suitable for use in branch names
    """
    # Lowercase and replace non-alphanumeric with hyphens
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower())
    # Remove leading/trailing hyphens
    slug = slug.strip('-')
    # Truncate and remove trailing hyphens that may result from truncation
    return slug[:max_length].rstrip('-')


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

    # Read .git file to find the gitdir
    git_file = worktree_path / ".git"
    if not git_file.exists():
        return  # Not a worktree

    # .git file contains: gitdir: /path/to/main/repo/.git/worktrees/name
    content = git_file.read_text().strip()
    if not content.startswith("gitdir:"):
        return

    gitdir = Path(content.split(":", 1)[1].strip())
    hooks_dir = gitdir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Check if project uses core.hooksPath (common pattern for version-controlled hooks)
    # IMPORTANT: Read from main repo config, not worktree config
    # (worktree config may have our override from a previous install)
    main_repo_root = gitdir.parent.parent  # /repo/.git from /repo/.git/worktrees/name
    hooks_path_result = _git_run(
        main_repo_root,
        ["config", "--local", "--get", "core.hooksPath"],
        check=False,
    )
    custom_hooks_path = hooks_path_result.stdout.strip() if hooks_path_result.returncode == 0 else None

    # Always set per-worktree hooksPath so hooks live with the worktree.
    # IMPORTANT: Use explicit GIT_DIR to prevent config leaking to wrong worktree.
    # Without this, symlink resolution issues (/tmp vs /private/tmp) can cause
    # git to write config to the wrong worktree's config file.
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

    # Find the project's pre-push hook
    project_hook = None
    if custom_hooks_path:
        # Project uses custom hooksPath (e.g., .githooks/)
        # Resolve relative to MAIN repo root (not worktree)
        project_hook = main_repo_root.parent / custom_hooks_path / "pre-push"
    else:
        # Standard hooks location in main repo
        # The gitdir is like /repo/.git/worktrees/name, so main repo is gitdir.parent.parent
        main_repo_hooks = gitdir.parent.parent / "hooks"
        project_hook = main_repo_hooks / "pre-push"

    dst_hook = hooks_dir / "pre-push"
    orchestrator_hook = pre_push_hook if pre_push_hook else HOOKS_DIR / "pre-push"

    if project_hook.exists() and project_hook.is_file():
        # Chain hooks: copy project hook, then create wrapper that runs both
        project_hook_copy = hooks_dir / "pre-push.project"
        shutil.copy2(project_hook, project_hook_copy)
        project_hook_copy.chmod(0o755)

        # Create wrapper that runs project hook first, then orchestrator hook
        wrapper_content = f"""#!/bin/bash
# Chained pre-push hook: runs project hook first, then orchestrator hook
set -e

HOOKS_DIR="$(dirname "$0")"
AUDIT_LOG="$HOOKS_DIR/pre-push.log"

# Audit logging function
audit() {{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$AUDIT_LOG"
    echo "[orchestrator] $1"
}}

total_start=$(date +%s)
audit "Pre-push hook started (commit: $(git rev-parse --short HEAD))"

# Run project's pre-push hook first (lint, tests, etc.)
# Skip if ORCHESTRATOR_SKIP_PROJECT_HOOK=1 (used by e2e tests)
if [ "${{ORCHESTRATOR_SKIP_PROJECT_HOOK:-0}}" = "1" ]; then
    audit "Skipping project hook (ORCHESTRATOR_SKIP_PROJECT_HOOK=1)"
elif [ -x "$HOOKS_DIR/pre-push.project" ]; then
    audit "Running project pre-push hook..."
    project_start=$(date +%s)
    if "$HOOKS_DIR/pre-push.project" "$@"; then
        project_end=$(date +%s)
        project_duration=$((project_end - project_start))
        audit "Project hook PASSED (duration=${{project_duration}}s)"
    else
        project_exit=$?
        project_end=$(date +%s)
        project_duration=$((project_end - project_start))
        audit "Project hook FAILED (exit ${{project_exit}} duration=${{project_duration}}s)"
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
        audit "Orchestrator hook PASSED (duration=${{orch_duration}}s)"
    else
        orch_exit=$?
        orch_end=$(date +%s)
        orch_duration=$((orch_end - orch_start))
        audit "Orchestrator hook FAILED (exit ${{orch_exit}} duration=${{orch_duration}}s)"
        exit 1
    fi
else
    audit "No orchestrator hook found"
fi

total_end=$(date +%s)
total_duration=$((total_end - total_start))
audit "Pre-push hook completed successfully (total_duration=${{total_duration}}s)"
"""
        dst_hook.write_text(wrapper_content)
        dst_hook.chmod(0o755)

        # Copy orchestrator hook as pre-push.orchestrator
        if orchestrator_hook.exists():
            orch_hook_copy = hooks_dir / "pre-push.orchestrator"
            shutil.copy2(orchestrator_hook, orch_hook_copy)
            orch_hook_copy.chmod(0o755)

        logger.info("Installed chained pre-push hooks (project + orchestrator)")
    elif orchestrator_hook.exists():
        # No project hook, just install orchestrator's hook directly
        shutil.copy2(orchestrator_hook, dst_hook)
        dst_hook.chmod(0o755)
        logger.info("Installed orchestrator pre-push hook")


def generate_branch_name(issue_number: int, issue_title: str) -> str:
    """
    Generate a branch name from issue number and title.

    Args:
        issue_number: GitHub issue number
        issue_title: GitHub issue title

    Returns:
        Branch name in format: {number}-{slugified-title}
        Example: 123-add-user-authentication
    """
    slug = slugify(issue_title, max_length=50)
    return f"{issue_number}-{slug}"


# Regex pattern for extracting issue number from branch name
# Branch format: {issue_number}-{title-slug} (e.g., "328-add-feature")
BRANCH_ISSUE_PATTERN = re.compile(r'^(\d+)-')


def extract_issue_number_from_branch(branch_name: str) -> int | None:
    """
    Extract issue number from a branch name.

    This is the inverse of generate_branch_name(). All code that needs to
    extract issue numbers from branch names should use this function.

    Args:
        branch_name: Branch name (e.g., "328-add-feature")

    Returns:
        Issue number if found, None otherwise
    """
    match = BRANCH_ISSUE_PATTERN.match(branch_name)
    if match:
        return int(match.group(1))
    return None


def _branch_matches_issue(branch_name: str, issue_number: int) -> bool:
    extracted = extract_issue_number_from_branch(branch_name)
    return extracted == issue_number


def _list_branch_names(repo_root: Path) -> list[str]:
    result = _git_run(
        repo_root,
        ["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin"],
        check=False,
    )
    if result.returncode != 0:
        return []
    names: list[str] = []
    for line in (result.stdout or "").splitlines():
        name = line.strip()
        if not name:
            continue
        if name.startswith("origin/"):
            name = name[len("origin/"):]
        if name == "HEAD":
            continue
        names.append(name)
    return names


def next_branch_name(repo_root: Path, branch_name: str) -> str:
    base = re.sub(r"-r\d+$", "", branch_name)
    existing = _list_branch_names(repo_root)
    pattern = re.compile(rf"^{re.escape(base)}-r(\d+)$")
    max_suffix = 0
    for name in existing:
        match = pattern.match(name)
        if match:
            try:
                max_suffix = max(max_suffix, int(match.group(1)))
            except ValueError:
                continue
    return f"{base}-r{max_suffix + 1}"


def _delete_remote_branch(repo_root: Path, branch_name: str) -> bool:
    result = _git_run(
        repo_root,
        # Bypass local hooks (completion commands) for branch deletion only.
        ["push", "--no-verify", "origin", "--delete", branch_name],
        check=False,
        env=_git_env_no_prompt(),
    )
    if result.returncode == 0:
        return True
    stderr = (result.stderr or "").lower()
    if "remote ref does not exist" in stderr or "remote ref not found" in stderr:
        return True
    return False


def find_worktree_for_branch(repo_root: Path, branch_name: str) -> Path | None:
    """
    Find an existing worktree that has the given branch checked out.

    Args:
        repo_root: Path to the main git repository
        branch_name: Branch name to search for

    Returns:
        Path to the worktree if found, None otherwise
    """
    logger.debug("Searching for worktree: repo=%s branch=%s", repo_root, branch_name)
    result = _git_run(
        repo_root,
        ["worktree", "list", "--porcelain"],
        check=False,
    )
    if result.returncode != 0:
        logger.debug("worktree list failed: repo=%s returncode=%s", repo_root, result.returncode)
        return None

    # Parse porcelain output:
    # worktree /path/to/worktree
    # HEAD abc123
    # branch refs/heads/branch-name
    # (blank line)
    current_worktree = None
    for line in result.stdout.split("\n"):
        if line.startswith("worktree "):
            current_worktree = Path(line.split(" ", 1)[1])
        elif line.startswith("branch refs/heads/"):
            current_branch = line.split("refs/heads/", 1)[1]
            if current_branch == branch_name and current_worktree:
                logger.info("Found existing worktree for branch %s: %s", branch_name, current_worktree)
                return current_worktree

    return None


def _get_worktree_git_env(worktree_path: Path) -> dict[str, str] | None:
    """Get environment variables for worktree git operations.

    Returns:
        Environment dict with GIT_DIR and GIT_WORK_TREE set, or None if not a worktree.
    """
    git_file = worktree_path / ".git"
    if not git_file.exists():
        return None
    content = git_file.read_text().strip()
    if not content.startswith("gitdir:"):
        return None
    git_dir = content.split(":", 1)[1].strip()
    env = os.environ.copy()
    env["GIT_DIR"] = git_dir
    env["GIT_WORK_TREE"] = str(worktree_path)
    return env


def _handle_stale_lock_and_retry(
    worktree_path: Path,
    cmd: list[str],
    env: dict[str, str] | None,
    result: GitResult,
) -> GitResult | None:
    """Handle stale git lock file and retry operation.

    Returns:
        Successful GitResult if retry worked, None otherwise.
    """
    lock_match = re.search(r"Unable to create '([^']+index\.lock)'", result.stderr or "")
    if not lock_match:
        return None

    lock_path = Path(lock_match.group(1))
    if not lock_path.exists():
        return None

    age_seconds = time.time() - lock_path.stat().st_mtime
    if age_seconds < STALE_GIT_LOCK_SECONDS:
        time.sleep(STALE_GIT_LOCK_RECHECK_SECONDS)
        if lock_path.exists():
            age_seconds = time.time() - lock_path.stat().st_mtime

    if age_seconds <= STALE_GIT_LOCK_SECONDS:
        return None

    logger.warning(
        "Removing stale git lock before detach: path=%s age=%.1fs",
        lock_path,
        age_seconds,
    )
    try:
        lock_path.unlink()
    except OSError:
        return None

    retry = _git_run(worktree_path, cmd, check=False, env=env)
    if retry.returncode == 0:
        return retry
    return None


def _detach_worktree_branch(worktree_path: Path, branch_name: str) -> None:
    logger.info(
        "Detaching worktree branch to free branch: path=%s branch=%s",
        worktree_path,
        branch_name,
    )
    cmd = ["checkout", "--detach"]
    env = _get_worktree_git_env(worktree_path)
    if env:
        logger.info("Detaching with explicit GIT_DIR for worktree: %s", worktree_path)

    result = _git_run(worktree_path, cmd, check=False, env=env)
    if result.returncode == 0:
        return

    # Try to handle stale lock
    retry_result = _handle_stale_lock_and_retry(worktree_path, cmd, env, result)
    if retry_result is not None:
        return

    raise WorktreeError(
        "Failed to detach worktree branch: "
        f"path={worktree_path} branch={branch_name} stderr={result.stderr}"
    )


def _resolve_repo_root_from_worktree(worktree_path: Path) -> Path | None:
    worktree_path = Path(worktree_path)
    git_entry = worktree_path / ".git"
    if not git_entry.exists():
        return None
    if git_entry.is_dir():
        return worktree_path
    try:
        content = git_entry.read_text().strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    git_dir = Path(content.split(":", 1)[1].strip()).resolve()
    if git_dir.name == ".git":
        return git_dir.parent
    if git_dir.parent.name == "worktrees":
        return git_dir.parent.parent.parent
    return git_dir.parent


def _remove_existing_worktree_path(repo_root: Path, worktree_path: Path) -> None:
    logger.info("Removing existing worktree path for fresh create: %s", worktree_path)
    result = _git_run(
        repo_root,
        ["worktree", "remove", "--force", str(worktree_path)],
        check=False,
    )
    if result.returncode != 0:
        logger.warning(
            "Failed to remove worktree via git, deleting directory: path=%s stderr=%s",
            worktree_path,
            result.stderr.strip(),
        )
        shutil.rmtree(worktree_path, ignore_errors=True)


def _commit_setup_artifacts(worktree_path: Path) -> None:
    """Commit any dirty files left by worktree setup (hooks, cli_tools sync, settings).

    sync_cli_tools copies the orchestrator's current cli_tools source into the
    worktree.  When the orchestrator is running from a different version than
    the worktree's checkout (common during development), those copied files
    appear as modifications in ``git status``.  Committing them here keeps the
    worktree clean so that ``coding-done``'s dirty-tree check only detects
    files the agent actually changed.

    Best-effort: failures are logged but do not block worktree creation.
    """
    try:
        result = _git_run(worktree_path, ["status", "--porcelain"], check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return  # Nothing dirty or git failed — nothing to do

        _git_run(worktree_path, ["add", "-A"], check=False)
        _git_run(
            worktree_path,
            ["commit", "-m", "chore: worktree setup artifacts", "--no-verify"],
            check=False,
        )
        logger.debug("Committed setup artifacts in worktree %s", worktree_path)
    except Exception:
        logger.debug("Could not commit setup artifacts in %s (non-fatal)", worktree_path)


def _finalize_worktree(
    worktree_path: Path,
    repo_root: Path,
    enforce_hooks: bool,
    pre_push_hook: Path | None,
    allow_no_verify_dry_run_preflight: bool,
) -> None:
    """Install hooks, settings, cli_tools, and identity marker on a worktree."""
    if enforce_hooks:
        install_hooks(worktree_path, pre_push_hook)
    install_claude_settings(worktree_path)
    _configure_no_verify_dry_run(worktree_path, allow_no_verify_dry_run_preflight)
    sync_cli_tools(worktree_path)
    _install_worktree_identity(worktree_path)
    _commit_setup_artifacts(worktree_path)


def _try_reuse_worktree(
    worktree_path: Path,
    branch_name: str,
    repo_root: Path,
    issue_number: int,
    policy: WorktreePolicy,
    reuse_push_preflight: bool,
    allow_no_verify_dry_run_preflight: bool,
    base_branch: str | None,
) -> _WorktreeReuseResult:
    """Try to reuse an existing worktree, validating and preparing it.

    Returns:
        _WorktreeReuseResult indicating success/failure with details.
    """
    # Policy: validate worktree can be reused
    validation = policy.validate_for_reuse(worktree_path, branch_name, repo_root)
    if not validation.can_reuse:
        logger.warning(
            issue_log(issue_number, "Worktree failed validation, deleting: %s"),
            validation.reason,
        )
        policy.delete_worktree(worktree_path, repo_root)
        return _WorktreeReuseResult(
            success=False,
            recreated_reason=f"validation_failed: {validation.reason}",
        )

    # Rebase onto latest base branch (critical for reruns with stale branches)
    reset_info = _update_worktree_onto_main(worktree_path, repo_root, base_branch)

    # Policy: sync remote refs to prevent stale-info push failures
    sync_result = policy.sync_remote_refs(worktree_path, branch_name)
    if not sync_result.success:
        logger.warning(
            issue_log(issue_number, "Failed to sync remote refs, deleting worktree: %s"),
            sync_result.reason,
        )
        policy.delete_worktree(worktree_path, repo_root)
        return _WorktreeReuseResult(
            success=False,
            recreated_reason=f"sync_failed: {sync_result.reason}",
        )

    if not reset_info.success:
        logger.warning(
            issue_log(issue_number, "Reset to base branch failed, deleting worktree: %s"),
            reset_info.reason or "unknown",
        )
        policy.delete_worktree(worktree_path, repo_root)
        return _WorktreeReuseResult(
            success=False,
            recreated_reason=f"reset_failed: {reset_info.reason or 'rebase failed'}",
        )

    # Optional push preflight check
    if reuse_push_preflight:
        ok, reason = _push_dry_run_preflight(
            worktree_path,
            branch_name,
            allow_no_verify=allow_no_verify_dry_run_preflight,
        )
        if not ok:
            logger.warning(
                issue_log(issue_number, "Push preflight failed, deleting worktree: %s"),
                reason,
            )
            policy.delete_worktree(worktree_path, repo_root)
            return _WorktreeReuseResult(
                success=False,
                recreated_reason=f"push_preflight_failed: {reason}",
            )
        logger.info(issue_log(issue_number, "Push preflight ok for reused worktree"))

    return _WorktreeReuseResult(
        success=True,
        worktree_path=worktree_path,
        branch_name=branch_name,
        reset_info=reset_info,
    )


def _handle_branch_on_recreate(
    repo_root: Path,
    branch_name: str,
    issue_number: int,
    reuse_options: WorktreeReuseOptions,
) -> str:
    """Handle branch options when recreating a worktree.

    Returns:
        Possibly modified branch name.
    """
    worktree_branch_on_recreate = reuse_options.worktree_branch_on_recreate
    if worktree_branch_on_recreate == "delete":
        if not reuse_options.allow_remote_branch_delete:
            logger.info(
                issue_log(issue_number, "Skipping remote branch delete before recreate: %s"),
                branch_name,
            )
        elif _delete_remote_branch(repo_root, branch_name):
            logger.info(issue_log(issue_number, "Deleted remote branch before recreate: %s"), branch_name)
        else:
            logger.warning(issue_log(issue_number, "Failed to delete remote branch before recreate: %s"), branch_name)
    elif worktree_branch_on_recreate == "create_new_branch":
        new_branch = next_branch_name(repo_root, branch_name)
        logger.info(
            issue_log(issue_number, "Creating new branch for recreated worktree: %s -> %s"),
            branch_name,
            new_branch,
        )
        return new_branch
    return branch_name


def _build_worktree_add_command(
    repo_root: Path,
    worktree_path: Path,
    branch_name: str,
    base_branch: str | None,
) -> list[str]:
    """Build the git worktree add command.

    Returns:
        The git command arguments list.
    """
    # Check if branch already exists
    branch_check = _git_run(
        repo_root,
        ["rev-parse", "--verify", branch_name],
        check=False,
    )
    branch_exists = branch_check.returncode == 0
    logger.debug(
        "Branch exists check: repo=%s branch=%s exists=%s",
        repo_root,
        branch_name,
        branch_exists,
    )

    if branch_exists:
        # Use existing branch
        return ["worktree", "add", str(worktree_path), branch_name]

    # Try to fetch remote branch (for review/rework sessions)
    fetch_result = _git_run(
        repo_root,
        ["fetch", "origin", branch_name],
        check=False,
    )
    if fetch_result.returncode == 0:
        return [
            "worktree", "add",
            str(worktree_path), "-b", branch_name, f"origin/{branch_name}"
        ]

    # Create new branch from default branch, NOT from HEAD
    # This ensures agent worktrees don't inherit commits from user's feature branch
    default_branch = _resolve_base_branch(repo_root, base_branch)
    logger.info("Creating new branch from default branch: %s", default_branch)
    _ensure_origin_branch(repo_root, default_branch)
    return [
        "worktree", "add",
        str(worktree_path), "-b", branch_name, f"origin/{default_branch}"
    ]


@dataclass
class _WorktreeCreateContext:
    """Context for worktree creation."""
    repo_root: Path
    worktree_path: Path
    branch_name: str
    base_branch: str | None
    issue_number: int
    policy: WorktreePolicy
    reuse_options: WorktreeReuseOptions
    enforce_hooks: bool
    pre_push_hook: Path | None
    disable_reuse: bool


def _init_worktree_context(
    repo_root: Path,
    issue_number: int,
    issue_title: str,
    worktree_base: Path | None,
    base_branch: str | None,
    branch_name: str | None,
    reuse_options: WorktreeReuseOptions | None,
    policy: WorktreePolicy | None,
    enforce_hooks: bool,
    pre_push_hook: Path | None,
) -> _WorktreeCreateContext:
    """Initialize context for worktree creation."""
    if policy is None:
        from .worktree_policy import default_policy
        policy = default_policy
    if reuse_options is None:
        reuse_options = WorktreeReuseOptions()

    repo_root = Path(repo_root).resolve()
    if not (repo_root / ".git").exists():
        raise WorktreeError(f"Not a git repository: {repo_root}")

    worktree_base = Path(worktree_base).resolve() if worktree_base else repo_root.parent
    worktree_base.mkdir(parents=True, exist_ok=True)
    branch_name = branch_name or generate_branch_name(issue_number, issue_title)
    worktree_path = worktree_base / f"{repo_root.name}-{issue_number}"
    disable_reuse = (
        os.environ.get("ORCHESTRATOR_DISABLE_WORKTREE_REUSE") == "1"
        or reuse_options.disable_reuse
    )
    base_branch = _resolve_base_branch(repo_root, base_branch)

    logger.info(
        issue_log(issue_number, "Create worktree requested: branch=%s base=%s"),
        branch_name,
        worktree_base,
    )

    return _WorktreeCreateContext(
        repo_root=repo_root,
        worktree_path=worktree_path,
        branch_name=branch_name,
        base_branch=base_branch,
        issue_number=issue_number,
        policy=policy,
        reuse_options=reuse_options,
        enforce_hooks=enforce_hooks,
        pre_push_hook=pre_push_hook,
        disable_reuse=disable_reuse,
    )


def create_worktree(
    repo_root: Path,
    issue_number: int,
    issue_title: str,
    worktree_base: Path | None = None,
    base_branch: str | None = None,
    enforce_hooks: bool = True,
    pre_push_hook: Path | None = None,
    branch_name: str | None = None,
    reuse_options: WorktreeReuseOptions | None = None,
    policy: WorktreePolicy | None = None,
) -> tuple[Path, str, str, str | None, bool, int, int]:
    """
    Create a new git worktree for the given issue.

    Uses a "validate or delete" policy: if an existing worktree cannot be
    prepared for a clean session, it is deleted and a fresh one is created.

    Args:
        repo_root: Path to the main git repository
        issue_number: GitHub issue number
        issue_title: GitHub issue title (used to generate branch name if not provided)
        worktree_base: Base directory for worktrees. Defaults to parent of repo_root.
        base_branch: Base branch override (e.g., "main" or "master")
        enforce_hooks: Whether to install pre-push hooks
        pre_push_hook: Custom pre-push hook path
        branch_name: Specific branch to use (for checking out existing branches like PR reviews)
        reuse_options: Options controlling reuse behavior
        policy: Worktree setup policy (defaults to ValidateOrDeletePolicy)

    Returns:
        Tuple of (worktree_path, branch_name, reuse_status, reuse_reason,
        rebase_failed, uncommitted_discarded, commits_discarded)
        where rebase_failed is True if rebase failed and work was discarded to reset to main.
        uncommitted_discarded: count of uncommitted changes that were discarded
        commits_discarded: count of commits that were discarded (on rebase failure)

    Raises:
        WorktreeError: If worktree creation fails
    """
    try:
        ctx = _init_worktree_context(
            repo_root, issue_number, issue_title, worktree_base, base_branch, branch_name,
            reuse_options, policy, enforce_hooks, pre_push_hook,
        )

        # Prune stale worktrees
        prune_result = _git_run(ctx.repo_root, ["worktree", "prune"], check=False)
        logger.debug("Worktree prune: returncode=%s", prune_result.returncode)

        reuse_result, recreated_reason = _attempt_reuse(ctx)
        if reuse_result is not None:
            return reuse_result

        # Handle branch on recreate
        final_branch = ctx.branch_name
        if recreated_reason and _branch_matches_issue(ctx.branch_name, ctx.issue_number):
            final_branch = _handle_branch_on_recreate(
                ctx.repo_root, ctx.branch_name, ctx.issue_number, ctx.reuse_options
            )

        return _create_fresh_worktree(
            ctx.repo_root, ctx.worktree_path, final_branch, ctx.base_branch, ctx.issue_number,
            ctx.enforce_hooks, ctx.pre_push_hook, ctx.reuse_options, recreated_reason,
        )
    except WorktreeError:
        raise
    except Exception as e:
        raise WorktreeError(f"Error creating worktree: {e}")


def _attempt_reuse(
    ctx: _WorktreeCreateContext,
) -> tuple[tuple[Path, str, str, str | None, bool, int, int] | None, str | None]:
    """Attempt to reuse an existing worktree, returning (result, recreated_reason)."""
    recreated_reason: str | None = None
    if ctx.disable_reuse:
        recreated_reason = _handle_reuse_disabled(
            ctx.repo_root, ctx.worktree_path, ctx.branch_name, ctx.issue_number
        )
        return None, recreated_reason

    reuse_result, reuse_recreated_reason = _try_reuse_by_branch(
        ctx.repo_root, ctx.branch_name, ctx.issue_number, ctx.policy,
        ctx.reuse_options, ctx.enforce_hooks, ctx.pre_push_hook, ctx.base_branch,
    )
    if reuse_result is not None:
        return reuse_result, None
    if reuse_recreated_reason:
        recreated_reason = reuse_recreated_reason

    if ctx.worktree_path.exists():
        reuse_result, reuse_recreated_reason = _try_reuse_by_path(
            ctx.worktree_path, ctx.repo_root, ctx.issue_number, ctx.policy,
            ctx.reuse_options, ctx.enforce_hooks, ctx.pre_push_hook, ctx.base_branch,
        )
        if reuse_result is not None:
            return reuse_result, None
        if reuse_recreated_reason and not recreated_reason:
            recreated_reason = reuse_recreated_reason

    return None, recreated_reason


def _handle_reuse_disabled(
    repo_root: Path,
    worktree_path: Path,
    branch_name: str | None,
    issue_number: int,
) -> str | None:
    """Handle worktree cleanup when reuse is disabled."""
    logger.info("Worktree reuse disabled (ORCHESTRATOR_DISABLE_WORKTREE_REUSE=1)")
    recreated_reason = None
    if worktree_path.exists():
        recreated_reason = "reuse_disabled: existing worktree path removed"
        _remove_existing_worktree_path(repo_root, worktree_path)
    if branch_name:
        existing_worktree = find_worktree_for_branch(repo_root, branch_name)
        if existing_worktree and existing_worktree.exists():
            recreated_reason = "reuse_disabled: existing worktree branch removed"
            _detach_worktree_branch(existing_worktree, branch_name)
    return recreated_reason


def _try_reuse_by_branch(
    repo_root: Path,
    branch_name: str,
    issue_number: int,
    policy: WorktreePolicy,
    reuse_options: WorktreeReuseOptions,
    enforce_hooks: bool,
    pre_push_hook: Path | None,
    base_branch: str | None,
) -> tuple[tuple[Path, str, str, str | None, bool, int, int] | None, str | None]:
    """Try to reuse an existing worktree by branch name.

    Returns:
        Tuple of (result, recreated_reason) where:
        - result is the full result tuple if successful, None if failed
        - recreated_reason is set if the worktree was deleted (for branch_on_recreate handling)
    """
    existing_worktree = find_worktree_for_branch(repo_root, branch_name)
    if not existing_worktree or not existing_worktree.exists():
        return (None, None)

    logger.info(issue_log(issue_number, "Reusing existing worktree: branch=%s path=%s"), branch_name, existing_worktree)

    result = _try_reuse_worktree(
        existing_worktree,
        branch_name,
        repo_root,
        issue_number,
        policy,
        reuse_options.reuse_push_preflight,
        reuse_options.allow_no_verify_dry_run_preflight,
        base_branch,
    )

    if not result.success:
        # Return the recreated_reason so create_worktree can handle branch_on_recreate
        return (None, result.recreated_reason)

    # Success - finalize and return
    _finalize_worktree(
        existing_worktree, repo_root, enforce_hooks, pre_push_hook,
        reuse_options.allow_no_verify_dry_run_preflight,
    )
    logger.info(issue_log(issue_number, "Worktree reuse complete: path=%s"), existing_worktree)
    reset_info = result.reset_info or ResetInfo(success=True)
    return (
        (
            existing_worktree,
            branch_name,
            "reused",
            "existing_worktree_by_branch",
            False,
            reset_info.uncommitted_discarded,
            reset_info.commits_discarded,
        ),
        None,
    )


def _try_reuse_by_path(
    worktree_path: Path,
    repo_root: Path,
    issue_number: int,
    policy: WorktreePolicy,
    reuse_options: WorktreeReuseOptions,
    enforce_hooks: bool,
    pre_push_hook: Path | None,
    base_branch: str | None,
) -> tuple[tuple[Path, str, str, str | None, bool, int, int] | None, str | None]:
    """Try to reuse an existing worktree by path.

    Returns:
        Tuple of (result, recreated_reason) where:
        - result is the full result tuple if successful, None if failed
        - recreated_reason is set if the worktree was deleted (for branch_on_recreate handling)
    """
    logger.info(issue_log(issue_number, "Reusing existing worktree by path: %s"), worktree_path)

    # Validate first (no expected branch - we use whatever is there)
    validation = policy.validate_for_reuse(worktree_path, None, repo_root)
    if not validation.can_reuse:
        logger.warning(
            issue_log(issue_number, "Worktree failed validation, deleting: %s"),
            validation.reason,
        )
        policy.delete_worktree(worktree_path, repo_root)
        return (None, f"validation_failed: {validation.reason}")

    # Get current branch
    branch_result = _git_run(
        worktree_path,
        ["rev-parse", "--abbrev-ref", "HEAD"],
        check=False,
    )
    if branch_result.returncode != 0:
        logger.warning(issue_log(issue_number, "Could not get branch, deleting worktree"))
        policy.delete_worktree(worktree_path, repo_root)
        return (None, "validation_failed: could not determine branch")

    existing_branch = branch_result.stdout.strip()
    logger.info(issue_log(issue_number, "Existing worktree branch: %s"), existing_branch)

    result = _try_reuse_worktree(
        worktree_path,
        existing_branch,
        repo_root,
        issue_number,
        policy,
        reuse_options.reuse_push_preflight,
        reuse_options.allow_no_verify_dry_run_preflight,
        base_branch,
    )

    if not result.success:
        # Return the recreated_reason so create_worktree can handle branch_on_recreate
        return (None, result.recreated_reason)

    # Success - finalize and return
    _finalize_worktree(
        worktree_path, repo_root, enforce_hooks, pre_push_hook,
        reuse_options.allow_no_verify_dry_run_preflight,
    )
    logger.info(issue_log(issue_number, "Worktree reuse complete: path=%s"), worktree_path)
    reset_info = result.reset_info or ResetInfo(success=True)
    return (
        (
            worktree_path,
            existing_branch,
            "reused",
            "existing_worktree_by_path",
            False,
            reset_info.uncommitted_discarded,
            reset_info.commits_discarded,
        ),
        None,
    )


def _create_fresh_worktree(
    repo_root: Path,
    worktree_path: Path,
    branch_name: str,
    base_branch: str | None,
    issue_number: int,
    enforce_hooks: bool,
    pre_push_hook: Path | None,
    reuse_options: WorktreeReuseOptions,
    recreated_reason: str | None,
) -> tuple[Path, str, str, str | None, bool, int, int]:
    """Create a fresh worktree."""
    try:
        cmd = _build_worktree_add_command(repo_root, worktree_path, branch_name, base_branch)

        logger.info(issue_log(issue_number, "Creating worktree: branch=%s path=%s"), branch_name, worktree_path)
        result = _git_run(repo_root, cmd, check=False)
        if result.returncode != 0 and _recover_stale_branch_worktree_registration(
            repo_root=repo_root,
            issue_number=issue_number,
            branch_name=branch_name,
            stderr=result.stderr or "",
        ):
            logger.info(issue_log(issue_number, "Retrying worktree create after prune: branch=%s"), branch_name)
            result = _git_run(repo_root, cmd, check=False)

        if result.returncode != 0:
            logger.error(
                issue_log(issue_number, "Worktree creation FAILED: branch=%s error=%s"),
                branch_name,
                result.stderr.strip(),
            )
            raise WorktreeError(f"Failed to create worktree: {result.stderr}")

        _finalize_worktree(
            worktree_path, repo_root, enforce_hooks, pre_push_hook,
            reuse_options.allow_no_verify_dry_run_preflight,
        )

        logger.info(issue_log(issue_number, "Worktree created: branch=%s path=%s"), branch_name, worktree_path)
        reuse_status = "recreated" if recreated_reason else "created"
        reuse_reason = recreated_reason or "no_existing_worktree"
        return worktree_path, branch_name, reuse_status, reuse_reason, False, 0, 0

    except WorktreeError:
        raise
    except Exception as e:
        raise WorktreeError(f"Error creating worktree: {e}")


def _recover_stale_branch_worktree_registration(
    repo_root: Path,
    issue_number: int,
    branch_name: str,
    stderr: str,
) -> bool:
    """Prune stale git worktree metadata when branch is bound to a missing path."""
    match = _BRANCH_IN_USE_BY_WORKTREE_RE.search(stderr)
    if not match:
        return False
    conflict_branch = match.group(1)
    conflict_path = Path(match.group(2))
    if conflict_branch != branch_name:
        return False
    if conflict_path.exists():
        return False

    logger.warning(
        issue_log(
            issue_number,
            "Detected stale worktree registration for branch=%s at missing path=%s; pruning",
        ),
        branch_name,
        conflict_path,
    )
    prune_result = _git_run(repo_root, ["worktree", "prune"], check=False)
    if prune_result.returncode != 0:
        logger.warning(
            issue_log(issue_number, "Failed to prune stale worktree registration: %s"),
            (prune_result.stderr or "").strip(),
        )
        return False
    return True


def remove_worktree(worktree_path: Path) -> None:
    """
    Remove a git worktree and its associated branch.

    Args:
        worktree_path: Path to the worktree to remove

    Raises:
        WorktreeError: If removal fails
    """
    worktree_path = Path(worktree_path)
    logger.info("Removing worktree: path=%s", worktree_path)

    if not worktree_path.exists():
        raise WorktreeError(f"Worktree does not exist at {worktree_path}")

    try:
        repo_root = _resolve_repo_root_from_worktree(worktree_path)
        if repo_root is None:
            raise WorktreeError(f"Unable to resolve repo root for {worktree_path}")

        # Remove the worktree
        cmd = ["worktree", "remove", str(worktree_path)]
        result = _git_run(
            repo_root,
            cmd,
            check=False,
        )

        if result.returncode != 0:
            logger.error(
                "Worktree removal failed: path=%s stderr=%s",
                worktree_path,
                result.stderr.strip(),
            )
            raise WorktreeError(
                f"Failed to remove worktree: {result.stderr}"
            )

        # Get the branch name from the worktree
        branch_name = get_worktree_branch(worktree_path)
        if branch_name:
            # Delete the branch
            cmd = ["branch", "-D", branch_name]
            result = _git_run(
                repo_root,
                cmd,
                check=False,
            )

            if result.returncode != 0:
                # Log but don't fail if branch deletion fails
                pass
        logger.info("Worktree removed: path=%s branch=%s", worktree_path, branch_name or "(unknown)")

    except Exception as e:
        if isinstance(e, WorktreeError):
            raise
        raise WorktreeError(f"Error removing worktree: {e}")


def list_worktrees(repo_root: Path) -> list[Path]:
    """
    List all git worktree paths.

    Returns:
        List of paths to all worktrees

    Raises:
        WorktreeError: If listing fails
    """
    try:
        cmd = ["worktree", "list", "--porcelain"]
        result = _git_run(
            repo_root,
            cmd,
            check=False,
        )

        if result.returncode != 0:
            raise WorktreeError(
                f"Failed to list worktrees: {result.stderr}"
            )

        worktrees = []
        for line in result.stdout.strip().split("\n"):
            if line.startswith("worktree "):
                path_str = line.split(maxsplit=1)[1]
                worktrees.append(Path(path_str))

        return worktrees

    except Exception as e:
        if isinstance(e, WorktreeError):
            raise
        raise WorktreeError(f"Error listing worktrees: {e}")


def worktree_exists(worktree_path: Path, repo_root: Path) -> bool:
    """
    Check if a worktree exists.

    Args:
        worktree_path: Path to check

    Returns:
        True if the worktree exists, False otherwise

    Raises:
        WorktreeError: If the check fails
    """
    try:
        worktrees = list_worktrees(repo_root)
        worktree_path = Path(worktree_path)
        return worktree_path in worktrees

    except Exception as e:
        if isinstance(e, WorktreeError):
            raise
        raise WorktreeError(f"Error checking worktree existence: {e}")


def has_uncommitted_changes(worktree_path: Path) -> bool:
    """
    Check if a worktree has uncommitted changes.

    Args:
        worktree_path: Path to the worktree

    Returns:
        True if there are uncommitted changes, False otherwise

    Raises:
        WorktreeError: If the check fails
    """
    worktree_path = Path(worktree_path)

    if not worktree_path.exists():
        raise WorktreeError(f"Worktree does not exist at {worktree_path}")

    try:
        cmd = ["status", "--porcelain"]
        result = _git_run(
            worktree_path,
            cmd,
            check=False,
        )

        if result.returncode != 0:
            raise WorktreeError(
                f"Failed to check worktree status: {result.stderr}"
            )

        # If output is empty, there are no uncommitted changes
        return bool(result.stdout.strip())

    except Exception as e:
        if isinstance(e, WorktreeError):
            raise
        raise WorktreeError(f"Error checking uncommitted changes: {e}")


def get_worktree_branch(worktree_path: Path) -> str | None:
    """
    Get the branch name for a worktree.

    Args:
        worktree_path: Path to the worktree

    Returns:
        Branch name or None if it cannot be determined

    Raises:
        WorktreeError: If the operation fails
    """
    try:
        cmd = ["rev-parse", "--abbrev-ref", "HEAD"]
        result = _git_run(
            worktree_path,
            cmd,
            check=False,
        )

        if result.returncode != 0:
            return None

        return result.stdout.strip() or None

    except Exception:
        return None
