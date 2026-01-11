"""Sandbox verification for agent sessions.

This module provides verification that an agent session is properly sandboxed:
1. Git push fails fast (no credentials to push)
2. Forbidden environment variables are absent
3. HOME is isolated to the worktree

This verification should run at the start of each agent session to ensure
the agent cannot perform privileged operations.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..adapters.git.git_cli import GitCLI
from ..control.isolation import verify_env_scrubbed
from ..execution.command_runner import LocalCommandRunner
from ..ports.git import GitError

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    """Result of a single verification check."""

    name: str
    passed: bool
    message: str
    critical: bool = True  # If False, failure is a warning not an error


@dataclass
class SandboxVerificationResult:
    """Result of sandbox verification."""

    all_passed: bool
    results: list[VerificationResult]
    critical_failures: list[str]
    warnings: list[str]

    @property
    def summary(self) -> str:
        """Get a summary of the verification results."""
        if self.all_passed:
            return "All sandbox checks passed"
        else:
            failures = [r.name for r in self.results if not r.passed and r.critical]
            return f"Sandbox verification failed: {', '.join(failures)}"


def verify_git_push_fails(worktree: Path) -> VerificationResult:
    """Verify that git push fails fast (no credentials)."""
    try:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "/bin/false"

        git = GitCLI(runner=LocalCommandRunner())
        result = git.run(
            worktree,
            ["push", "--dry-run"],
            env=env,
            timeout_s=10,
            check=False,
        )

        if result.returncode == -1:
            return VerificationResult(
                name="git_push_fails",
                passed=False,
                message=f"Error checking git push: {result.stderr or 'unknown error'}",
            )
        if result.returncode != 0:
            return VerificationResult(
                name="git_push_fails",
                passed=True,
                message="git push fails (no credentials - expected)",
            )
        else:
            return VerificationResult(
                name="git_push_fails",
                passed=False,
                message="git push --dry-run succeeded - agent could push directly",
            )
    except GitError as exc:
        if "git command timed out" in str(exc):
            return VerificationResult(
                name="git_push_fails",
                passed=False,
                message="git push --dry-run timed out (may be prompting for credentials)",
                critical=False,
            )
        return VerificationResult(
            name="git_push_fails",
            passed=False,
            message=f"Error checking git push: {exc}",
        )


def verify_env_vars_absent() -> VerificationResult:
    """Verify that forbidden environment variables are absent."""
    env_status = verify_env_scrubbed()
    present_vars = [var for var, absent in env_status.items() if not absent]

    if not present_vars:
        return VerificationResult(
            name="env_vars_absent",
            passed=True,
            message="All forbidden env vars absent (expected)",
        )
    else:
        return VerificationResult(
            name="env_vars_absent",
            passed=False,
            message=f"Forbidden env vars present: {', '.join(present_vars)}",
        )


def verify_home_isolated(worktree: Path) -> VerificationResult:
    """Verify that HOME is set to the worktree."""
    current_home = Path(os.environ.get("HOME", ""))
    worktree_resolved = worktree.resolve()
    home_resolved = current_home.resolve() if current_home.exists() else current_home

    if home_resolved == worktree_resolved:
        return VerificationResult(
            name="home_isolated",
            passed=True,
            message=f"HOME is isolated to worktree: {worktree}",
        )
    else:
        return VerificationResult(
            name="home_isolated",
            passed=False,
            message=f"HOME ({current_home}) is not worktree ({worktree})",
            critical=False,
        )


def verify_sandbox(
    worktree: Optional[Path] = None,
    check_git_push: bool = True,
    check_env_vars: bool = True,
    check_home: bool = True,
) -> SandboxVerificationResult:
    """Run all sandbox verification checks."""
    results: list[VerificationResult] = []

    if check_git_push and worktree:
        results.append(verify_git_push_fails(worktree))

    if check_env_vars:
        results.append(verify_env_vars_absent())

    if check_home and worktree:
        results.append(verify_home_isolated(worktree))

    critical_failures = [r.name for r in results if not r.passed and r.critical]
    warnings = [r.name for r in results if not r.passed and not r.critical]
    all_passed = len(critical_failures) == 0

    return SandboxVerificationResult(
        all_passed=all_passed,
        results=results,
        critical_failures=critical_failures,
        warnings=warnings,
    )


def main() -> None:
    """Entry point for running sandbox verification."""
    import argparse

    parser = argparse.ArgumentParser(description="Verify agent sandbox isolation")
    parser.add_argument("--worktree", type=Path, help="Worktree path for git/home checks")
    args = parser.parse_args()

    verification = verify_sandbox(worktree=args.worktree)
    if verification.all_passed:
        print("✅ " + verification.summary)
        if verification.warnings:
            print("Warnings: " + ", ".join(verification.warnings))
        raise SystemExit(0)
    else:
        print("❌ " + verification.summary)
        if verification.warnings:
            print("Warnings: " + ", ".join(verification.warnings))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
