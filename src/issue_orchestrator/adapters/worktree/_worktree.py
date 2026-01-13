"""Git worktree management module."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...infra.logging_config import issue_log
from ...ports.git import GitResult
from ...ports.worktree_policy import WorktreePolicy
from ..git.git_cli import GitCLI, SubprocessCommandRunner

if TYPE_CHECKING:
    from issue_orchestrator.ports.worktree_manager import WorktreeReuseOptions


@dataclass
class ResetInfo:
    """Information about work discarded during worktree reset.

    This is returned by _update_worktree_onto_main and propagated
    to WorktreeInfo for event emission.
    """
    success: bool
    uncommitted_discarded: int = 0  # Count of uncommitted changes discarded
    commits_discarded: int = 0  # Count of commits discarded (on rebase failure)


logger = logging.getLogger(__name__)

_git = GitCLI(runner=SubprocessCommandRunner())


def _git_run(
    repo: Path,
    argv: list[str],
    *,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> GitResult:
    return _git.run(repo=repo, argv=argv, check=check, env=env)


# Path to bundled hooks (in issue_orchestrator/hooks/, 3 levels up from this module)
HOOKS_DIR = Path(__file__).parent.parent.parent / "hooks"

# Claude Code settings to enforce agent-done on exit
# The Stop hook checks for a marker file that agent-done creates
CLAUDE_SETTINGS_FOR_AGENTS = {
    "hooks": {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "test -f .agent-done-marker || echo '⚠️  WARNING: Session ending without agent-done! Run: agent-done completed/blocked/needs_human'",
                        "timeout": 5
                    }
                ]
            }
        ]
    }
}


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


def _update_worktree_onto_main(worktree_path: Path, repo_root: Path) -> ResetInfo:
    """
    Update a worktree's branch onto the latest main.

    This is crucial for reruns - branches created from old main need to be
    rebased onto current main to get the latest code changes.

    Strategy: Success > preserving agent work. If rebase fails for any reason
    (conflicts, stale branch, etc.), discard all local work and reset to main.
    This ensures agents always work with the latest code.

    The sequence:
    1. Fetch origin to get latest main
    2. Discard any uncommitted changes
    3. Try to rebase current branch onto origin/main
    4. If rebase fails, hard reset to origin/main (discards all local commits)

    Args:
        worktree_path: Path to the worktree to update
        repo_root: Path to the main repository

    Returns:
        ResetInfo with success status and counts of discarded work
    """
    uncommitted_discarded = 0
    commits_discarded = 0
    try:
        # Step 1: Fetch origin to get latest main
        fetch_result = _git_run(
            worktree_path,
            ["fetch", "origin", "main"],
            check=False,
        )
        if fetch_result.returncode != 0:
            logger.warning(
                "Failed to fetch origin main in worktree %s: %s",
                worktree_path,
                fetch_result.stderr.strip(),
            )
            # Non-fatal - try to continue with whatever main we have

        # Step 2: Get current branch
        branch_result = _git_run(
            worktree_path,
            ["rev-parse", "--abbrev-ref", "HEAD"],
            check=False,
        )
        if branch_result.returncode != 0:
            logger.warning("Could not determine current branch in %s", worktree_path)
            return ResetInfo(success=False)

        current_branch = branch_result.stdout.strip()
        if current_branch in ("main", "master"):
            # Already on main, just pull
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

        # Step 4: Try to rebase onto origin/main
        logger.info(
            "Rebasing branch %s onto origin/main in %s",
            current_branch,
            worktree_path,
        )
        rebase_result = _git_run(
            worktree_path,
            ["rebase", "origin/main"],
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
                ["rev-list", "--count", "origin/main..HEAD"],
                check=False,
            )
            if commits_result.returncode == 0 and commits_result.stdout.strip():
                try:
                    commits_discarded = int(commits_result.stdout.strip())
                except ValueError:
                    pass

            logger.warning(
                "[WORKTREE_RESET] Rebase failed, discarding %d commits and resetting to main "
                "(branch: %s, path: %s, error: %s)",
                commits_discarded,
                current_branch,
                worktree_path,
                rebase_result.stderr.strip(),
            )

            # Hard reset to origin/main - discards all local commits
            _git_run(
                worktree_path,
                ["reset", "--hard", "origin/main"],
                check=False,
            )
            return ResetInfo(
                success=True,
                uncommitted_discarded=uncommitted_discarded,
                commits_discarded=commits_discarded,
            )

        logger.info(
            "Successfully rebased branch %s onto origin/main in %s",
            current_branch,
            worktree_path,
        )
        return ResetInfo(success=True, uncommitted_discarded=uncommitted_discarded)

    except Exception as e:
        logger.exception("Error updating worktree onto main: %s", e)
        return ResetInfo(success=False)


def _push_dry_run_preflight(worktree_path: Path, branch_name: str) -> tuple[bool, str]:
    """Check if a dry-run push would succeed for a reused worktree."""
    import time

    cmd = ["push", "--dry-run", "--force-with-lease", "--no-verify", "-u", "origin", branch_name]
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


def install_venv_symlink(worktree_path: Path, repo_root: Path) -> bool:
    """
    Symlink .venv from main repo into worktree.

    This gives the worktree access to dev tools (pyright, pytest, etc.)
    so the agent can run validation during their work and catch issues early.

    Args:
        worktree_path: Path to the worktree
        repo_root: Path to the main repository

    Returns:
        True if symlink was created, False if .venv doesn't exist in main repo
    """
    main_venv = repo_root / ".venv"
    worktree_venv = worktree_path / ".venv"

    if not main_venv.exists():
        logger.debug("No .venv in main repo at %s, skipping symlink", main_venv)
        return False

    if worktree_venv.exists() or worktree_venv.is_symlink():
        # Already exists (symlink or real) - don't overwrite
        logger.debug(".venv already exists in worktree at %s", worktree_venv)
        return True

    try:
        worktree_venv.symlink_to(main_venv)
        logger.info("Symlinked .venv: %s -> %s", worktree_venv, main_venv)
        return True
    except OSError as e:
        logger.warning("Failed to symlink .venv: %s", e)
        return False


def sync_cli_tools(worktree_path: Path, repo_root: Path) -> None:
    """
    Sync CLI tools from main repo to worktree.

    This ensures the worktree has the latest orchestrator tools (especially
    agent-done) regardless of when the worktree was created or what branch
    it's on. The venv symlink means entry points use main repo's source,
    but we also copy the source files so the worktree has them available.

    Args:
        worktree_path: Path to the worktree
        repo_root: Path to the main repository
    """
    src_cli_tools = repo_root / "src" / "issue_orchestrator" / "entrypoints" / "cli_tools"
    dst_cli_tools = worktree_path / "src" / "issue_orchestrator" / "entrypoints" / "cli_tools"

    if not src_cli_tools.exists():
        logger.debug("No cli_tools in main repo at %s, skipping sync", src_cli_tools)
        return

    if not dst_cli_tools.parent.exists():
        logger.debug("Worktree entrypoints dir doesn't exist at %s, skipping sync", dst_cli_tools.parent)
        return

    # Copy each .py file from source to destination
    for src_file in src_cli_tools.glob("*.py"):
        dst_file = dst_cli_tools / src_file.name
        try:
            shutil.copy2(src_file, dst_file)
            logger.debug("Synced cli tool: %s -> %s", src_file.name, dst_file)
        except OSError as e:
            logger.warning("Failed to sync cli tool %s: %s", src_file.name, e)

    logger.info("Synced cli_tools from main repo to worktree")


def install_claude_settings(worktree_path: Path) -> None:
    """
    Install Claude Code settings to enforce agent-done on exit.

    Creates .claude/settings.json in the worktree with a Stop hook
    that checks if agent-done was called before allowing exit.

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


