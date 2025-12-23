#!/usr/bin/env python3
"""
Create a GitHub issue with standardized naming convention, or validate existing issues.

Naming format: [Mx-nnn] <title> or [Mx-nnn][Px-nnn] <title>
- Mx-nnn: Milestone x, sequence nnn (permanent external name)
- Px-nnn: Priority tier x (0-3), sequence nnn (optional)

Usage:
    # Create issues
    python create_issue.py --agent backend --milestone 6 --title "Fix login bug"
    python create_issue.py --agent mobile --milestone 7 --title "Add dark mode" --priority 1
    python create_issue.py --agent frontend --milestone 6 --title "Update header" --depends-on 123

    # Validate existing issues
    python create_issue.py --validate              # Check all open issues
    python create_issue.py --validate --milestone 6  # Check only M6 issues

    # With repo override
    TEST_REPO=owner/repo python create_issue.py --agent backend --milestone 6 --title "Test"
"""

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Optional


def get_next_sequence(repo: Optional[str], milestone: int, priority: Optional[int] = None) -> tuple[int, int]:
    """Get the next sequence numbers for milestone and priority.

    Returns (milestone_seq, priority_seq) where priority_seq is 0 if no priority.
    """
    cmd = ["gh", "issue", "list", "--state", "all", "--limit", "1000", "--json", "title"]
    if repo:
        cmd.extend(["--repo", repo])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Warning: Could not fetch issues: {result.stderr}", file=sys.stderr)
        return (1, 1 if priority is not None else 0)

    try:
        issues = json.loads(result.stdout)
    except json.JSONDecodeError:
        return (1, 1 if priority is not None else 0)

    # Find max sequence for this milestone
    milestone_pattern = rf'\[M{milestone}-(\d+)\]'
    max_milestone_seq = 0
    for issue in issues:
        match = re.search(milestone_pattern, issue.get("title", ""))
        if match:
            seq = int(match.group(1))
            max_milestone_seq = max(max_milestone_seq, seq)

    # Find max sequence for this priority (if specified)
    max_priority_seq = 0
    if priority is not None:
        priority_pattern = rf'\[P{priority}-(\d+)\]'
        for issue in issues:
            match = re.search(priority_pattern, issue.get("title", ""))
            if match:
                seq = int(match.group(1))
                max_priority_seq = max(max_priority_seq, seq)

    return (max_milestone_seq + 1, max_priority_seq + 1 if priority is not None else 0)


def validate_issues(repo: Optional[str], milestone_filter: Optional[int] = None) -> int:
    """Validate that issues follow naming conventions.

    Checks:
    1. Issues with [Mx-nnn] in title are in GitHub milestone Mx
    2. All issues in milestone Mx have [Mx-nnn] in title

    Returns number of violations found.
    """
    repo = repo or os.environ.get("TEST_REPO")

    cmd = ["gh", "issue", "list", "--state", "open", "--limit", "500", "--json", "number,title,milestone"]
    if repo:
        cmd.extend(["--repo", repo])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error fetching issues: {result.stderr}", file=sys.stderr)
        return 1

    try:
        issues = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"Error parsing issues: {e}", file=sys.stderr)
        return 1

    violations = []

    # Track external names for duplicate detection
    external_names: dict[str, list[int]] = {}  # name -> [issue numbers]

    for issue in issues:
        number = issue["number"]
        title = issue["title"]
        milestone = issue.get("milestone")
        milestone_title = milestone.get("title") if milestone else None

        # Skip E2E test issues
        if "[E2E-TEST]" in title:
            continue

        # Extract [Mx-nnn] from title
        title_match = re.search(r'\[M(\d+)-(\d+)\]', title)
        title_milestone = int(title_match.group(1)) if title_match else None

        # Track external name for duplicate detection
        if title_match:
            external_name = f"[M{title_match.group(1)}-{title_match.group(2)}]"
            if external_name not in external_names:
                external_names[external_name] = []
            external_names[external_name].append(number)

        # Extract milestone number from GitHub milestone (e.g., "M6" -> 6)
        gh_milestone = None
        if milestone_title:
            gh_match = re.match(r'M(\d+)', milestone_title)
            gh_milestone = int(gh_match.group(1)) if gh_match else None

        # Apply milestone filter if specified
        if milestone_filter is not None:
            if title_milestone != milestone_filter and gh_milestone != milestone_filter:
                continue

        # Check 1: Title has [Mx-nnn] but wrong milestone
        if title_milestone is not None and gh_milestone is not None:
            if title_milestone != gh_milestone:
                violations.append(
                    f"#{number}: Title says M{title_milestone} but GitHub milestone is M{gh_milestone}"
                    f"\n    Title: {title}"
                )

        # Check 2: Title has [Mx-nnn] but no GitHub milestone
        if title_milestone is not None and gh_milestone is None:
            violations.append(
                f"#{number}: Title says M{title_milestone} but no GitHub milestone set"
                f"\n    Title: {title}"
            )

        # Check 3: Has GitHub milestone but no [Mx-nnn] in title
        if gh_milestone is not None and title_milestone is None:
            violations.append(
                f"#{number}: In milestone M{gh_milestone} but title missing [M{gh_milestone}-nnn] prefix"
                f"\n    Title: {title}"
            )

        # Check 4: No milestone info at all (warning, not violation)
        if gh_milestone is None and title_milestone is None:
            # Only warn if not filtered
            if milestone_filter is None:
                print(f"  Warning: #{number} has no milestone: {title[:60]}...")

    # Check for duplicate external names
    for ext_name, issue_nums in external_names.items():
        if len(issue_nums) > 1:
            violations.append(
                f"Duplicate external name {ext_name}: issues #{', #'.join(map(str, issue_nums))}"
            )

    if violations:
        print(f"\nFound {len(violations)} naming violations:\n")
        for v in violations:
            print(f"  {v}\n")
        return len(violations)
    else:
        print("All issues follow naming conventions.")
        return 0


