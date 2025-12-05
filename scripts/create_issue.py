#!/usr/bin/env python3
"""
Create a GitHub issue with required agent label.

Usage:
    python create_issue.py --agent backend --title "Fix login bug" --body "Details..."
    python create_issue.py --agent mobile --title "Add dark mode" --priority high
    python create_issue.py --agent frontend --title "Update header" --milestone M6

    # With repo override
    TEST_REPO=owner/repo python create_issue.py --agent backend --title "Test issue"
"""

import argparse
import os
import subprocess
import sys
from typing import Optional


def slugify(text: str, max_length: int = 50) -> str:
    """Convert text to a URL/branch-friendly slug."""
    import re
    # Lowercase and replace spaces/special chars with hyphens
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower())
    # Remove leading/trailing hyphens
    slug = slug.strip('-')
    # Truncate
    return slug[:max_length]


def create_issue(
    agent: str,
    title: str,
    body: Optional[str] = None,
    priority: Optional[str] = None,
    milestone: Optional[str] = None,
    repo: Optional[str] = None,
) -> int:
    """Create an issue and return its number."""

    repo = repo or os.environ.get("TEST_REPO")

    cmd = ["gh", "issue", "create", "--title", title]

    if repo:
        cmd.extend(["--repo", repo])

    # Required agent label
    agent_label = f"agent:{agent}" if not agent.startswith("agent:") else agent
    cmd.extend(["--label", agent_label])

    # Optional priority
    if priority:
        priority_label = f"priority:{priority}" if not priority.startswith("priority:") else priority
        cmd.extend(["--label", priority_label])

    # Optional milestone
    if milestone:
        cmd.extend(["--milestone", milestone])

    # Body
    if body:
        cmd.extend(["--body", body])
    else:
        cmd.extend(["--body", f"Created for {agent_label} agent."])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Error creating issue: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Parse issue URL to get number
    issue_url = result.stdout.strip()
    issue_number = int(issue_url.split("/")[-1])

    # Suggest branch name
    branch_name = f"fix/issue-{issue_number}-{slugify(title)}"

    print(f"Created issue #{issue_number}: {title}")
    print(f"  URL: {issue_url}")
    print(f"  Agent: {agent_label}")
    print(f"  Suggested branch: {branch_name}")

    return issue_number


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a GitHub issue with required agent label"
    )
    parser.add_argument(
        "--agent", "-a",
        required=True,
        help="Agent type (e.g., 'backend', 'frontend', 'mobile') - will prefix with 'agent:' if needed"
    )
    parser.add_argument(
        "--title", "-t",
        required=True,
        help="Issue title"
    )
    parser.add_argument(
        "--body", "-b",
        help="Issue body/description"
    )
    parser.add_argument(
        "--priority", "-p",
        choices=["high", "medium", "low"],
        help="Priority level"
    )
    parser.add_argument(
        "--milestone", "-m",
        help="Milestone (e.g., M6, M7)"
    )
    parser.add_argument(
        "--repo", "-r",
        help="Repository (owner/repo) - overrides TEST_REPO env var"
    )

    args = parser.parse_args()

    create_issue(
        agent=args.agent,
        title=args.title,
        body=args.body,
        priority=args.priority,
        milestone=args.milestone,
        repo=args.repo,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
