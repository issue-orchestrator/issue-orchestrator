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
REPO = os.environ.get("TEST_REPO", "BruceBGordon/issue-orchestrator")

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


def cleanup_local_worktrees() -> int:
    """Remove local worktrees created for test issues."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"Error listing worktrees: {result.stderr}")
        return 0

    count = 0
    lines = result.stdout.strip().split("\n")
    worktree_path = None

    for line in lines:
        if line.startswith("worktree "):
            worktree_path = line[9:]  # Remove "worktree " prefix
        elif line.startswith("branch ") and worktree_path:
            branch = line[7:]  # Remove "branch " prefix
            # Check if this looks like a test worktree (issue number 1-10 typically)
            if any(f"-{i}-test" in worktree_path.lower() or
                   f"/{i}-test" in worktree_path.lower()
                   for i in range(1, 20)):
                remove_result = subprocess.run(
                    ["git", "worktree", "remove", "--force", worktree_path],
                    capture_output=True, text=True
                )
                if remove_result.returncode == 0:
                    print(f"Removed worktree: {worktree_path}")
                    count += 1
                else:
                    print(f"Failed to remove worktree {worktree_path}: {remove_result.stderr}")
            worktree_path = None

    return count


def cleanup_local_branches() -> int:
    """Remove local branches created for test issues."""
    result = subprocess.run(
        ["git", "branch", "--list"],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"Error listing branches: {result.stderr}")
        return 0

    count = 0
    for line in result.stdout.strip().split("\n"):
        branch = line.strip().lstrip("* ")
        # Check if this looks like a test branch (starts with small number)
        if branch and any(branch.startswith(f"{i}-test") for i in range(1, 20)):
            delete_result = subprocess.run(
                ["git", "branch", "-D", branch],
                capture_output=True, text=True
            )
            if delete_result.returncode == 0:
                print(f"Deleted branch: {branch}")
                count += 1
            else:
                print(f"Failed to delete branch {branch}: {delete_result.stderr}")

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

    # GitHub cleanup
    issues_closed = close_test_issues()
    prs_closed = close_test_prs()

    # Local cleanup
    print()
    print("Cleaning up local git artifacts...")
    worktrees_removed = cleanup_local_worktrees()
    branches_deleted = cleanup_local_branches()

    print()
    print(f"Teardown complete:")
    print(f"  GitHub: {issues_closed} issues closed, {prs_closed} PRs closed")
    print(f"  Local:  {worktrees_removed} worktrees removed, {branches_deleted} branches deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
