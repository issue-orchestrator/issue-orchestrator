#!/usr/bin/env python3
"""
Clean up test issues after integration testing.

Usage:
    python teardown_test_issues.py

Closes all issues with the 'test-data' label in the issue-orchestrator repo.
Also cleans up any test branches and PRs.
"""

import json
import os
import subprocess
import sys

# Default to issue-orchestrator repo, override with env var if needed
REPO = os.environ.get("TEST_REPO", "brucegordon/issue-orchestrator")

# Label that marks test data
TEST_LABEL = "test-data"


def run_gh(args: list[str]) -> subprocess.CompletedProcess:
    """Run gh command and return result."""
    return subprocess.run(["gh"] + args, capture_output=True, text=True)


def close_test_issues() -> int:
    """Close all issues with test-data label."""
    result = run_gh([
        "issue", "list",
        "--repo", REPO,
        "--label", TEST_LABEL,
        "--state", "open",
        "--json", "number,title"
    ])

    if result.returncode != 0:
        print(f"Error listing issues: {result.stderr}")
        return 0

    issues = json.loads(result.stdout)
    count = 0

    for issue in issues:
        number = issue["number"]
        title = issue["title"]
        close_result = run_gh([
            "issue", "close", str(number),
            "--repo", REPO,
            "--comment", "Closed by test teardown script."
        ])

        if close_result.returncode == 0:
            print(f"Closed #{number}: {title}")
            count += 1
        else:
            print(f"Failed to close #{number}: {close_result.stderr}")

    return count


def close_test_prs() -> int:
    """Close any PRs that were created by test agents."""
    result = run_gh([
        "pr", "list",
        "--repo", REPO,
        "--state", "open",
        "--json", "number,title,headRefName"
    ])

    if result.returncode != 0:
        print(f"Error listing PRs: {result.stderr}")
        return 0

    prs = json.loads(result.stdout)
    count = 0

    for pr in prs:
        # Only close PRs that look like test PRs
        if "[TEST]" in pr["title"] or pr["title"].startswith("Test:"):
            close_result = run_gh([
                "pr", "close", str(pr["number"]),
                "--repo", REPO,
                "--delete-branch",
                "--comment", "Closed by test teardown script."
            ])

            if close_result.returncode == 0:
                print(f"Closed PR #{pr['number']}: {pr['title']}")
                count += 1
            else:
                print(f"Failed to close PR #{pr['number']}: {close_result.stderr}")

    return count


def main() -> int:
    print(f"Tearing down test data in {REPO}...")
    print(f"Test label: {TEST_LABEL}")
    print()

    # Check if gh is authenticated
    result = run_gh(["auth", "status"])
    if result.returncode != 0:
        print("Error: gh CLI not authenticated. Run 'gh auth login' first.")
        return 1

    issues_closed = close_test_issues()
    prs_closed = close_test_prs()

    print()
    print(f"Teardown complete: {issues_closed} issues closed, {prs_closed} PRs closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
