"""Pre-push validation check.

This module provides a CLI command for checking validation cache
and running validation if needed, for use in pre-push hooks.

Usage:
    python -m issue_orchestrator.prepush_check

Exit codes:
    0 = validation passed (or no validation configured)
    1 = validation failed
    2 = validation error
"""

import logging
import sys
import time
from pathlib import Path
from typing import Optional

from .control.validation import PublishGate
from .execution import GitWorkingCopy, LocalCommandRunner

logger = logging.getLogger(__name__)


def find_worktree_root() -> Path:
    """Find the worktree root by looking for .git."""
    cwd = Path.cwd()
    for path in [cwd, *cwd.parents]:
        if (path / ".git").exists():
            return path
    return cwd


def load_publish_gate_config(worktree: Path) -> tuple[Optional[str], int]:
    """Load publish gate configuration from the worktree.

    Uses the shared config lookup to find configuration and extract publish_gate config.

    Environment variable overrides (for testing):
    - ORCHESTRATOR_PUBLISH_GATE_CMD: Override the validation command
    - ORCHESTRATOR_PUBLISH_GATE_TIMEOUT: Override the timeout in seconds

    Args:
        worktree: Path to the worktree root

    Returns:
        Tuple of (command, timeout_seconds) or (None, 0) if not configured
    """
    import os
    from .config import load_validation_config

    # Check for environment variable override (useful for e2e tests)
    env_cmd = os.environ.get("ORCHESTRATOR_PUBLISH_GATE_CMD")
    env_timeout = os.environ.get("ORCHESTRATOR_PUBLISH_GATE_TIMEOUT")
    if env_cmd:
        timeout = int(env_timeout) if env_timeout else 120
        return env_cmd, timeout

    # Use shared config lookup (checks both .issue-orchestrator.yaml and .issue-orchestrator/config.yaml)
    validation_config = load_validation_config(worktree)

    publish_gate = validation_config["publish_gate"]
    policy = validation_config["policy"]

    # Only return command if policy enables it
    if policy["publish_requires"] == "publish_gate" and publish_gate["cmd"]:
        return publish_gate["cmd"], publish_gate["timeout_seconds"]

    return None, 0


def run_prepush_check(verbose: bool = False) -> int:
    """Run pre-push validation check.

    This function:
    1. Finds the worktree root
    2. Loads publish gate config
    3. Checks cache for existing valid result
    4. Runs validation if needed
    5. Returns exit code based on result

    Args:
        verbose: Whether to print status messages

    Returns:
        Exit code (0 = passed, 1 = failed, 2 = error)
    """
    worktree = find_worktree_root()
    cmd, timeout = load_publish_gate_config(worktree)

    if not cmd:
        if verbose:
            print("No publish gate configured - allowing push")
        return 0

    if verbose:
        print(f"Publish gate configured: {cmd}")

    gate = PublishGate(
        worktree,
        command_runner=LocalCommandRunner(),
        working_copy=GitWorkingCopy(),
        command=cmd,
        timeout_seconds=timeout,
    )
    start = time.monotonic()
    result = gate.check()
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
            print(f"Publish gate passed{cache_note}: {result.reason} (%.2fs)" % duration)
        return 0
    else:
        if verbose:
            print(f"Publish gate failed: {result.reason} (%.2fs)" % duration)
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