def create_issue(
    agent: str,
    milestone: int,
    title: str,
    body: Optional[str] = None,
    priority: Optional[int] = None,
    depends_on: Optional[list[int]] = None,
    repo: Optional[str] = None,
) -> int:
    """Create an issue with standardized naming and return its number."""

    repo = repo or os.environ.get("TEST_REPO")

    # Get next sequence numbers
    milestone_seq, priority_seq = get_next_sequence(repo, milestone, priority)

    # Build the external name (permanent identifier)
    external_name = f"[M{milestone}-{milestone_seq:03d}]"

    # Build full title prefix
    if priority is not None:
        title_prefix = f"{external_name}[P{priority}-{priority_seq:03d}]"
    else:
        title_prefix = external_name

    full_title = f"{title_prefix} {title}"

    # Build body
    body_parts = []

    if depends_on:
        for dep in depends_on:
            body_parts.append(f"Depends-on: #{dep}")
        body_parts.append("")

    if body:
        body_parts.append(body)

    full_body = "\n".join(body_parts) if body_parts else ""

    # Build gh command
    cmd = ["gh", "issue", "create", "--title", full_title, "--body", full_body]

    if repo:
        cmd.extend(["--repo", repo])

    # Required agent label
    agent_label = f"agent:{agent}" if not agent.startswith("agent:") else agent
    cmd.extend(["--label", agent_label])

    # Optional priority label
    if priority is not None:
        priority_labels = {0: "priority:critical", 1: "priority:high", 2: "priority:medium", 3: "priority:low"}
        if priority in priority_labels:
            cmd.extend(["--label", priority_labels[priority]])

    # Milestone (GitHub milestone name, e.g., "M6")
    cmd.extend(["--milestone", f"M{milestone}"])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Error creating issue: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Parse issue URL to get number
    issue_url = result.stdout.strip()
    issue_number = int(issue_url.split("/")[-1])

    print(f"Created issue #{issue_number}: {full_title}")
    print(f"  URL: {issue_url}")
    print(f"  External name: {external_name}")
    print(f"  Agent: {agent_label}")
    if depends_on:
        print(f"  Dependencies: {', '.join(f'#{d}' for d in depends_on)}")

    return issue_number


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a GitHub issue with standardized naming convention, or validate existing issues"
    )

    # Validation mode
    parser.add_argument(
        "--validate", "-v",
        action="store_true",
        help="Validate existing issues instead of creating a new one"
    )

    # Create mode arguments
    parser.add_argument(
        "--agent", "-a",
        help="Agent type (e.g., 'backend', 'frontend', 'mobile') - required for create"
    )
    parser.add_argument(
        "--milestone", "-m",
        type=int,
        help="Milestone number (e.g., 6 for M6) - required for create, optional filter for validate"
    )
    parser.add_argument(
        "--title", "-t",
        help="Issue title (without prefix - will be auto-generated) - required for create"
    )
    parser.add_argument(
        "--body", "-b",
        help="Issue body/description"
    )
    parser.add_argument(
        "--priority", "-p",
        type=int,
        choices=[0, 1, 2, 3],
        help="Priority tier: 0=critical, 1=high, 2=medium, 3=low"
    )
    parser.add_argument(
        "--depends-on", "-d",
        type=int,
        nargs="+",
        help="Issue numbers this depends on (e.g., --depends-on 123 456)"
    )
    parser.add_argument(
        "--repo", "-r",
        help="Repository (owner/repo) - overrides TEST_REPO env var"
    )

    args = parser.parse_args()

    if args.validate:
        # Validation mode
        violations = validate_issues(repo=args.repo, milestone_filter=args.milestone)
        return 1 if violations > 0 else 0
    else:
        # Create mode - validate required args
        if not args.agent:
            parser.error("--agent is required when creating an issue")
        if not args.milestone:
            parser.error("--milestone is required when creating an issue")
        if not args.title:
            parser.error("--title is required when creating an issue")

        create_issue(
            agent=args.agent,
            milestone=args.milestone,
            title=args.title,
            body=args.body,
            priority=args.priority,
            depends_on=args.depends_on,
            repo=args.repo,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
