"""Agent completion CLI - enforces structured status reporting.

This command is the ONLY sanctioned way for agents to complete their work.
It ensures all required fields are provided and formats comments correctly.
"""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import NoReturn, Optional


class Status(Enum):
    """Allowed completion statuses. No others accepted."""
    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"


# Required fields per status - agents MUST provide these
REQUIRED_FIELDS = {
    Status.COMPLETED: ["implementation", "problems"],
    Status.BLOCKED: ["reason", "attempted"],
    Status.NEEDS_HUMAN: ["question"],
}


@dataclass
class CompletionData:
    """Validated completion data."""
    status: Status
    # Completion fields
    implementation: str | None = None
    problems: str | None = None
    # Blocked fields
    reason: str | None = None
    attempted: str | None = None
    blocked_by: list[int] | None = None
    # Needs human fields
    question: str | None = None
    context: str | None = None
    options: list[str] | None = None
    default_action: str | None = None


def die(message: str) -> NoReturn:
    """Print error and exit with failure."""
    print(f"❌ ERROR: {message}", file=sys.stderr)
    print("\nUse --help for usage information.", file=sys.stderr)
    sys.exit(1)


def get_issue_number() -> int:
    """Extract issue number from current branch name."""
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        die("Not in a git repository or no branch checked out")

    branch = result.stdout.strip()
    match = re.match(r"^(\d+)-", branch)
    if not match:
        die(f"Branch '{branch}' doesn't match issue branch pattern (e.g., '123-fix-bug')")

    return int(match.group(1))


def get_repo() -> str:
    """Get owner/repo from git remote."""
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        die("Could not determine repository. Is 'gh' authenticated?")
    return result.stdout.strip()


def get_review_label() -> Optional[str]:
    """Try to load review label from orchestrator config."""
    try:
        from .config import Config
        config = Config.find_and_load()
        return config.review_label
    except Exception:
        # Config not found or error loading - that's fine, review is optional
        return None


def add_label_to_pr(pr_url: str, label: str) -> None:
    """Add a label to a PR."""
    # Extract PR number from URL
    match = re.search(r"/pull/(\d+)", pr_url)
    if not match:
        print(f"⚠️  Could not extract PR number from {pr_url}", file=sys.stderr)
        return

    pr_number = match.group(1)
    result = subprocess.run(
        ["gh", "pr", "edit", pr_number, "--add-label", label],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"⚠️  Could not add label '{label}' to PR: {result.stderr}", file=sys.stderr)
    else:
        print(f"🏷️  Added '{label}' label to PR")


def validate_fields(data: CompletionData) -> None:
    """Validate all required fields are present for the status."""
    required = REQUIRED_FIELDS[data.status]
    missing = []

    for field in required:
        value = getattr(data, field)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(f"--{field.replace('_', '-')}")

    if missing:
        die(f"Status '{data.status.value}' requires: {', '.join(missing)}")


def format_completion_comment(data: CompletionData) -> str:
    """Format a completion comment with all sections."""
    return f"""## Implementation

{data.implementation}

## Problems Encountered

{data.problems}

## Pull Request

<PR_LINK_PLACEHOLDER>"""


def format_blocked_comment(data: CompletionData) -> str:
    """Format a blocked comment."""
    blocked_by = ""
    if data.blocked_by:
        refs = ", ".join(f"#{n}" for n in data.blocked_by)
        blocked_by = f"\n**Blocked by:** {refs}"

    return f"""## Blocked

**Reason:** {data.reason}{blocked_by}
**Attempted:** {data.attempted}
**Unblock action:** {data.reason}"""


def format_needs_human_comment(data: CompletionData) -> str:
    """Format a needs-human comment."""
    parts = [f"## Needs Human Input\n\n**Question:** {data.question}"]

    if data.context:
        parts.append(f"**Context:** {data.context}")

    if data.options:
        parts.append("**Options:**")
        for i, opt in enumerate(data.options, 1):
            parts.append(f"{i}. {opt}")

    if data.default_action:
        parts.append(f"**Default if no response:** {data.default_action}")

    return "\n".join(parts)


