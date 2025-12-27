"""Sandbox verification for agent sessions.

This module provides verification that an agent session is properly sandboxed:
1. GitHub CLI authentication is not available (gh auth status fails)
2. Git push fails fast (no credentials to push)
3. Forbidden environment variables are absent
4. HOME is isolated to the worktree

This verification should run at the start of each agent session to ensure
the agent cannot perform privileged operations.
"""

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .isolation import verify_env_scrubbed

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


def verify_gh_auth_unavailable() -> VerificationResult:
    """Verify that GitHub CLI authentication is not available.

    The gh CLI should fail with "not logged in" when gh auth status is run.
    This confirms the agent cannot use gh to perform privileged operations.

    Returns:
        VerificationResult indicating pass/fail
    """
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            # gh auth status failed - good, not authenticated
            return VerificationResult(
                name="gh_auth_unavailable",
                passed=True,
                message="GitHub CLI not authenticated (expected)",
            )
        else:
            # gh auth status succeeded - bad, we're authenticated
            return VerificationResult(
                name="gh_auth_unavailable",
                passed=False,
                message="GitHub CLI is authenticated - agent could bypass guardrails",
            )
    except FileNotFoundError:
        # gh not installed - also acceptable
        return VerificationResult(
            name="gh_auth_unavailable",
            passed=True,
            message="GitHub CLI not installed (acceptable)",
        )
    except subprocess.TimeoutExpired:
        return VerificationResult(
            name="gh_auth_unavailable",
            passed=False,
            message="gh auth status timed out",
        )
    except Exception as e:
        return VerificationResult(
            name="gh_auth_unavailable",
            passed=False,
            message=f"Error checking gh auth: {e}",
        )


def verify_git_push_fails(worktree: Path) -> VerificationResult:
    """Verify that git push fails fast (no credentials).

    Runs git push --dry-run to verify that pushing would fail.
    This confirms the agent cannot push code directly.

    Args:
        worktree: Path to the worktree to check

    Returns:
        VerificationResult indicating pass/fail
    """
    try:
        # Use GIT_TERMINAL_PROMPT=0 to ensure git doesn't prompt for credentials
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "/bin/false"

        result = subprocess.run(
            ["git", "push", "--dry-run"],
            cwd=worktree,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

        if result.returncode != 0:
            # Push failed - good, no credentials
            return VerificationResult(
                name="git_push_fails",
                passed=True,
                message="git push fails (no credentials - expected)",
            )
        else:
            # Push would succeed - bad
            return VerificationResult(
                name="git_push_fails",
                passed=False,
                message="git push --dry-run succeeded - agent could push directly",
            )
    except subprocess.TimeoutExpired:
        # Timeout might indicate credential prompt - treat as warning
        return VerificationResult(
            name="git_push_fails",
            passed=False,
            message="git push --dry-run timed out (may be prompting for credentials)",
            critical=False,  # Warning, not critical failure
        )
    except Exception as e:
        return VerificationResult(
            name="git_push_fails",
            passed=False,
            message=f"Error checking git push: {e}",
        )


def verify_env_vars_absent() -> VerificationResult:
    """Verify that forbidden environment variables are absent.

    Returns:
        VerificationResult indicating pass/fail
    """
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
    """Verify that HOME is set to the worktree.

    Args:
        worktree: Path to the expected HOME directory

    Returns:
        VerificationResult indicating pass/fail
    """
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
            critical=False,  # Warning - some setups may not use HOME isolation
        )


def verify_sandbox(
    worktree: Optional[Path] = None,
    check_gh_auth: bool = True,
    check_git_push: bool = True,
    check_env_vars: bool = True,
    check_home: bool = True,
) -> SandboxVerificationResult:
    """Run all sandbox verification checks.

    Args:
        worktree: Path to the worktree (required for git push and HOME checks)
        check_gh_auth: Whether to check gh auth status
        check_git_push: Whether to check git push
        check_env_vars: Whether to check environment variables
        check_home: Whether to check HOME isolation

    Returns:
        SandboxVerificationResult with all check results
    """
    results = []

    if check_gh_auth:
        results.append(verify_gh_auth_unavailable())

    if check_git_push and worktree:
        results.append(verify_git_push_fails(worktree))

    if check_env_vars:
        results.append(verify_env_vars_absent())

    if check_home and worktree:
        results.append(verify_home_isolated(worktree))

    # Collect failures
    critical_failures = [r.name for r in results if not r.passed and r.critical]
    warnings = [r.name for r in results if not r.passed and not r.critical]

    all_critical_passed = len(critical_failures) == 0

    result = SandboxVerificationResult(
        all_passed=all_critical_passed,
        results=results,
        critical_failures=critical_failures,
        warnings=warnings,
    )

    # Log results
    for r in results:
        if r.passed:
            logger.debug("Sandbox check %s: PASS - %s", r.name, r.message)
        elif r.critical:
            logger.error("Sandbox check %s: FAIL - %s", r.name, r.message)
        else:
            logger.warning("Sandbox check %s: WARN - %s", r.name, r.message)

    return result


def run_verification_cli() -> int:
    """Run sandbox verification as a CLI command.

    This can be called from agent sessions to verify sandbox.

    Returns:
        Exit code (0 = passed, 1 = failed)
    """
    # Get worktree from current directory
    worktree = Path.cwd()

    # Find git root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            worktree = Path(result.stdout.strip())
    except Exception:
        pass

    print("Running sandbox verification...")
    print()

    verification = verify_sandbox(worktree=worktree)

    for r in verification.results:
        status = "✓" if r.passed else ("⚠" if not r.critical else "✗")
        print(f"{status} {r.name}: {r.message}")

    print()

    if verification.all_passed:
        print("All sandbox checks passed.")
        return 0
    else:
        print(f"Sandbox verification failed: {', '.join(verification.critical_failures)}")
        if verification.warnings:
            print(f"Warnings: {', '.join(verification.warnings)}")
        return 1


def main() -> None:
    """CLI entry point for verify-agent-sandbox command."""
    import sys
    sys.exit(run_verification_cli())
