"""Issue state analysis - shared between dry-run and startup."""
from dataclasses import dataclass
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.pull_request_tracker import PullRequestTracker
    from ..ports.issue_tracker import IssueTracker

from ..ports.issue import Issue


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


def extract_issue_branches(branches: list[str]) -> dict[int, str]:
    """Extract issue-numbered branches from a list of branch names."""
    issue_branches: dict[int, str] = {}
    for raw in branches:
        branch = raw.strip()
        if branch.startswith("origin/"):
            branch = branch[len("origin/"):]
        if branch and branch[0].isdigit():
            parts = branch.split("-", 1)
            if parts[0].isdigit():
                issue_branches[int(parts[0])] = branch
    return issue_branches


def analyze_issue(
    issue: Issue,
    repo: Optional[str],
    issue_branches: dict[int, str],
    check_session_fn,
    pr_tracker: Optional["PullRequestTracker"] = None,
) -> IssueState:
    """Analyze the full state of an issue.

    Args:
        issue: The Issue to analyze
        repo: GitHub repo (owner/repo format)
        issue_branches: Dict of issue number -> branch name
        check_session_fn: Function to check if tmux session exists (issue_number) -> bool
        pr_tracker: Optional PullRequestTracker for checking open PRs

    Returns:
        IssueState with all analyzed information
    """
    state = IssueState(issue=issue)

    # Check tmux session
    state.has_session = check_session_fn(issue.number)

    # Check for branch
    state.branch = issue_branches.get(issue.number)

    # Check for open PR if branch exists
    if pr_tracker:
        if state.branch:
            try:
                prs = pr_tracker.get_prs_for_branch(state.branch, state="open")
                if prs:
                    state.has_open_pr = True
                    state.pr_url = prs[0].url
            except Exception:
                pass

        # pr-pending is already a persisted claim that an issue has moved into PR
        # flow. Recover via issue-level PR lookup only when the active branch is
        # unknown; otherwise we would let an older PR on a stale branch override
        # the current attempt after a scratch reset.
        if not state.has_open_pr and state.branch is None and "pr-pending" in issue.labels:
            try:
                prs = pr_tracker.get_prs_for_issue(issue.number, state="open")
                if prs:
                    state.has_open_pr = True
                    state.pr_url = prs[0].url
            except Exception:
                pass

    return state


def analyze_all_issues(
    issues: list[Issue],
    repo: Optional[str],
    issue_branches: dict[int, str],
    check_session_fn,
    pr_tracker: Optional["PullRequestTracker"] = None,
) -> list[IssueState]:
    """Analyze all issues and return their states.

    Args:
        issues: List of Issues to analyze
        repo: GitHub repo (owner/repo format)
        issue_branches: Dict of issue number -> branch name
        check_session_fn: Function to check if tmux session exists
        pr_tracker: Optional PullRequestTracker for checking open PRs

    Returns:
        List of IssueState objects
    """
    return [
        analyze_issue(issue, repo, issue_branches, check_session_fn, pr_tracker)
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


def _safe_call(fn, *args, default=None):
    """Call fn safely, returning default on exception."""
    if fn is None:
        return default
    try:
        return fn(*args)
    except Exception:
        return default


def _enrich_orphan_state(
    state: OrphanBranchState,
    issue_tracker: "IssueTracker | None",
    pr_tracker: "PullRequestTracker | None",
    commits_ahead_fn: Callable[[str], int] | None,
    last_commit_date_fn: Callable[[str], Optional[str]] | None,
) -> None:
    """Enrich orphan branch state with additional data."""
    if commits_ahead_fn:
        result = _safe_call(commits_ahead_fn, state.branch_name)
        if result is not None:
            state.commits_ahead = result

    if last_commit_date_fn:
        result = _safe_call(last_commit_date_fn, state.branch_name)
        if result is not None:
            state.last_commit_date = result

    if issue_tracker:
        issue = _safe_call(issue_tracker.get_issue, state.issue_number)
        if issue:
            state.issue_state = str(issue.state).lower()
            state.issue_title = issue.title

    if pr_tracker:
        prs = _safe_call(pr_tracker.get_prs_for_branch, state.branch_name, "all", default=[])
        closed_prs = [p for p in prs if p.state.lower() in ("closed", "merged")]
        if closed_prs:
            state.has_closed_pr = True
            state.closed_pr_url = closed_prs[0].url


def analyze_orphan_branches(
    issue_branches: dict[int, str],
    in_progress_issue_numbers: set[int],
    repo: Optional[str],
    issue_tracker: "IssueTracker | None" = None,
    pr_tracker: "PullRequestTracker | None" = None,
    commits_ahead_fn: Callable[[str], int] | None = None,
    last_commit_date_fn: Callable[[str], Optional[str]] | None = None,
) -> list[OrphanBranchState]:
    """Analyze branches that exist but aren't marked in-progress."""
    orphans = []
    for issue_num, branch_name in issue_branches.items():
        if issue_num in in_progress_issue_numbers:
            continue

        state = OrphanBranchState(issue_number=issue_num, branch_name=branch_name)
        _enrich_orphan_state(state, issue_tracker, pr_tracker, commits_ahead_fn, last_commit_date_fn)
        orphans.append(state)

    # Sort by suggested action priority (resume-work first, then investigate, then delete)
    action_priority = {"resume-work": 0, "investigate": 1, "delete-branch": 2}
    orphans.sort(key=lambda o: (action_priority.get(o.suggested_action, 1), -o.commits_ahead))

    return orphans
