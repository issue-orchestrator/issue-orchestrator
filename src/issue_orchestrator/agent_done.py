"""Agent completion CLI - enforces structured status reporting.

This command is the ONLY sanctioned way for agents to complete their work.
It ensures all required fields are provided and formats comments correctly.
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import NoReturn, Optional

# Secret for PR verification - agents don't know this
# Set via ORCHESTRATOR_PR_SECRET env var or use default
PR_VERIFICATION_SECRET = os.environ.get("ORCHESTRATOR_PR_SECRET", "orchestrator-2024-verified")


class Status(Enum):
    """Allowed completion statuses. No others accepted."""
    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    # Reviewer statuses
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


# Required fields per status - agents MUST provide these
REQUIRED_FIELDS = {
    Status.COMPLETED: ["implementation", "problems"],
    Status.BLOCKED: ["reason", "attempted"],
    Status.NEEDS_HUMAN: ["question"],
    Status.APPROVED: ["summary"],
    Status.CHANGES_REQUESTED: ["issues"],
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
    when_unblocked: str | None = None  # Hint for future agent when blocker is resolved
    # Needs human fields
    question: str | None = None
    context: str | None = None
    options: list[str] | None = None
    default_action: str | None = None
    # Reviewer fields
    summary: str | None = None  # For approved
    issues: str | None = None   # For changes_requested


def die(message: str) -> NoReturn:
    """Print error and exit with failure."""
    print(f"❌ ERROR: {message}", file=sys.stderr)
    print("\nUse --help for usage information.", file=sys.stderr)
    sys.exit(1)


def generate_pr_verification_token(issue_number: int) -> str:
    """Generate a verification token that proves PR was created via agent-done.

    This token is a hash of the issue number + secret. Agents can't forge this
    because they don't know the secret. The orchestrator can verify PRs by
    checking for this token.
    """
    data = f"{issue_number}:{PR_VERIFICATION_SECRET}"
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def get_pr_verification_marker(issue_number: int) -> str:
    """Get the HTML comment marker to embed in PR body."""
    token = generate_pr_verification_token(issue_number)
    return f"<!-- orchestrator-verified:{token} -->"


def verify_pr_token(pr_body: str, issue_number: int) -> bool:
    """Verify that a PR was created via agent-done by checking for valid token.

    Args:
        pr_body: The PR body text to check
        issue_number: The issue number to verify against

    Returns:
        True if PR has valid verification token, False otherwise
    """
    expected_marker = get_pr_verification_marker(issue_number)
    return expected_marker in pr_body


def extract_pr_verification_status(pr_body: str) -> tuple[bool, str | None]:
    """Extract verification status from PR body.

    Returns:
        Tuple of (has_marker, token_if_found)
    """
    import re
    match = re.search(r'<!-- orchestrator-verified:([a-f0-9]+) -->', pr_body)
    if match:
        return True, match.group(1)
    return False, None


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


def get_issue_title(issue_number: int) -> str:
    """Fetch the issue title from GitHub."""
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--json", "title", "-q", ".title"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        # Fallback to just the issue number if we can't fetch
        return f"Issue #{issue_number}"
    return result.stdout.strip()


def find_config_from_worktree() -> Optional[Path]:
    """Find orchestrator config, handling worktrees that are siblings to main repo.

    Worktrees are typically at /repo-parent/repo-N/ while config is at /repo-parent/repo/.
    This function checks if we're in a worktree and looks for config in the main repo.
    """
    cwd = Path.cwd()

    # First, try standard parent directory search
    for path in [cwd, *cwd.parents]:
        config_file = path / ".issue-orchestrator.yaml"
        if config_file.exists():
            return config_file

    # Check if we're in a git worktree (has .git file, not .git directory)
    git_path = cwd / ".git"
    if git_path.is_file():
        # .git file contains: gitdir: /path/to/main/repo/.git/worktrees/name
        content = git_path.read_text().strip()
        if content.startswith("gitdir:"):
            gitdir = Path(content.split(":", 1)[1].strip())
            # Navigate from /repo/.git/worktrees/name to /repo
            main_repo = gitdir.parent.parent.parent
            config_file = main_repo / ".issue-orchestrator.yaml"
            if config_file.exists():
                return config_file

    return None


def get_code_review_label() -> Optional[str]:
    """Try to load code review label from orchestrator config."""
    try:
        config_path = find_config_from_worktree()
        if not config_path:
            return None

        from .config import Config
        config = Config.load(config_path)
        # Only return label if code review agent is configured
        if config.code_review_agent:
            return config.code_review_label
        return None
    except Exception:
        # Config not found or error loading - that's fine, review is optional
        return None


def get_blocked_label() -> str:
    """Get the blocked label from config (with prefix if configured)."""
    try:
        config_path = find_config_from_worktree()
        if not config_path:
            return "blocked"
        from .config import Config
        config = Config.load(config_path)
        return config.get_label_blocked()
    except Exception:
        # Config not found - use default
        return "blocked"


def get_needs_human_label() -> str:
    """Get the needs-human label from config (with prefix if configured)."""
    try:
        config_path = find_config_from_worktree()
        if not config_path:
            return "needs-human"
        from .config import Config
        config = Config.load(config_path)
        return config.get_label_needs_human()
    except Exception:
        # Config not found - use default
        return "needs-human"


def get_needs_rework_label() -> str:
    """Get the needs-rework label from config (with prefix if configured)."""
    try:
        config_path = find_config_from_worktree()
        if not config_path:
            return "needs-rework"
        from .config import Config
        config = Config.load(config_path)
        return config.get_label_needs_rework()
    except Exception:
        return "needs-rework"


def get_code_reviewed_label() -> Optional[str]:
    """Get the code-reviewed label from config."""
    try:
        config_path = find_config_from_worktree()
        if not config_path:
            return None
        from .config import Config
        config = Config.load(config_path)
        return config.code_reviewed_label
    except Exception:
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


def remove_label_from_pr(pr_number: int, label: str) -> None:
    """Remove a label from a PR."""
    print(f"🏷️  Attempting to remove '{label}' from PR #{pr_number}...")
    # Set auth env var to bypass gh-wrapper block
    env = os.environ.copy()
    env["ORCHESTRATOR_GH_AUTH"] = "agent-done-authorized"
    result = subprocess.run(
        ["gh", "pr", "edit", str(pr_number), "--remove-label", label],
        capture_output=True, text=True,
        env=env
    )
    if result.returncode != 0:
        # Log all errors (not just non-"not found")
        print(f"⚠️  Label removal failed (code {result.returncode}): {result.stderr.strip()}", file=sys.stderr)
    else:
        print(f"✅ Successfully removed '{label}' from PR #{pr_number}")


def get_pr_for_branch() -> Optional[int]:
    """Get the PR number for the current branch, if one exists."""
    result = subprocess.run(
        ["gh", "pr", "view", "--json", "number", "-q", ".number"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def post_pr_comment(pr_number: int, body: str) -> str:
    """Post a comment to a PR. Returns comment URL."""
    result = subprocess.run(
        ["gh", "pr", "comment", str(pr_number), "--body", body],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        die(f"Failed to post PR comment: {result.stderr}")
    return result.stdout.strip()


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

    when_unblocked = ""
    if data.when_unblocked:
        when_unblocked = f"\n\n**When unblocked:** {data.when_unblocked}"

    return f"""## Blocked

