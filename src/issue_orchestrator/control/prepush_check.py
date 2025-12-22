"""Pre-push validation check.

This module provides a CLI command for checking validation cache
and running validation if needed, for use in pre-push hooks.

Usage:
    python -m issue_orchestrator.control.prepush_check

Exit codes:
    0 = validation passed (or no validation configured)
    1 = validation failed
    2 = validation error
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml

from .validation import PublishGate

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

    Args:
        worktree: Path to the worktree root

    Returns:
        Tuple of (command, timeout_seconds) or (None, 0) if not configured
    """
    config_path = worktree / ".issue-orchestrator" / "config.yaml"
    if not config_path.exists():
        return None, 0

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

        validation = config.get("validation", {})
        publish_gate = validation.get("publish_gate", {})
        cmd = publish_gate.get("cmd")
        timeout = publish_gate.get("timeout_seconds", 1800)

        # Check if publish_requires is set
        policy = config.get("validation_policy", {})
        publish_requires = policy.get("publish_requires")

        if publish_requires == "publish_gate" and cmd:
            return cmd, timeout

        return None, 0
    except Exception as e:
        logger.warning("Failed to load config: %s", e)
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

    # Use PublishGate which handles cache lookup automatically
    gate = PublishGate(worktree, command=cmd, timeout_seconds=timeout)
    result = gate.check()

    if result.allowed:
        cache_note = " (cached)" if result.cache_hit else ""
        if verbose:
            print(f"Publish gate passed{cache_note}: {result.reason}")
        return 0
    else:
        if verbose:
            print(f"Publish gate failed: {result.reason}")
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
