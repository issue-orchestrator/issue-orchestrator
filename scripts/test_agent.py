#!/usr/bin/env python3
"""
Test agent that simulates different outcomes for integration testing.

Usage:
    python test_agent.py --issue 123 --outcome complete --delay 5

Outcomes:
    complete    - Create empty commit, push, create PR
    blocked     - Add blocked comment and label
    needs-human - Add needs-human comment and label
    timeout     - Sleep forever (orchestrator should kill)
    fail        - Exit with error code
"""

import argparse
import subprocess
import sys
import time


def run_gh(args: list[str]) -> None:
    """Run gh command."""
    subprocess.run(["gh"] + args, check=True)


def run_git(args: list[str]) -> None:
    """Run git command."""
    subprocess.run(["git"] + args, check=True)


def complete(issue: int) -> None:
    """Simulate successful completion - commit and create PR."""
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True
    ).stdout.strip()

    # Create empty commit
    run_git(["commit", "--allow-empty", "-m", f"test: fix for issue #{issue}"])

    # Push branch
    run_git(["push", "-u", "origin", branch])

    # Create PR
    run_gh([
        "pr", "create",
        "--title", f"Fix #{issue} (test)",
        "--body", f"Automated fix for issue #{issue}\n\nCloses #{issue}"
    ])

    # Post completion comment
    run_gh([
        "issue", "comment", str(issue),
        "--body", "## ✅ Completed\n\n**PR:** Created successfully\n**Status:** Ready for review"
    ])


def blocked(issue: int) -> None:
    """Simulate blocked state."""
    run_gh([
        "issue", "comment", str(issue),
        "--body", """## 🚧 Blocked

**Reason:** Test simulation - pretending to be blocked
**Blocked by:** N/A (test)
**Attempted:** Simulated attempt
**Unblock action:** This is a test, just remove the blocked label
"""
    ])
    run_gh(["issue", "edit", str(issue), "--add-label", "blocked"])


def needs_human(issue: int) -> None:
    """Simulate needs-human state."""
    run_gh([
        "issue", "comment", str(issue),
        "--body", """## ❓ Needs Human

**Question:** This is a test question - which option should I choose?
**Context:** Running in test mode
**Options:**
1. Option A
2. Option B
**Default if no response:** Will proceed with Option A after 1 hour
"""
    ])
    run_gh(["issue", "edit", str(issue), "--add-label", "needs-human"])


def timeout(issue: int) -> None:
    """Simulate timeout - sleep forever."""
    print(f"Simulating timeout for issue #{issue}...")
    while True:
        time.sleep(60)


def fail(issue: int) -> None:
    """Simulate failure."""
    print(f"Simulating failure for issue #{issue}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Test agent for issue-orchestrator")
    parser.add_argument("--issue", type=int, required=True, help="Issue number")
    parser.add_argument(
        "--outcome",
        choices=["complete", "blocked", "needs-human", "timeout", "fail"],
        default="complete",
        help="Outcome to simulate"
    )
    parser.add_argument("--delay", type=int, default=5, help="Delay before action (seconds)")

    args = parser.parse_args()

    print(f"Test agent starting: issue=#{args.issue}, outcome={args.outcome}, delay={args.delay}s")
    time.sleep(args.delay)

    outcomes = {
        "complete": complete,
        "blocked": blocked,
        "needs-human": needs_human,
        "timeout": timeout,
        "fail": fail,
    }

    outcomes[args.outcome](args.issue)

    print(f"Test agent completed: {args.outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