**Reason:** {data.reason}{blocked_by}
**Attempted:** {data.attempted}{when_unblocked}"""


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


def format_approved_comment(data: CompletionData) -> str:
    """Format an approved review comment."""
    return f"""## ✅ Code Review Approved

{data.summary}"""


def format_changes_requested_comment(data: CompletionData) -> str:
    """Format a changes-requested review comment."""
    return f"""## 🔄 Changes Requested

{data.issues}

---
*The work agent will be re-queued to address these issues.*"""


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
        if data.when_unblocked:
            trailers.append(f"Agent-When-Unblocked: {data.when_unblocked}")
    elif data.status == Status.NEEDS_HUMAN:
        trailers.append(f"Agent-Question: {data.question}")
        if data.context:
            trailers.append(f"Agent-Context: {data.context}")
    elif data.status == Status.APPROVED:
        trailers.append(f"Agent-Summary: {data.summary}")
    elif data.status == Status.CHANGES_REQUESTED:
        trailers.append(f"Agent-Issues: {data.issues}")

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


def run_preflight_checks(status: Status, dry_run: bool = False) -> None:
    """Run pre-flight checks before executing the completion workflow.

    These checks catch common issues early:
    - Uncommitted changes that would cause rebase to fail
    - Test failures that would block push via pre-push hooks

    Args:
        status: The completion status being executed
        dry_run: If True, only report issues without failing

    Raises:
        SystemExit: If checks fail and not in dry_run mode
    """
    issues = []

    # Check 1: Clean git status (no uncommitted changes)
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        uncommitted = result.stdout.strip().split('\n')
        issues.append(
            f"Uncommitted changes detected ({len(uncommitted)} files):\n"
            f"  {chr(10).join('  ' + line for line in uncommitted[:5])}"
            + (f"\n  ... and {len(uncommitted) - 5} more" if len(uncommitted) > 5 else "")
            + "\n  Fix: Stage and commit all changes before running agent-done"
        )

    # Check 2: Tests pass (only for completed status which pushes with pre-push hooks)
    if status == Status.COMPLETED and not issues:  # Skip if already have issues
        print("🧪 Running pre-flight test check...")
        # Run a quick test to verify the pre-push hook won't block us
        # Use --collect-only first to check for collection errors, then run tests
        result = subprocess.run(
            ["python", "-m", "pytest", "--tb=no", "-q", "--co", "-q"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            issues.append(
                "Test collection failed - tests would block push:\n"
                f"  {result.stderr.strip()[:500]}\n"
                "  Fix: Ensure all tests can be collected (fix import errors, syntax errors)"
            )
        else:
            # Actually run the tests
            result = subprocess.run(
                ["python", "-m", "pytest", "--tb=short", "-q"],
                capture_output=True, text=True,
                timeout=300  # 5 minute timeout
            )
            if result.returncode != 0:
                # Extract failure count from pytest output
                output = result.stdout + result.stderr
                issues.append(
                    "Tests are failing - push would be blocked by pre-push hook:\n"
                    f"  {output.strip()[-500:]}\n"
                    "  Fix: Make all tests pass before running agent-done"
                )

    if issues:
        if dry_run:
            print("\n⚠️  PRE-FLIGHT CHECK ISSUES (dry-run, not blocking):")
            for issue in issues:
                print(f"\n{issue}")
            print()
        else:
            print("\n❌ PRE-FLIGHT CHECKS FAILED:", file=sys.stderr)
            for issue in issues:
                print(f"\n{issue}", file=sys.stderr)
            print("\nFix the issues above and run agent-done again.", file=sys.stderr)
            sys.exit(1)
    else:
        print("✅ Pre-flight checks passed")


def git_rebase_on_main() -> None:
    """Fetch latest main and rebase current branch onto it.

    This prevents merge conflicts by ensuring the branch is up-to-date
    before creating a PR.
    """
    print("🔄 Rebasing on latest main...")

    # Fetch latest main
    result = subprocess.run(
        ["git", "fetch", "origin", "main"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"⚠️  Warning: Could not fetch main: {result.stderr}", file=sys.stderr)
        return  # Continue without rebase - push may still work

    # Rebase onto origin/main
    result = subprocess.run(
        ["git", "rebase", "origin/main"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        # Rebase failed - abort and continue without it
        subprocess.run(["git", "rebase", "--abort"], capture_output=True)
        print(f"⚠️  Warning: Rebase failed, continuing with current state", file=sys.stderr)
        print(f"    Conflict details: {result.stderr}", file=sys.stderr)
        return

    print("✅ Rebased on latest main")


def git_push() -> None:
    """Push current branch to origin."""
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True
    ).stdout.strip()

    # Use --force-with-lease for safety after rebase
    result = subprocess.run(
        ["git", "push", "-u", "--force-with-lease", "origin", branch],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        # Try regular push if force-with-lease fails (first push)
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

    # Add notes for reviewer and verification marker
    # Include label marker for orchestrator to reconcile on restart
    from .models import ORCHESTRATOR_PR_MARKER
    body_parts.extend([
        "---",
        f"*{ORCHESTRATOR_PR_MARKER} agent*",
        "",
        get_pr_verification_marker(issue_number),
        "<!-- orchestrator:needs-code-review -->",
    ])

    body = "\n".join(body_parts)

    # Set auth env var to bypass gh-wrapper block (only agent-done knows this)
    env = os.environ.copy()
    env["ORCHESTRATOR_GH_AUTH"] = "agent-done-authorized"

    result = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body],
        capture_output=True, text=True,
        env=env
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
    agent-done blocked --reason "Waiting for API docs" --attempted "..." --when-unblocked "Implement auth flow using new endpoints"

  Need human input:
    agent-done needs_human --question "Should we use OAuth or API keys?"
    agent-done needs_human --question "Which approach?" --options "Use Redis" "Use Postgres" --default "Use Redis"

  Review approved:
    agent-done approved --summary "Code is clean, tests pass, follows patterns"

  Review requests changes:
    agent-done changes_requested --issues "Missing error handling in foo(), needs tests for bar()"

STATUSES:
  completed          - Work done, PR ready (requires: --implementation, --problems)
  blocked            - Cannot proceed (requires: --reason, --attempted)
  needs_human        - Need decision/clarification (requires: --question)
  approved           - Review passed (requires: --summary)
  changes_requested  - Review needs fixes (requires: --issues)
"""
    )

    # Positional: status (required, validated)
    parser.add_argument(
        "status",
        choices=["completed", "blocked", "needs_human", "approved", "changes_requested"],
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
    parser.add_argument(
        "--when-unblocked", "-w",
        help="Hint for future agent: what to do when blocker is resolved (optional, for 'blocked')"
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

    # Reviewer fields
    parser.add_argument(
        "--summary", "-s",
        help="Summary of review (required for 'approved')"
    )
    parser.add_argument(
        "--issues",
        help="Issues found that need fixing (required for 'changes_requested')"
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
        when_unblocked=args.when_unblocked,
        question=args.question,
        context=args.context,
        options=args.options,
        default_action=args.default,
        summary=args.summary,
        issues=args.issues,
    )

    # Validate required fields (strict!)
    validate_fields(data)

    # Create marker file to indicate agent-done was called
    # This is checked by the Stop hook to detect sessions that exit without agent-done
    from pathlib import Path
    marker_file = Path(".agent-done-marker")
    marker_file.write_text(f"agent-done {status.value} called at {__import__('datetime').datetime.now().isoformat()}\n")

    # Run pre-flight checks before doing anything destructive
    # This catches issues like uncommitted changes or failing tests early
    run_preflight_checks(status, dry_run=args.dry_run)

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
    elif status == Status.NEEDS_HUMAN:
        comment_body = format_needs_human_comment(data)
    elif status == Status.APPROVED:
        comment_body = format_approved_comment(data)
    else:  # CHANGES_REQUESTED
        comment_body = format_changes_requested_comment(data)

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
        # 1. Rebase on latest main to avoid merge conflicts
        git_rebase_on_main()

        # 2. Push code (pre-push hook validates trailers)
        print("🚀 Pushing code...")
        git_push()

        # 2. Create PR with structured content
        print("📝 Creating PR...")
        issue_title = get_issue_title(issue_number)
        pr_title = f"{issue_title} (#{issue_number})"
        pr_url = create_pr(issue_number, pr_title, data)

        # 3. Add code review label if configured (triggers code review agent)
        code_review_label = get_code_review_label()
        if code_review_label:
            add_label_to_pr(pr_url, code_review_label)

        # 4. Update comment with PR URL and post
        comment_body = comment_body.replace("<PR_LINK_PLACEHOLDER>", pr_url)
        print("💬 Posting completion comment...")
        post_comment(repo, issue_number, comment_body)

        print(f"\n✅ COMPLETED: PR created at {pr_url}")

    elif status == Status.BLOCKED:
        # 1. Push any work done (trailers included)
        print("🚀 Pushing work so far...")
        git_push()

        # 2. Add blocked label (with prefix if configured)
        blocked_label = get_blocked_label()
        print(f"🏷️  Adding '{blocked_label}' label...")
        add_label(repo, issue_number, blocked_label)

        # 3. Post comment
        print("💬 Posting blocked comment...")
        post_comment(repo, issue_number, comment_body)

        print(f"\n🚧 BLOCKED: Issue #{issue_number} marked as blocked")

    elif status == Status.NEEDS_HUMAN:
        # 1. Push any work done (trailers included)
        print("🚀 Pushing work so far...")
        git_push()

        # 2. Add needs-human label (with prefix if configured)
        needs_human_label = get_needs_human_label()
        print(f"🏷️  Adding '{needs_human_label}' label...")
        add_label(repo, issue_number, needs_human_label)

        # 3. Post comment
        print("💬 Posting question comment...")
        post_comment(repo, issue_number, comment_body)

        print(f"\n❓ NEEDS HUMAN: Question posted on issue #{issue_number}")

    elif status == Status.APPROVED:
        # Reviewer approved the PR
        # 1. Get PR number (reviewer is working in PR context)
        pr_number = get_pr_for_branch()
        if not pr_number:
            die("Could not find PR for current branch. Are you in a review worktree?")

        # 2. Remove needs-code-review label, add code-reviewed label
        code_review_label = get_code_review_label()
        if code_review_label:
            remove_label_from_pr(pr_number, code_review_label)
        else:
            print("⚠️  No code_review_label configured - skipping label removal", file=sys.stderr)

        code_reviewed_label = get_code_reviewed_label()
        if code_reviewed_label:
            print(f"🏷️  Adding '{code_reviewed_label}' label to PR #{pr_number}...")
            # Set auth env var to bypass gh-wrapper block
            env = os.environ.copy()
            env["ORCHESTRATOR_GH_AUTH"] = "agent-done-authorized"
            result = subprocess.run(
                ["gh", "pr", "edit", str(pr_number), "--add-label", code_reviewed_label],
                capture_output=True, text=True,
                env=env
            )
            if result.returncode == 0:
                print(f"✅ Successfully added '{code_reviewed_label}' to PR #{pr_number}")
            else:
                print(f"⚠️  Failed to add label: {result.stderr.strip()}", file=sys.stderr)
        else:
            print("⚠️  No code_reviewed_label configured - skipping label addition", file=sys.stderr)

        # 3. Post review comment on PR
        print("💬 Posting review comment...")
        post_pr_comment(pr_number, comment_body)

        print(f"\n✅ APPROVED: PR #{pr_number} approved by reviewer")

    elif status == Status.CHANGES_REQUESTED:
        # Reviewer requested changes
        # 1. Get PR number
        pr_number = get_pr_for_branch()
        if not pr_number:
            die("Could not find PR for current branch. Are you in a review worktree?")

        # 2. Remove needs-code-review, add needs-rework label
        code_review_label = get_code_review_label()
        if code_review_label:
            remove_label_from_pr(pr_number, code_review_label)
        else:
            print("⚠️  No code_review_label configured - skipping label removal", file=sys.stderr)

        needs_rework_label = get_needs_rework_label()
        print(f"🏷️  Adding '{needs_rework_label}' label to PR #{pr_number}...")
        # Set auth env var to bypass gh-wrapper block
        env = os.environ.copy()
        env["ORCHESTRATOR_GH_AUTH"] = "agent-done-authorized"
        result = subprocess.run(
            ["gh", "pr", "edit", str(pr_number), "--add-label", needs_rework_label],
            capture_output=True, text=True,
            env=env
        )
        if result.returncode == 0:
            print(f"✅ Successfully added '{needs_rework_label}' to PR #{pr_number}")
        else:
            print(f"⚠️  Failed to add label: {result.stderr.strip()}", file=sys.stderr)

        # 3. Post review comment on PR
        print("💬 Posting review comment...")
        post_pr_comment(pr_number, comment_body)

        print(f"\n🔄 CHANGES REQUESTED: PR #{pr_number} needs rework")


if __name__ == "__main__":
    main()
