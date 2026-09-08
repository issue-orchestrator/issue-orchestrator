"""Committed scratch checkouts with explicit branch-preserving reuse."""

from pathlib import Path
import subprocess


def initialize_scenario_checkout(worktree: Path, branch: str) -> None:
    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    if (worktree / ".git").exists():
        if git("branch", "--show-current") != branch:
            raise ValueError("scenario checkout belongs to a different branch")
        # Reuse preserves commits and all uncommitted scenario artifacts.
        git("rev-parse", "--verify", "HEAD")
        return
    git("init")
    git("config", "user.email", "test@test.com")
    git("config", "user.name", "Test User")
    git("checkout", "-b", branch)
    ignore = worktree / ".gitignore"
    previous = ignore.read_text() if ignore.exists() else ""
    ignore.write_text(previous + "\n.issue-orchestrator/\n")
    git("add", ".gitignore")
    git("commit", "-m", "Scenario baseline")


def initialize_linked_scenario_checkout(repository: Path, worktree: Path, branch: str) -> None:
    """Share real Git objects so custody pins survive the source checkout."""
    from issue_orchestrator.execution.git_tools import create_git
    from issue_orchestrator.execution.command_runner import LocalCommandRunner
    git = create_git(LocalCommandRunner())
    if (worktree / ".git").exists():
        current = git.run(worktree, ["branch", "--show-current"]).stdout.strip()
        if current != branch:
            raise ValueError(f"scenario checkout {worktree} belongs to {current}, requested {branch}")
        return
    exists = git.run(repository, ["show-ref", "--verify", f"refs/heads/{branch}"], check=False).returncode == 0
    arguments = ["worktree", "add", "--force"]
    arguments += [str(worktree), branch] if exists else ["-b", branch, str(worktree), "HEAD"]
    git.run(repository, arguments)
