#!/usr/bin/env python3
"""
Create test issues for integration testing.

Usage:
    python setup_test_issues.py

Creates test issues in the issue-orchestrator repo with 'test-data' label.
These issues are isolated from real issues and used for testing the orchestrator.
"""

import json
import os
import subprocess
import sys

# Default to issue-orchestrator repo, override with env var if needed
REPO = os.environ.get("TEST_REPO", "BruceBGordon/issue-orchestrator")

# Label that marks test data
TEST_LABEL = "test-data"

# Test issues to create
TEST_ISSUES = [
    {
        "title": "[TEST] Simple backend task",
        "body": "This is a test issue for the orchestrator.\n\nExpected behavior: Agent completes successfully.",
        "labels": ["agent:backend", "priority:high"],
    },
    {
        "title": "[TEST] Frontend feature",
        "body": "Test frontend task.\n\nExpected behavior: Agent completes successfully.",
        "labels": ["agent:frontend", "priority:medium"],
    },
    {
        "title": "[TEST] Mobile bug fix",
        "body": "Test mobile task.\n\nExpected behavior: Agent completes successfully.",
        "labels": ["agent:mobile", "priority:low"],
    },
    {
        "title": "[TEST] Task that will block",
        "body": "This task should simulate a blocked state.\n\nExpected behavior: Agent marks as blocked.",
        "labels": ["agent:backend"],
    },
    {
        "title": "[TEST] Task with dependency",
        "body": "Blocked by #FIRST_ISSUE (will be updated after creation).\n\nExpected behavior: Orchestrator detects dependency.",
        "labels": ["agent:backend"],
    },
]


def run_gh(args: list[str]) -> subprocess.CompletedProcess:
    """Run gh command and return result."""
    return subprocess.run(["gh"] + args, capture_output=True, text=True)


def create_label_if_missing(label: str) -> None:
    """Create a label if it doesn't exist."""
    result = run_gh(["label", "list", "--repo", REPO, "--json", "name"])
    if result.returncode != 0:
        print(f"Warning: Could not list labels: {result.stderr}")
        return

    existing = {l["name"] for l in json.loads(result.stdout)}

    if label not in existing:
        print(f"Creating label: {label}")
        run_gh(["label", "create", label, "--repo", REPO, "--force",
                "--description", "Test data for integration tests"])


def create_issue(issue_def: dict) -> int:
    """Create an issue and return its number."""
    # Build labels list - always include test-data label
    labels = [TEST_LABEL] + issue_def.get("labels", [])

    # Ensure all labels exist
    for label in labels:
        create_label_if_missing(label)

    cmd = [
        "issue", "create",
        "--repo", REPO,
        "--title", issue_def["title"],
        "--body", issue_def["body"],
    ]
    for label in labels:
        cmd.extend(["--label", label])

    result = run_gh(cmd)

    if result.returncode != 0:
        print(f"Error creating issue: {result.stderr}")
        return -1

    # Output is like "https://github.com/owner/repo/issues/123"
    issue_url = result.stdout.strip()
    issue_number = int(issue_url.split("/")[-1])
    print(f"Created issue #{issue_number}: {issue_def['title']}")
    return issue_number


def main() -> int:
    print(f"Setting up test issues in {REPO}...")
    print(f"Test label: {TEST_LABEL}")
    print()

    # Check if gh is authenticated
    result = run_gh(["auth", "status"])
    if result.returncode != 0:
        print("Error: gh CLI not authenticated. Run 'gh auth login' first.")
        return 1

    created_numbers = []
    for issue_def in TEST_ISSUES:
        number = create_issue(issue_def)
        if number > 0:
            created_numbers.append(number)

    # Update the dependency issue to reference the first created issue
    if len(created_numbers) >= 5:
        first_issue = created_numbers[0]
        dep_issue = created_numbers[4]
        run_gh([
            "issue", "edit", str(dep_issue),
            "--repo", REPO,
            "--body", f"Blocked by #{first_issue}\n\nExpected behavior: Orchestrator detects dependency."
        ])
        print(f"Updated issue #{dep_issue} to depend on #{first_issue}")

    print()
    print(f"Created {len(created_numbers)} test issues: {created_numbers}")
    print(f"View at: https://github.com/{REPO}/issues?q=label%3A{TEST_LABEL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