def _next_branch_name(repo_root: Path, branch_name: str) -> str:
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
        ["push", "origin", "--delete", branch_name],
        check=False,
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


def _detach_worktree_branch(worktree_path: Path, branch_name: str) -> None:
    logger.info(
        "Detaching worktree branch to free branch: path=%s branch=%s",
        worktree_path,
        branch_name,
    )
    env = None
    cmd = ["checkout", "--detach"]
    git_file = worktree_path / ".git"
    if git_file.exists():
        content = git_file.read_text().strip()
        if content.startswith("gitdir:"):
            git_dir = content.split(":", 1)[1].strip()
            env = os.environ.copy()
            env["GIT_DIR"] = git_dir
            env["GIT_WORK_TREE"] = str(worktree_path)
            logger.info("Detaching with explicit GIT_DIR for worktree: %s", worktree_path)

    result = _git_run(
        worktree_path,
        cmd,
        check=False,
        env=env,
    )
    if result.returncode != 0:
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


def create_worktree(
    repo_root: Path,
    issue_number: int,
    issue_title: str,
    worktree_base: Path | None = None,
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
    # Get default policy if none provided
    if policy is None:
        from .worktree_policy import default_policy
        policy = default_policy
    if reuse_options is None:
        reuse_options = WorktreeReuseOptions()
    reuse_push_preflight = reuse_options.reuse_push_preflight
    worktree_branch_on_recreate = reuse_options.worktree_branch_on_recreate
    repo_root = Path(repo_root).resolve()
    logger.info(
        issue_log(issue_number, "Create worktree requested: branch=%s base=%s"),
        branch_name or "(auto)",
        worktree_base,
    )
    reuse_status = "created"
    reuse_reason = "no_existing_worktree"
    recreated_reason: str | None = None

    if not (repo_root / ".git").exists():
        raise WorktreeError(f"Not a git repository: {repo_root}")

    # Default worktree location: sibling to repo
    if worktree_base is None:
        worktree_base = repo_root.parent
    else:
        worktree_base = Path(worktree_base).resolve()

    worktree_base.mkdir(parents=True, exist_ok=True)

    # Use provided branch name or generate one
    if branch_name is None:
        branch_name = generate_branch_name(issue_number, issue_title)
    logger.info(
        issue_log(issue_number, "Resolved worktree target: branch=%s base=%s"),
        branch_name,
        worktree_base,
    )

    # Get repo name for worktree directory
    repo_name = repo_root.name

    # Worktree path: {base}/{repo_name}-{issue_number}
    worktree_path = worktree_base / f"{repo_name}-{issue_number}"

    disable_reuse = os.environ.get("ORCHESTRATOR_DISABLE_WORKTREE_REUSE") == "1"

    # Prune stale worktrees (handles case where directory was deleted but git still has it registered)
    prune_result = _git_run(
        repo_root,
        ["worktree", "prune"],
        check=False,
    )
    if prune_result.stderr:
        prune_stderr = str(prune_result.stderr)
    else:
        prune_stderr = ""
    logger.debug(
        "Worktree prune: repo=%s returncode=%s stderr=%s",
        repo_root,
        prune_result.returncode,
        prune_stderr,
    )
    if disable_reuse:
        logger.info("Worktree reuse disabled (ORCHESTRATOR_DISABLE_WORKTREE_REUSE=1)")
        if worktree_path.exists():
            reuse_status = "recreated"
            recreated_reason = "reuse_disabled: existing worktree path removed"
            _remove_existing_worktree_path(repo_root, worktree_path)
        if branch_name:
            existing_worktree = find_worktree_for_branch(repo_root, branch_name)
            if existing_worktree and existing_worktree.exists():
                reuse_status = "recreated"
                recreated_reason = "reuse_disabled: existing worktree branch removed"
                _detach_worktree_branch(existing_worktree, branch_name)

    # If a specific branch was requested, check if it's already checked out in another worktree
    # This is common when reviewing PRs - the branch may still be checked out from the work session
    if branch_name and not disable_reuse:
        existing_worktree = find_worktree_for_branch(repo_root, branch_name)
        if existing_worktree and existing_worktree.exists():
            logger.info(issue_log(issue_number, "Reusing existing worktree: branch=%s path=%s"), branch_name, existing_worktree)

            # Policy: validate worktree can be reused
            validation = policy.validate_for_reuse(existing_worktree, branch_name, repo_root)
            if not validation.can_reuse:
                logger.warning(
                    issue_log(issue_number, "Worktree failed validation, deleting: %s"),
                    validation.reason,
                )
                reuse_status = "recreated"
                recreated_reason = f"validation_failed: {validation.reason}"
                policy.delete_worktree(existing_worktree, repo_root)
                # Fall through to fresh creation
            else:
                # Rebase onto latest main (critical for reruns with stale branches)
                reset_info = _update_worktree_onto_main(existing_worktree, repo_root)

                # Policy: sync remote refs to prevent stale-info push failures
                sync_result = policy.sync_remote_refs(existing_worktree, branch_name)
                if not sync_result.success:
                    logger.warning(
                        issue_log(issue_number, "Failed to sync remote refs, deleting worktree: %s"),
                        sync_result.reason,
                    )
                    reuse_status = "recreated"
                    recreated_reason = f"sync_failed: {sync_result.reason}"
                    policy.delete_worktree(existing_worktree, repo_root)
                    # Fall through to fresh creation
                elif not reset_info.success:
                    logger.warning(
                        issue_log(issue_number, "Reset to main failed, deleting worktree"),
                    )
                    reuse_status = "recreated"
                    recreated_reason = "reset_failed: rebase onto main failed"
                    policy.delete_worktree(existing_worktree, repo_root)
                    # Fall through to fresh creation
                else:
                    if reuse_push_preflight:
                        ok, reason = _push_dry_run_preflight(existing_worktree, branch_name)
                        if not ok:
                            logger.warning(
                                issue_log(issue_number, "Push preflight failed, deleting worktree: %s"),
                                reason,
                            )
                            reuse_status = "recreated"
                            recreated_reason = f"push_preflight_failed: {reason}"
                            policy.delete_worktree(existing_worktree, repo_root)
                            # Fall through to fresh creation
                        else:
                            logger.info(
                                issue_log(issue_number, "Push preflight ok for reused worktree"),
                            )
                    if reuse_status != "recreated":
                        # Success - install hooks and return
                        if enforce_hooks:
                            install_hooks(existing_worktree, pre_push_hook)
                        install_claude_settings(existing_worktree)
                        install_venv_symlink(existing_worktree, repo_root)
                        sync_cli_tools(existing_worktree, repo_root)
                        logger.info(issue_log(issue_number, "Worktree reuse complete: path=%s"), existing_worktree)
                        reuse_status = "reused"
                        reuse_reason = "existing_worktree_by_branch"
                        return (
                            existing_worktree,
                            branch_name,
                            reuse_status,
                            reuse_reason,
                            False,
                            reset_info.uncommitted_discarded,
                            reset_info.commits_discarded,
                        )

    # Check if worktree already exists - if so, reuse it (faster than delete/recreate)
    if worktree_path.exists() and not disable_reuse:
        logger.info(issue_log(issue_number, "Reusing existing worktree by path: %s"), worktree_path)

        # Policy: validate worktree can be reused (no expected branch - we use whatever is there)
        validation = policy.validate_for_reuse(worktree_path, None, repo_root)
        if not validation.can_reuse:
            logger.warning(
                issue_log(issue_number, "Worktree failed validation, deleting: %s"),
                validation.reason,
            )
            reuse_status = "recreated"
            recreated_reason = f"validation_failed: {validation.reason}"
            policy.delete_worktree(worktree_path, repo_root)
            # Fall through to fresh creation
        else:
            # Get current branch for sync and return value
            branch_result = _git_run(
                worktree_path,
                ["rev-parse", "--abbrev-ref", "HEAD"],
                check=False,
            )
            if branch_result.returncode != 0:
                logger.warning(issue_log(issue_number, "Could not get branch, deleting worktree"))
                reuse_status = "recreated"
                recreated_reason = "branch_lookup_failed"
                policy.delete_worktree(worktree_path, repo_root)
                # Fall through to fresh creation
            else:
                existing_branch = branch_result.stdout.strip()
                logger.info(issue_log(issue_number, "Existing worktree branch: %s"), existing_branch)

                # Rebase onto latest main (critical for reruns with stale branches)
                reset_info = _update_worktree_onto_main(worktree_path, repo_root)

                # Policy: sync remote refs to prevent stale-info push failures
                sync_result = policy.sync_remote_refs(worktree_path, existing_branch)
                if not sync_result.success:
                    logger.warning(
                        issue_log(issue_number, "Failed to sync remote refs, deleting worktree: %s"),
                        sync_result.reason,
                    )
                    reuse_status = "recreated"
                    recreated_reason = f"sync_failed: {sync_result.reason}"
                    policy.delete_worktree(worktree_path, repo_root)
                    # Fall through to fresh creation
                elif not reset_info.success:
                    logger.warning(issue_log(issue_number, "Reset to main failed, deleting worktree"))
                    reuse_status = "recreated"
                    recreated_reason = "reset_failed: rebase onto main failed"
                    policy.delete_worktree(worktree_path, repo_root)
                    # Fall through to fresh creation
                else:
                    if reuse_push_preflight:
                        ok, reason = _push_dry_run_preflight(worktree_path, existing_branch)
                        if not ok:
                            logger.warning(
                                issue_log(issue_number, "Push preflight failed, deleting worktree: %s"),
                                reason,
                            )
                            reuse_status = "recreated"
                            recreated_reason = f"push_preflight_failed: {reason}"
                            policy.delete_worktree(worktree_path, repo_root)
                            # Fall through to fresh creation
                        else:
                            logger.info(
                                issue_log(issue_number, "Push preflight ok for reused worktree"),
                            )
                    if reuse_status != "recreated":
                        # Success - install hooks and return
                        if enforce_hooks:
                            install_hooks(worktree_path, pre_push_hook)
                        install_claude_settings(worktree_path)
                        install_venv_symlink(worktree_path, repo_root)
                        sync_cli_tools(worktree_path, repo_root)
                        logger.info(issue_log(issue_number, "Worktree reuse complete: path=%s"), worktree_path)
                        reuse_status = "reused"
                        reuse_reason = "existing_worktree_by_path"
                        return (
                            worktree_path,
                            existing_branch,
                            reuse_status,
                            reuse_reason,
                            False,
                            reset_info.uncommitted_discarded,
                            reset_info.commits_discarded,
                        )

    if recreated_reason and branch_name and _branch_matches_issue(branch_name, issue_number):
        if worktree_branch_on_recreate == "delete":
            if _delete_remote_branch(repo_root, branch_name):
                logger.info(issue_log(issue_number, "Deleted remote branch before recreate: %s"), branch_name)
            else:
                logger.warning(issue_log(issue_number, "Failed to delete remote branch before recreate: %s"), branch_name)
        elif worktree_branch_on_recreate == "create_new_branch":
            new_branch = _next_branch_name(repo_root, branch_name)
            logger.info(
                issue_log(issue_number, "Creating new branch for recreated worktree: %s -> %s"),
                branch_name,
                new_branch,
            )
            branch_name = new_branch

    try:
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
            cmd = ["worktree", "add", str(worktree_path), branch_name]
        else:
            # Try to fetch remote branch (for review/rework sessions)
            fetch_result = _git_run(
                repo_root,
                ["fetch", "origin", branch_name],
                check=False,
            )
            if fetch_result.returncode == 0:
                cmd = [
                    "worktree", "add",
                    str(worktree_path), "-b", branch_name, f"origin/{branch_name}"
                ]
            else:
                # Create new branch from default branch (main), NOT from HEAD
                # This ensures agent worktrees don't inherit commits from user's feature branch
                # Unless ORCHESTRATOR_WORKTREE_BASE_BRANCH is set (e.g., for e2e tests)
                base_branch_override = os.environ.get("ORCHESTRATOR_WORKTREE_BASE_BRANCH")
                if base_branch_override:
                    default_branch = base_branch_override
                    logger.info("Using override base branch: %s (from ORCHESTRATOR_WORKTREE_BASE_BRANCH)", default_branch)
                else:
                    default_branch = get_default_branch(repo_root)
                    logger.info("Creating new branch from default branch: %s", default_branch)
                cmd = [
                    "worktree", "add",
                    str(worktree_path), "-b", branch_name, default_branch
                ]

        logger.info(issue_log(issue_number, "Creating worktree: branch=%s path=%s"), branch_name, worktree_path)
        result = _git_run(
            repo_root,
            cmd,
            check=False,
        )

        if result.returncode != 0:
            logger.error(
                issue_log(issue_number, "Worktree creation FAILED: branch=%s error=%s"),
                branch_name,
                result.stderr.strip(),
            )
            raise WorktreeError(
                f"Failed to create worktree: {result.stderr}"
            )

        # Install git hooks for agent enforcement (if enabled)
        if enforce_hooks:
            install_hooks(worktree_path, pre_push_hook)

        # Install Claude Code settings with exit hook to enforce agent-done
        install_claude_settings(worktree_path)

        # Symlink .venv so agent has access to dev tools for validation
        install_venv_symlink(worktree_path, repo_root)

        # Sync cli_tools from main repo (ensures latest agent-done)
        sync_cli_tools(worktree_path, repo_root)

        logger.info(issue_log(issue_number, "Worktree created: branch=%s path=%s"), branch_name, worktree_path)
        if reuse_status == "recreated" and recreated_reason:
            reuse_reason = recreated_reason
        return worktree_path, branch_name, reuse_status, reuse_reason, False, 0, 0  # No rebase failure - new worktree

    except Exception as e:
        if isinstance(e, WorktreeError):
            raise
        raise WorktreeError(f"Error creating worktree: {e}")


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
        branch_name = _get_worktree_branch(worktree_path)
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


def _get_worktree_branch(worktree_path: Path) -> str | None:
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