def post_comment(repo: str, issue_number: int, body: str) -> str:
    """Post a comment to the issue. Returns comment URL."""
    result = subprocess.run(
        ["gh", "issue", "comment", str(issue_number), "--body", body],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        die(f"Failed to post comment: {result.stderr}")

    # gh issue comment outputs the URL
    return result.stdout.strip()


def add_label(repo: str, issue_number: int, label: str) -> None:
    """Add a label to the issue."""
    result = subprocess.run(
        ["gh", "issue", "edit", str(issue_number), "--add-label", label],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"⚠️  Warning: Could not add label '{label}': {result.stderr}", file=sys.stderr)


def add_trailers_to_commit(data: CompletionData) -> None:
    """Add structured trailers to the last commit via amend."""
    # Build trailer arguments
    trailers = [f"Agent-Status: {data.status.value}"]

    if data.status == Status.COMPLETED:
        trailers.append(f"Agent-Implementation: {data.implementation}")
        trailers.append(f"Agent-Problems: {data.problems}")
    elif data.status == Status.BLOCKED:
        trailers.append(f"Agent-Reason: {data.reason}")
        trailers.append(f"Agent-Attempted: {data.attempted}")
        if data.blocked_by:
            trailers.append(f"Agent-Blocked-By: {','.join(str(n) for n in data.blocked_by)}")
    else:  # NEEDS_HUMAN
        trailers.append(f"Agent-Question: {data.question}")
        if data.context:
            trailers.append(f"Agent-Context: {data.context}")

    # Get current commit message
    result = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        die("Failed to get current commit message")

    original_msg = result.stdout.strip()

    # Check if trailers already exist (avoid duplicates)
    if "Agent-Status:" in original_msg:
        print("⚠️  Trailers already present in commit, skipping amend")
        return

    # Build new message with trailers
    # Trailers go at the end, separated by blank line
    trailer_block = "\n".join(trailers)
    new_msg = f"{original_msg}\n\n{trailer_block}"

    # Amend the commit with new message
    result = subprocess.run(
        ["git", "commit", "--amend", "-m", new_msg],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        die(f"Failed to amend commit with trailers: {result.stderr}")

    print("📝 Added structured trailers to commit")


def git_push() -> None:
    """Push current branch to origin."""
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True
    ).stdout.strip()

    result = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        die(f"Failed to push: {result.stderr}")

    print(f"✅ Pushed branch '{branch}' to origin")


def create_pr(issue_number: int, title: str, data: CompletionData) -> str:
    """Create a PR with structured content and return its URL."""
    # Build rich PR body with context for reviewers
    body_parts = [
        f"Closes #{issue_number}",
        "",
        "## Summary",
        "",
        data.implementation or "No implementation summary provided.",
        "",
    ]

    # Add problems section if there were any
    if data.problems and data.problems.lower() not in ("none", "n/a", "no problems"):
        body_parts.extend([
            "## Problems Encountered",
            "",
            data.problems,
            "",
        ])

    # Add notes for reviewer
    body_parts.extend([
        "---",
        "*Generated by issue-orchestrator agent*",
    ])

    body = "\n".join(body_parts)

    result = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        # PR might already exist
        if "already exists" in result.stderr:
            # Get existing PR URL
            pr_result = subprocess.run(
                ["gh", "pr", "view", "--json", "url", "-q", ".url"],
                capture_output=True, text=True
            )
            if pr_result.returncode == 0:
                return pr_result.stdout.strip()
        die(f"Failed to create PR: {result.stderr}")

    return result.stdout.strip()


def update_comment_with_pr(repo: str, comment_url: str, pr_url: str, original_body: str) -> None:
    """Update the comment to include the PR URL."""
    # The comment URL format is https://github.com/owner/repo/issues/N#issuecomment-ID
    # We need to extract the comment ID
    match = re.search(r"#issuecomment-(\d+)", comment_url)
    if not match:
        print("⚠️  Could not update comment with PR URL", file=sys.stderr)
        return

    # Replace placeholder with actual PR URL
    new_body = original_body.replace("<PR_LINK_PLACEHOLDER>", pr_url)

    # Unfortunately gh doesn't have a direct way to edit a comment
    # We'd need to use the API directly. For now, just print a note.
    print(f"📝 PR created: {pr_url}")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Complete agent work with structured status reporting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES:
  Completed successfully:
    agent-done completed --implementation "Added user auth" --problems "None"
    agent-done completed --implementation "Fixed bug in X" --problems "Found flaky test in Y"

  Blocked:
    agent-done blocked --reason "Need API credentials" --attempted "Checked env vars and secrets"
    agent-done blocked --reason "Depends on #123" --attempted "..." --blocked-by 123

  Need human input:
    agent-done needs_human --question "Should we use OAuth or API keys?"
    agent-done needs_human --question "Which approach?" --options "Use Redis" "Use Postgres" --default "Use Redis"

STATUSES:
  completed   - Work done, PR ready (requires: --implementation, --problems)
  blocked     - Cannot proceed (requires: --reason, --attempted)
  needs_human - Need decision/clarification (requires: --question)
"""
    )

    # Positional: status (required, validated)
    parser.add_argument(
        "status",
        choices=["completed", "blocked", "needs_human"],
        help="Completion status (only these values allowed)"
    )

    # Completion fields
    parser.add_argument(
        "--implementation", "-i",
        help="What was implemented (required for 'completed')"
    )
    parser.add_argument(
        "--problems", "-p",
        help="Problems encountered during work (required for 'completed', use 'None' if none)"
    )

    # Blocked fields
    parser.add_argument(
        "--reason", "-r",
        help="Why blocked (required for 'blocked')"
    )
    parser.add_argument(
        "--attempted", "-a",
        help="What was attempted (required for 'blocked')"
    )
    parser.add_argument(
        "--blocked-by", "-b",
        type=int, nargs="+",
        help="Issue numbers this is blocked by (optional, for 'blocked')"
    )

    # Needs human fields
    parser.add_argument(
        "--question", "-q",
        help="Question for human (required for 'needs_human')"
    )
    parser.add_argument(
        "--context", "-c",
        help="Context for the question (optional, for 'needs_human')"
    )
    parser.add_argument(
        "--options", "-o",
        nargs="+",
        help="Available options (optional, for 'needs_human')"
    )
    parser.add_argument(
        "--default",
        help="Default action if no response (optional, for 'needs_human')"
    )

    # Meta options
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without doing it"
    )

    args = parser.parse_args()

    # Parse status (already validated by argparse choices)
    status = Status(args.status)

    # Build completion data
    data = CompletionData(
        status=status,
        implementation=args.implementation,
        problems=args.problems,
        reason=args.reason,
        attempted=args.attempted,
        blocked_by=args.blocked_by,
        question=args.question,
        context=args.context,
        options=args.options,
        default_action=args.default,
    )

    # Validate required fields (strict!)
    validate_fields(data)

    # Get context
    issue_number = get_issue_number()
    repo = get_repo()

    print(f"📋 Issue: #{issue_number} ({repo})")
    print(f"📊 Status: {status.value}")

    # Format comment based on status
    if status == Status.COMPLETED:
        comment_body = format_completion_comment(data)
    elif status == Status.BLOCKED:
        comment_body = format_blocked_comment(data)
    else:  # NEEDS_HUMAN
        comment_body = format_needs_human_comment(data)

    if args.dry_run:
        print("\n--- DRY RUN: Would post this comment ---")
        print(comment_body)
        print("--- END ---")
        return

    # Execute the workflow
    # First, add trailers to commit (for all statuses)
    print("\n📝 Adding trailers to commit...")
    add_trailers_to_commit(data)

    if status == Status.COMPLETED:
        # 1. Push code (pre-push hook validates trailers)
        print("🚀 Pushing code...")
        git_push()

        # 2. Create PR with structured content
        print("📝 Creating PR...")
        pr_title = f"Fix #{issue_number}"  # TODO: get from branch/issue
        pr_url = create_pr(issue_number, pr_title, data)

        # 3. Add review label if configured
        review_label = get_review_label()
        if review_label:
            add_label_to_pr(pr_url, review_label)

        # 4. Update comment with PR URL and post
        comment_body = comment_body.replace("<PR_LINK_PLACEHOLDER>", pr_url)
        print("💬 Posting completion comment...")
        post_comment(repo, issue_number, comment_body)

        print(f"\n✅ COMPLETED: PR created at {pr_url}")

    elif status == Status.BLOCKED:
        # 1. Push any work done (trailers included)
        print("🚀 Pushing work so far...")
        git_push()

        # 2. Add blocked label
        print("🏷️  Adding 'blocked' label...")
        add_label(repo, issue_number, "blocked")

        # 3. Post comment
        print("💬 Posting blocked comment...")
        post_comment(repo, issue_number, comment_body)

        print(f"\n🚧 BLOCKED: Issue #{issue_number} marked as blocked")

    else:  # NEEDS_HUMAN
        # 1. Push any work done (trailers included)
        print("🚀 Pushing work so far...")
        git_push()

        # 2. Add needs-human label
        print("🏷️  Adding 'needs-human' label...")
        add_label(repo, issue_number, "needs-human")

        # 3. Post comment
        print("💬 Posting question comment...")
        post_comment(repo, issue_number, comment_body)

        print(f"\n❓ NEEDS HUMAN: Question posted on issue #{issue_number}")


if __name__ == "__main__":
    main()
