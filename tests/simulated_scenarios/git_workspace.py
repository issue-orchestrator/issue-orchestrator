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
