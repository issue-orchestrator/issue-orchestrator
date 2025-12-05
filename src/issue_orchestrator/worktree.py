"""Git worktree management module."""

import re
import subprocess
from pathlib import Path


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
) -> tuple[Path, str]:
    """
    Create a new git worktree for the given issue.

    Args:
        repo_root: Path to the main git repository
        issue_number: GitHub issue number
        issue_title: GitHub issue title (used to generate branch name)
        worktree_base: Base directory for worktrees. Defaults to parent of repo_root.

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

    # Generate branch name: {issue_number}-{slugified-title}
    branch_name = generate_branch_name(issue_number, issue_title)

    # Get repo name for worktree directory
    repo_name = repo_root.name

    # Worktree path: {base}/{repo_name}-{issue_number}
    worktree_path = worktree_base / f"{repo_name}-{issue_number}"

    # Check if worktree already exists
    if worktree_path.exists():
        raise WorktreeError(f"Worktree already exists at {worktree_path}")

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
