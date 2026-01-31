"""Pre-push validation check.

This module provides a CLI command for checking validation cache
and running validation if needed, for use in pre-push hooks.

Usage:
    python -m issue_orchestrator.entrypoints.cli_tools.prepush_check

Exit codes:
    0 = validation passed (or no validation configured)
    1 = validation failed
    2 = validation error
"""

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ...control.validation import PublishGate
from ...execution import GitWorkingCopy, LocalCommandRunner

logger = logging.getLogger(__name__)


def find_worktree_root() -> Path:
    """Find the worktree root by looking for .git."""
    cwd = Path.cwd()
    for path in [cwd, *cwd.parents]:
        if (path / ".git").exists():
            return path
    return cwd


DIRTY_CHECK_MODES = {"tracked", "unstaged", "off"}


def load_validation_cmd(worktree: Path) -> tuple[Optional[str], int, str]:
    """Load validation configuration from the worktree's config file.

    Reads from .issue-orchestrator/config/ in the worktree.
    This ensures tests are deterministic - no env var leakage from parent processes.

    Args:
        worktree: Path to the worktree root

    Returns:
        Tuple of (command, timeout_seconds, pre_push_dirty_check)
    """
    from ...infra.config import load_validation_config

    # Read validation config from the worktree's config file
    validation_config = load_validation_config(worktree)

    cmd = validation_config.get("cmd")
    timeout = validation_config.get("timeout_seconds", 300)
    dirty_check = validation_config.get("pre_push_dirty_check", "tracked")
    if cmd:
        return cmd, timeout, dirty_check

    return None, 0, dirty_check


def _git_diff_quiet(worktree: Path, args: list[str]) -> bool:
    """Return True when git diff reports changes."""
    result = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--quiet", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    raise RuntimeError(result.stderr.strip() or "git diff failed")


def _has_dirty_tracked_changes(worktree: Path, mode: str) -> bool:
    """Check for dirty tracked changes based on configured mode."""
    if mode == "off":
        return False
    if _git_diff_quiet(worktree, []):
        return True
    if mode == "tracked" and _git_diff_quiet(worktree, ["--cached"]):
        return True
    return False


def run_prepush_check(verbose: bool = False) -> int:
    """Run pre-push validation check.

    This function:
    1. Finds the worktree root
    2. Loads validation config
    3. Checks cache for existing valid result
    4. Runs validation if needed
    5. Returns exit code based on result

    Args:
        verbose: Whether to print status messages

    Returns:
        Exit code (0 = passed, 1 = failed, 2 = error)
    """
    worktree = find_worktree_root()
    cmd, timeout, dirty_check = load_validation_cmd(worktree)

    if dirty_check not in DIRTY_CHECK_MODES:
        if verbose:
            print(
                "Invalid validation.pre_push_dirty_check value: "
                f"{dirty_check!r} (expected tracked|unstaged|off)"
            )
        return 1

    try:
        if _has_dirty_tracked_changes(worktree, dirty_check):
            if verbose:
                print(
                    "Tracked files are dirty; commit or stash before pushing. "
                    "Ignored files are allowed. "
                    "Override with validation.pre_push_dirty_check."
                )
            return 1
    except Exception as e:
        if verbose:
            print(f"Error checking dirty tracked files: {e}")
        return 1

    if not cmd:
        if verbose:
            print("No validation configured - allowing push")
        return 0

    if verbose:
        print(f"Validation configured: {cmd}")

    gate = PublishGate(
        worktree,
        command_runner=LocalCommandRunner(),
        working_copy=GitWorkingCopy(),
        command=cmd,
        timeout_seconds=timeout,
    )
    start = time.monotonic()
    # Create a temp directory for validation output (prepush runs outside orchestrator sessions)
    import tempfile
    with tempfile.TemporaryDirectory(prefix="prepush-validation-") as tmpdir:
        result = gate.check(session_output_dir=Path(tmpdir))
    duration = time.monotonic() - start
    logger.info(
        "Pre-push validation completed in %.2fs: allowed=%s cache_hit=%s",
        duration,
        result.allowed,
        result.cache_hit,
    )

    if result.allowed:
        cache_note = " (cached)" if result.cache_hit else ""
        if verbose:
            print(f"Validation passed{cache_note}: {result.reason} (%.2fs)" % duration)
        return 0
    else:
        if verbose:
            print(f"Validation failed: {result.reason} (%.2fs)" % duration)
            if result.record and result.record.stderr_path:
                stderr_path = worktree / result.record.stderr_path
                if stderr_path.exists():
                    print("\nValidation stderr:")
                    print(stderr_path.read_text()[:1000])
        return 1


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Run pre-push validation check",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed output",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress all output",
    )

    args = parser.parse_args()

    if args.quiet:
        logging.disable(logging.CRITICAL)

    try:
        exit_code = run_prepush_check(verbose=args.verbose)
        sys.exit(exit_code)
    except Exception as e:
        if not args.quiet:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
