#!/usr/bin/env python3
"""
Issue management tool with standardized naming conventions.

Naming format: [Mx-nnn] <title> or [Mx-nnn][Px-nnn] <title>
- Mx-nnn: Milestone x, sequence nnn (permanent external name)
- Px-nnn: Priority tier x (0-3), sequence nnn (optional)

Commands:
    validate    Check issues follow naming conventions
    fix         Add [Mx-nnn] prefix to issues in milestones
    create      Create a new issue with proper naming

Usage:
    python scripts/issues.py validate
    python scripts/issues.py validate --milestone 6
    python scripts/issues.py validate --repo owner/repo

    python scripts/issues.py fix                    # Dry run
    python scripts/issues.py fix --apply            # Actually rename
    python scripts/issues.py fix --milestone 10     # Only M10

    python scripts/issues.py create --agent backend --milestone 6 --title "Fix bug"
    python scripts/issues.py create --agent mobile --milestone 7 --title "Feature" --priority 1
"""

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Optional


def get_next_sequence(repo: Optional[str], milestone: int, priority: Optional[int] = None) -> tuple[int, int]:
    """Get the next sequence numbers for milestone and priority."""
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


# =============================================================================
# Fix Command
# =============================================================================

def cmd_fix(args) -> int:
    """Fix issue names by adding [Mx-nnn] prefix for issues in milestones."""
    repo = args.repo or os.environ.get("TEST_REPO")
    dry_run = not args.apply

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

    # Group issues by milestone that need fixing
    issues_to_fix: dict[int, list[dict]] = {}

    for issue in issues:
        title = issue["title"]
        milestone = issue.get("milestone")
        milestone_title = milestone.get("title") if milestone else None

        if not milestone_title:
            continue

        gh_match = re.match(r'M(\d+)', milestone_title)
        if not gh_match:
            continue
        gh_milestone = int(gh_match.group(1))

        if args.milestone is not None and gh_milestone != args.milestone:
            continue

        if re.search(r'\[M\d+-\d+\]', title):
            continue

        if "[E2E-TEST]" in title:
            continue

        if gh_milestone not in issues_to_fix:
            issues_to_fix[gh_milestone] = []
        issues_to_fix[gh_milestone].append(issue)

    if not issues_to_fix:
        print("No issues need fixing.")
        return 0

    # Get existing sequence numbers
    existing_seqs: dict[int, int] = {}
    for issue in issues:
        title = issue["title"]
        match = re.search(r'\[M(\d+)-(\d+)\]', title)
        if match:
            m_num = int(match.group(1))
            seq = int(match.group(2))
            existing_seqs[m_num] = max(existing_seqs.get(m_num, 0), seq)

    fixed_count = 0
    for milestone_num in sorted(issues_to_fix.keys()):
        issues_list = issues_to_fix[milestone_num]
        next_seq = existing_seqs.get(milestone_num, 0) + 1

        for issue in issues_list:
            number = issue["number"]
            old_title = issue["title"]
            new_title = f"[M{milestone_num}-{next_seq:03d}] {old_title}"

            if dry_run:
                print(f"  Would rename #{number}:")
                print(f"    Old: {old_title}")
                print(f"    New: {new_title}")
            else:
                rename_cmd = ["gh", "issue", "edit", str(number), "--title", new_title]
                if repo:
                    rename_cmd.extend(["--repo", repo])
                rename_result = subprocess.run(rename_cmd, capture_output=True, text=True)
                if rename_result.returncode == 0:
                    print(f"  Renamed #{number}: {new_title}")
                else:
                    print(f"  Failed to rename #{number}: {rename_result.stderr}", file=sys.stderr)
                    continue

            next_seq += 1
            fixed_count += 1

    if dry_run:
        print(f"\nDry run: {fixed_count} issues would be renamed. Use --apply to apply changes.")
    else:
        print(f"\nFixed {fixed_count} issues.")

    return 0


# =============================================================================
# Validate Command
# =============================================================================

