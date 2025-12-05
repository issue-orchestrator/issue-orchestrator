#!/usr/bin/env python3
"""
Reset test environment: teardown existing test data, then create fresh test issues.

Usage:
    python scripts/test_reset.py

This is a convenience wrapper that runs:
1. teardown_test_issues.py - Clean up old test data
2. setup_test_issues.py - Create fresh test issues
"""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    scripts_dir = Path(__file__).parent

    print("=" * 60)
    print("TEST RESET: Clean slate for integration testing")
    print("=" * 60)
    print()

    # Step 1: Teardown
    print("STEP 1: Tearing down existing test data...")
    print("-" * 40)
    result = subprocess.run(
        [sys.executable, scripts_dir / "teardown_test_issues.py"],
        cwd=scripts_dir.parent,  # Run from repo root
    )
    if result.returncode != 0:
        print("Warning: Teardown had issues, continuing anyway...")
    print()

    # Step 2: Setup
    print("STEP 2: Creating fresh test issues...")
    print("-" * 40)
    result = subprocess.run(
        [sys.executable, scripts_dir / "setup_test_issues.py"],
        cwd=scripts_dir.parent,
    )
    if result.returncode != 0:
        print("Error: Setup failed!")
        return 1
    print()

    print("=" * 60)
    print("TEST RESET COMPLETE")
    print()
    print("You can now run:")
    print("  issue-orchestrator start --test-mode")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
