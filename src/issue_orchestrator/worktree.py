"""Git worktree management module."""

import logging
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


# Path to bundled hooks (relative to this module)
HOOKS_DIR = Path(__file__).parent / "hooks"


class WorktreeError(Exception):
    """Raised when a worktree operation fails."""

    pass


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
    hooks_path_result = subprocess.run(
        ["git", "-C", str(worktree_path), "config", "--get", "core.hooksPath"],
        capture_output=True, text=True, check=False
    )
    custom_hooks_path = hooks_path_result.stdout.strip() if hooks_path_result.returncode == 0 else None

    # Find the project's pre-push hook
    project_hook = None
    if custom_hooks_path:
        # Project uses custom hooksPath (e.g., .githooks/)
        # Resolve relative to worktree root
        custom_hooks_dir = worktree_path / custom_hooks_path
        project_hook = custom_hooks_dir / "pre-push"

        # Override hooksPath for this worktree only (not repo-wide)
        # Using --worktree flag stores config in worktree-specific config file
        # This allows our chained hooks to run while preserving project hooks
        # First enable worktree config extension (required for --worktree flag)
        subprocess.run(
            ["git", "-C", str(worktree_path), "config", "extensions.worktreeConfig", "true"],
            capture_output=True, check=False
        )
        subprocess.run(
            ["git", "-C", str(worktree_path), "config", "--worktree", "core.hooksPath", str(hooks_dir)],
            capture_output=True, check=False
        )
        logger.info("Overriding core.hooksPath to %s for this worktree only", hooks_dir)
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

audit "Pre-push hook started (commit: $(git rev-parse --short HEAD))"

# Run project's pre-push hook first (lint, tests, etc.)
if [ -x "$HOOKS_DIR/pre-push.project" ]; then
    audit "Running project pre-push hook..."
    if "$HOOKS_DIR/pre-push.project" "$@"; then
        audit "Project hook PASSED"
    else
        audit "Project hook FAILED (exit $?)"
        exit 1
    fi
else
    audit "No project hook found"
fi

# Then run orchestrator's trailer validation
if [ -x "$HOOKS_DIR/pre-push.orchestrator" ]; then
    audit "Running orchestrator pre-push hook..."
    if "$HOOKS_DIR/pre-push.orchestrator" "$@"; then
        audit "Orchestrator hook PASSED"
    else
        audit "Orchestrator hook FAILED (exit $?)"
        exit 1
    fi
else
    audit "No orchestrator hook found"
fi

audit "Pre-push hook completed successfully"
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


def create_worktree(
    repo_root: Path,
    issue_number: int,
    issue_title: str,
    worktree_base: Path | None = None,
    enforce_hooks: bool = True,
    pre_push_hook: Path | None = None,
    branch_name: str | None = None,
) -> tuple[Path, str]:
    """
    Create a new git worktree for the given issue.

    Args:
        repo_root: Path to the main git repository
        issue_number: GitHub issue number
        issue_title: GitHub issue title (used to generate branch name if not provided)
        worktree_base: Base directory for worktrees. Defaults to parent of repo_root.
        enforce_hooks: Whether to install pre-push hooks
        pre_push_hook: Custom pre-push hook path
        branch_name: Specific branch to use (for checking out existing branches like PR reviews)

    Returns:
        Tuple of (worktree_path, branch_name)

    Raises:
        WorktreeError: If worktree creation fails
    """
    repo_root = Path(repo_root).resolve()

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

    # Get repo name for worktree directory
    repo_name = repo_root.name

    # Worktree path: {base}/{repo_name}-{issue_number}
    worktree_path = worktree_base / f"{repo_name}-{issue_number}"

    # Prune stale worktrees (handles case where directory was deleted but git still has it registered)
    subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "prune"],
        capture_output=True, check=False
    )

    # Check if worktree already exists - if so, reuse it (faster than delete/recreate)
    if worktree_path.exists():
        # Verify it's a valid git worktree
        git_dir = worktree_path / ".git"
        if git_dir.exists():
            # Valid worktree - reuse it by pulling latest changes
            logger.info("Reusing existing worktree at %s", worktree_path)
            # Try to get current branch
            branch_result = subprocess.run(
                ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, check=False
            )
            if branch_result.returncode == 0:
                existing_branch = branch_result.stdout.strip()
                # Pull latest changes (best effort)
                subprocess.run(
                    ["git", "-C", str(worktree_path), "pull", "--rebase"],
                    capture_output=True, check=False
                )
                # Reinstall hooks (ensures latest hook chaining logic is applied)
                if enforce_hooks:
                    install_hooks(worktree_path, pre_push_hook)
                return worktree_path, existing_branch
        # Invalid worktree directory - remove it
        logger.warning("Removing invalid worktree directory at %s", worktree_path)
        shutil.rmtree(worktree_path, ignore_errors=True)

    try:
        # Check if branch already exists
        branch_check = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", branch_name],
            capture_output=True, text=True, check=False
        )
        branch_exists = branch_check.returncode == 0

        if branch_exists:
            # Use existing branch
            cmd = ["git", "-C", str(repo_root), "worktree", "add", str(worktree_path), branch_name]
        else:
            # Create new branch
            cmd = ["git", "-C", str(repo_root), "worktree", "add", str(worktree_path), "-b", branch_name]

        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False
        )

        if result.returncode != 0:
            raise WorktreeError(
                f"Failed to create worktree: {result.stderr}"
            )

        # Install git hooks for agent enforcement (if enabled)
        if enforce_hooks:
            install_hooks(worktree_path, pre_push_hook)

        return worktree_path, branch_name

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

    if not worktree_path.exists():
        raise WorktreeError(f"Worktree does not exist at {worktree_path}")

    try:
        # Remove the worktree
        cmd = ["git", "worktree", "remove", str(worktree_path)]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False
        )

        if result.returncode != 0:
            raise WorktreeError(
                f"Failed to remove worktree: {result.stderr}"
            )

        # Get the branch name from the worktree
        branch_name = _get_worktree_branch(worktree_path)
        if branch_name:
            # Delete the branch
            cmd = ["git", "branch", "-D", branch_name]
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False
            )

            if result.returncode != 0:
                # Log but don't fail if branch deletion fails
                pass

    except Exception as e:
        if isinstance(e, WorktreeError):
            raise
        raise WorktreeError(f"Error removing worktree: {e}")


def list_worktrees() -> list[Path]:
    """
    List all git worktree paths.

    Returns:
        List of paths to all worktrees

    Raises:
        WorktreeError: If listing fails
    """
    try:
        cmd = ["git", "worktree", "list", "--porcelain"]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False
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


def worktree_exists(worktree_path: Path) -> bool:
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
        worktrees = list_worktrees()
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
        cmd = ["git", "-C", str(worktree_path), "status", "--porcelain"]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False
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
        cmd = ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False
        )

        if result.returncode != 0:
            return None

        return result.stdout.strip() or None

    except Exception:
        return None