def cmd_validate(args) -> int:
    """Validate that issues follow naming conventions."""
    repo = args.repo or os.environ.get("TEST_REPO")

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
    external_names: dict[str, list[int]] = {}

    for issue in issues:
        number = issue["number"]
        title = issue["title"]
        milestone = issue.get("milestone")
        milestone_title = milestone.get("title") if milestone else None

        if "[E2E-TEST]" in title:
            continue

        title_match = re.search(r'\[M(\d+)-(\d+)\]', title)
        title_milestone = int(title_match.group(1)) if title_match else None

        if title_match:
            external_name = f"[M{title_match.group(1)}-{title_match.group(2)}]"
            if external_name not in external_names:
                external_names[external_name] = []
            external_names[external_name].append(number)

        gh_milestone = None
        if milestone_title:
            gh_match = re.match(r'M(\d+)', milestone_title)
            gh_milestone = int(gh_match.group(1)) if gh_match else None

        if args.milestone is not None:
            if title_milestone != args.milestone and gh_milestone != args.milestone:
                continue

        if title_milestone is not None and gh_milestone is not None:
            if title_milestone != gh_milestone:
                violations.append(
                    f"#{number}: Title says M{title_milestone} but GitHub milestone is M{gh_milestone}"
                    f"\n    Title: {title}"
                )

        if title_milestone is not None and gh_milestone is None:
            violations.append(
                f"#{number}: Title says M{title_milestone} but no GitHub milestone set"
                f"\n    Title: {title}"
            )

        if gh_milestone is not None and title_milestone is None:
            violations.append(
                f"#{number}: In milestone M{gh_milestone} but title missing [M{gh_milestone}-nnn] prefix"
                f"\n    Title: {title}"
            )

        if gh_milestone is None and title_milestone is None:
            if args.milestone is None:
                print(f"  Warning: #{number} has no milestone: {title[:60]}...")

    # Check for duplicates
    for ext_name, issue_nums in external_names.items():
        if len(issue_nums) > 1:
            violations.append(
                f"Duplicate external name {ext_name}: issues #{', #'.join(map(str, issue_nums))}"
            )

    if violations:
        print(f"\nFound {len(violations)} naming violations:\n")
        for v in violations:
            print(f"  {v}\n")
        return 1
    else:
        print("All issues follow naming conventions.")
        return 0


# =============================================================================
# Create Command
# =============================================================================

def cmd_create(args) -> int:
    """Create a new issue with standardized naming."""
    repo = args.repo or os.environ.get("TEST_REPO")

    milestone_seq, priority_seq = get_next_sequence(repo, args.milestone, args.priority)

    external_name = f"[M{args.milestone}-{milestone_seq:03d}]"

    if args.priority is not None:
        title_prefix = f"{external_name}[P{args.priority}-{priority_seq:03d}]"
    else:
        title_prefix = external_name

    full_title = f"{title_prefix} {args.title}"

    # Build body
    body_parts = []
    if args.depends_on:
        for dep in args.depends_on:
            body_parts.append(f"Depends-on: #{dep}")
        body_parts.append("")
    if args.body:
        body_parts.append(args.body)

    full_body = "\n".join(body_parts) if body_parts else ""

    # Build gh command
    cmd = ["gh", "issue", "create", "--title", full_title, "--body", full_body]

    if repo:
        cmd.extend(["--repo", repo])

    agent_label = f"agent:{args.agent}" if not args.agent.startswith("agent:") else args.agent
    cmd.extend(["--label", agent_label])

    if args.priority is not None:
        priority_labels = {0: "priority:critical", 1: "priority:high", 2: "priority:medium", 3: "priority:low"}
        if args.priority in priority_labels:
            cmd.extend(["--label", priority_labels[args.priority]])

    cmd.extend(["--milestone", f"M{args.milestone}"])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Error creating issue: {result.stderr}", file=sys.stderr)
        return 1

    issue_url = result.stdout.strip()
    issue_number = int(issue_url.split("/")[-1])

    print(f"Created issue #{issue_number}: {full_title}")
    print(f"  URL: {issue_url}")
    print(f"  External name: {external_name}")
    print(f"  Agent: {agent_label}")
    if args.depends_on:
        print(f"  Dependencies: {', '.join(f'#{d}' for d in args.depends_on)}")

    return 0


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Issue management tool with standardized naming conventions"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Common arguments
    repo_help = "Repository (owner/repo) - overrides TEST_REPO env var"
    milestone_help = "Filter by milestone number (e.g., 6 for M6)"

    # Validate command
    validate_parser = subparsers.add_parser("validate", help="Check issues follow naming conventions")
    validate_parser.add_argument("--repo", "-r", help=repo_help)
    validate_parser.add_argument("--milestone", "-m", type=int, help=milestone_help)

    # Fix command
    fix_parser = subparsers.add_parser("fix", help="Add [Mx-nnn] prefix to issues in milestones")
    fix_parser.add_argument("--repo", "-r", help=repo_help)
    fix_parser.add_argument("--milestone", "-m", type=int, help=milestone_help)
    fix_parser.add_argument("--apply", action="store_true", help="Actually apply changes (default is dry run)")

    # Create command
    create_parser = subparsers.add_parser("create", help="Create a new issue with proper naming")
    create_parser.add_argument("--repo", "-r", help=repo_help)
    create_parser.add_argument("--agent", "-a", required=True, help="Agent type (e.g., 'backend', 'mobile')")
    create_parser.add_argument("--milestone", "-m", type=int, required=True, help="Milestone number")
    create_parser.add_argument("--title", "-t", required=True, help="Issue title (prefix auto-generated)")
    create_parser.add_argument("--body", "-b", help="Issue body/description")
    create_parser.add_argument("--priority", "-p", type=int, choices=[0, 1, 2, 3],
                               help="Priority: 0=critical, 1=high, 2=medium, 3=low")
    create_parser.add_argument("--depends-on", "-d", type=int, nargs="+",
                               help="Issue numbers this depends on")

    args = parser.parse_args()

    if args.command == "validate":
        return cmd_validate(args)
    elif args.command == "fix":
        return cmd_fix(args)
    elif args.command == "create":
        return cmd_create(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
