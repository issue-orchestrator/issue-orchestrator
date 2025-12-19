"""Issue state analysis - shared between dry-run and startup."""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .github import get_open_prs_for_branch
from .models import Issue


@dataclass
class IssueState:
    """Analyzed state of an issue."""
    issue: Issue
    has_session: bool = False
    branch: Optional[str] = None
    has_open_pr: bool = False
    pr_url: Optional[str] = None

    @property
    def is_stale(self) -> bool:
        """Issue is stale if marked in-progress but no active work."""
        return (
            self.issue.is_in_progress
            and not self.has_session
            and not self.has_open_pr
        )

    @property
    def is_orphaned_label(self) -> bool:
        """Label is orphaned if in-progress but nothing exists."""
        return (
            self.issue.is_in_progress
            and not self.has_session
            and not self.branch
            and not self.has_open_pr
        )

    @property
    def has_partial_work(self) -> bool:
        """Has partial work if branch exists but no session/PR."""
        return (
            self.branch is not None
            and not self.has_session
            and not self.has_open_pr
        )

    @property
    def status_summary(self) -> str:
        """Human-readable status summary."""
        if self.has_session:
            return "active"
        if self.has_open_pr:
            return "pr-pending"
        if self.issue.is_blocked:
            return "blocked"
        if self.issue.is_in_progress:
            if self.branch:
                return "stale-with-branch"
            return "stale-orphaned"
        return "available"


def get_issue_branches(repo_root: Path) -> dict[int, str]:
    """Get branches that match issue pattern (e.g., '5-some-title').

    Args:
        repo_root: Path to the git repository root

    Returns:
        Dict mapping issue number to branch name
    """
    try:
        result = subprocess.run(
            ["git", "branch", "-r", "--list", "origin/*"],
            capture_output=True, text=True, cwd=repo_root
        )
        branches = {}
        for line in result.stdout.strip().split("\n"):
            branch = line.strip().replace("origin/", "")
            # Match pattern: {issue_number}-{slug}
            if branch and branch[0].isdigit():
                parts = branch.split("-", 1)
                if parts[0].isdigit():
                    branches[int(parts[0])] = branch
        return branches
    except Exception:
        return {}


def analyze_issue(
    issue: Issue,
    repo: Optional[str],
    issue_branches: dict[int, str],
    check_session_fn,
) -> IssueState:
    """Analyze the full state of an issue.

    Args:
        issue: The Issue to analyze
        repo: GitHub repo (owner/repo format)
        issue_branches: Dict of issue number -> branch name
        check_session_fn: Function to check if tmux session exists (issue_number) -> bool

    Returns:
        IssueState with all analyzed information
    """
    state = IssueState(issue=issue)

    # Check tmux session
    state.has_session = check_session_fn(issue.number)

    # Check for branch
    state.branch = issue_branches.get(issue.number)

    # Check for open PR if branch exists
    if state.branch and repo:
        try:
            prs = get_open_prs_for_branch(repo, state.branch)
            if prs:
                state.has_open_pr = True
                state.pr_url = prs[0].get("url")
        except Exception:
            pass

    return state


def analyze_all_issues(
    issues: list[Issue],
    repo: Optional[str],
    repo_root: Path,
    check_session_fn,
) -> list[IssueState]:
    """Analyze all issues and return their states.

    Args:
        issues: List of Issues to analyze
        repo: GitHub repo (owner/repo format)
        repo_root: Path to git repository
        check_session_fn: Function to check if tmux session exists

    Returns:
        List of IssueState objects
    """
    branches = get_issue_branches(repo_root)
    return [
        analyze_issue(issue, repo, branches, check_session_fn)
        for issue in issues
    ]


@dataclass
class OrphanBranchState:
    """Analyzed state of a branch without an in-progress issue."""
    issue_number: int
    branch_name: str
    issue_state: Optional[str] = None  # "open", "closed", or None if not found
    issue_title: Optional[str] = None
    has_closed_pr: bool = False
    closed_pr_url: Optional[str] = None
    commits_ahead: int = 0
    last_commit_date: Optional[str] = None

    @property
    def suggested_action(self) -> str:
        """Suggest what to do with this branch."""
        if self.has_closed_pr:
            return "delete-branch"  # PR merged/closed, branch can go
        if self.issue_state == "closed":
            return "delete-branch"  # Issue closed, work abandoned
        if self.issue_state == "open" and self.commits_ahead > 0:
            return "resume-work"  # Has commits, issue still open
        if self.commits_ahead == 0:
            return "delete-branch"  # Empty branch
        return "investigate"


def analyze_orphan_branches(
    issue_branches: dict[int, str],
    in_progress_issue_numbers: set[int],
    repo: Optional[str],
    repo_root: Path,
) -> list[OrphanBranchState]:
    """Analyze branches that exist but aren't marked in-progress.

    Args:
        issue_branches: Dict of issue number -> branch name
        in_progress_issue_numbers: Set of issue numbers currently marked in-progress
        repo: GitHub repo (owner/repo format)
        repo_root: Path to git repository

    Returns:
        List of OrphanBranchState objects
    """
    from .github import get_open_prs_for_branch

    orphans = []
    for issue_num, branch_name in issue_branches.items():
        if issue_num in in_progress_issue_numbers:
            continue

        state = OrphanBranchState(
            issue_number=issue_num,
            branch_name=branch_name,
        )

        # Get commits ahead of main
        try:
            result = subprocess.run(
                ["git", "rev-list", "--count", f"origin/main..origin/{branch_name}"],
                capture_output=True, text=True, cwd=repo_root
            )
            if result.returncode == 0:
                state.commits_ahead = int(result.stdout.strip())
        except Exception:
            pass

        # Get last commit date
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%cr", f"origin/{branch_name}"],
                capture_output=True, text=True, cwd=repo_root
            )
            if result.returncode == 0:
                state.last_commit_date = result.stdout.strip()
        except Exception:
            pass

        # Check issue state via gh CLI
        if repo:
            try:
                result = subprocess.run(
                    ["gh", "issue", "view", str(issue_num), "--repo", repo,
                     "--json", "state,title"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    import json
                    data = json.loads(result.stdout)
                    state.issue_state = data.get("state", "").lower()
                    state.issue_title = data.get("title")
            except Exception:
                pass

            # Check for closed PRs on this branch
            try:
                result = subprocess.run(
                    ["gh", "pr", "list", "--head", branch_name, "--repo", repo,
                     "--state", "all", "--json", "number,state,url"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    import json
                    prs = json.loads(result.stdout)
                    closed_prs = [p for p in prs if p.get("state") in ("CLOSED", "MERGED")]
                    if closed_prs:
                        state.has_closed_pr = True
                        state.closed_pr_url = closed_prs[0].get("url")
            except Exception:
                pass

        orphans.append(state)

    # Sort by suggested action priority (resume-work first, then investigate, then delete)
    action_priority = {"resume-work": 0, "investigate": 1, "delete-branch": 2}
    orphans.sort(key=lambda o: (action_priority.get(o.suggested_action, 1), -o.commits_ahead))

    return orphans
