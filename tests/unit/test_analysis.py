"""Unit tests for issue state analysis."""

from unittest.mock import Mock, patch


from issue_orchestrator.infra.analysis import (
    IssueState,
    OrphanBranchState,
    extract_issue_branches,
    analyze_issue,
    analyze_all_issues,
    analyze_orphan_branches,
)
from issue_orchestrator.domain.models import Issue


class TestIssueState:
    """Test the IssueState dataclass and its properties."""

    def test_is_stale_when_in_progress_no_session_no_pr(self):
        """Issue is stale if marked in-progress but no active work."""
        issue = Issue(
            number=1,
            title="Test",
            labels=["in-progress"],
        )
        state = IssueState(
            issue=issue,
            has_session=False,
            has_open_pr=False,
        )
        assert state.is_stale is True

    def test_is_not_stale_when_has_session(self):
        """Issue is not stale if it has an active session."""
        issue = Issue(
            number=1,
            title="Test",
            labels=["in-progress"],
        )
        state = IssueState(
            issue=issue,
            has_session=True,
            has_open_pr=False,
        )
        assert state.is_stale is False

    def test_is_not_stale_when_has_open_pr(self):
        """Issue is not stale if it has an open PR."""
        issue = Issue(
            number=1,
            title="Test",
            labels=["in-progress"],
        )
        state = IssueState(
            issue=issue,
            has_session=False,
            has_open_pr=True,
        )
        assert state.is_stale is False

    def test_is_not_stale_when_not_in_progress(self):
        """Issue is not stale if not marked in-progress."""
        issue = Issue(
            number=1,
            title="Test",
            labels=[],
        )
        state = IssueState(
            issue=issue,
            has_session=False,
            has_open_pr=False,
        )
        assert state.is_stale is False

    def test_is_orphaned_label_when_in_progress_nothing_exists(self):
        """Label is orphaned if in-progress but nothing exists."""
        issue = Issue(
            number=1,
            title="Test",
            labels=["in-progress"],
        )
        state = IssueState(
            issue=issue,
            has_session=False,
            branch=None,
            has_open_pr=False,
        )
        assert state.is_orphaned_label is True

    def test_is_not_orphaned_label_when_has_branch(self):
        """Label is not orphaned if branch exists."""
        issue = Issue(
            number=1,
            title="Test",
            labels=["in-progress"],
        )
        state = IssueState(
            issue=issue,
            has_session=False,
            branch="1-test",
            has_open_pr=False,
        )
        assert state.is_orphaned_label is False

    def test_is_not_orphaned_label_when_has_session(self):
        """Label is not orphaned if session exists."""
        issue = Issue(
            number=1,
            title="Test",
            labels=["in-progress"],
        )
        state = IssueState(
            issue=issue,
            has_session=True,
            branch=None,
            has_open_pr=False,
        )
        assert state.is_orphaned_label is False

    def test_is_not_orphaned_label_when_not_in_progress(self):
        """Label is not orphaned if not in-progress."""
        issue = Issue(
            number=1,
            title="Test",
            labels=[],
        )
        state = IssueState(
            issue=issue,
            has_session=False,
            branch=None,
            has_open_pr=False,
        )
        assert state.is_orphaned_label is False

    def test_has_partial_work_when_branch_no_session_no_pr(self):
        """Has partial work if branch exists but no session/PR."""
        issue = Issue(
            number=1,
            title="Test",
            labels=[],
        )
        state = IssueState(
            issue=issue,
            has_session=False,
            branch="1-test",
            has_open_pr=False,
        )
        assert state.has_partial_work is True

    def test_no_partial_work_when_has_session(self):
        """No partial work if session exists."""
        issue = Issue(
            number=1,
            title="Test",
            labels=[],
        )
        state = IssueState(
            issue=issue,
            has_session=True,
            branch="1-test",
            has_open_pr=False,
        )
        assert state.has_partial_work is False

    def test_no_partial_work_when_has_pr(self):
        """No partial work if PR exists."""
        issue = Issue(
            number=1,
            title="Test",
            labels=[],
        )
        state = IssueState(
            issue=issue,
            has_session=False,
            branch="1-test",
            has_open_pr=True,
        )
        assert state.has_partial_work is False

    def test_no_partial_work_when_no_branch(self):
        """No partial work if no branch exists."""
        issue = Issue(
            number=1,
            title="Test",
            labels=[],
        )
        state = IssueState(
            issue=issue,
            has_session=False,
            branch=None,
            has_open_pr=False,
        )
        assert state.has_partial_work is False

    def test_status_summary_active_when_has_session(self):
        """Status is 'active' when session exists."""
        issue = Issue(number=1, title="Test", labels=[])
        state = IssueState(issue=issue, has_session=True)
        assert state.status_summary == "active"

    def test_status_summary_pr_pending_when_has_pr(self):
        """Status is 'pr-pending' when PR exists."""
        issue = Issue(number=1, title="Test", labels=[])
        state = IssueState(issue=issue, has_session=False, has_open_pr=True)
        assert state.status_summary == "pr-pending"

    def test_status_summary_blocked_when_blocked_label(self):
        """Status is 'blocked' when blocked label exists."""
        issue = Issue(number=1, title="Test", labels=["blocked"])
        state = IssueState(issue=issue, has_session=False, has_open_pr=False)
        assert state.status_summary == "blocked"

    def test_status_summary_blocked_when_needs_human_label(self):
        """Status is 'blocked' when needs-human label exists (it's a blocking label)."""
        issue = Issue(number=1, title="Test", labels=["needs-human"])
        state = IssueState(issue=issue, has_session=False, has_open_pr=False)
        assert state.status_summary == "blocked"

    def test_status_summary_stale_with_branch(self):
        """Status is 'stale-with-branch' when in-progress with branch but no activity."""
        issue = Issue(number=1, title="Test", labels=["in-progress"])
        state = IssueState(
            issue=issue,
            has_session=False,
            branch="1-test",
            has_open_pr=False,
        )
        assert state.status_summary == "stale-with-branch"

    def test_status_summary_stale_orphaned(self):
        """Status is 'stale-orphaned' when in-progress but nothing exists."""
        issue = Issue(number=1, title="Test", labels=["in-progress"])
        state = IssueState(
            issue=issue,
            has_session=False,
            branch=None,
            has_open_pr=False,
        )
        assert state.status_summary == "stale-orphaned"

    def test_status_summary_available(self):
        """Status is 'available' when no special conditions."""
        issue = Issue(number=1, title="Test", labels=[])
        state = IssueState(
            issue=issue,
            has_session=False,
            branch=None,
            has_open_pr=False,
        )
        assert state.status_summary == "available"

    def test_status_summary_priority_active_over_blocked(self):
        """Active session takes priority over blocked label."""
        issue = Issue(number=1, title="Test", labels=["blocked"])
        state = IssueState(issue=issue, has_session=True)
        assert state.status_summary == "active"

    def test_status_summary_priority_pr_over_blocked(self):
        """Open PR takes priority over blocked label."""
        issue = Issue(number=1, title="Test", labels=["blocked"])
        state = IssueState(issue=issue, has_session=False, has_open_pr=True)
        assert state.status_summary == "pr-pending"


class TestExtractIssueBranches:
    """Test the extract_issue_branches function."""

    def test_extract_issue_branches_success(self):
        branches = ["  origin/5-add-feature", "origin/12-fix-bug", "origin/main"]
        result = extract_issue_branches(branches)
        assert result == {5: "5-add-feature", 12: "12-fix-bug"}

    def test_extract_issue_branches_no_matches(self):
        branches = ["origin/main", "origin/develop"]
        result = extract_issue_branches(branches)
        assert result == {}

    def test_extract_issue_branches_empty_output(self):
        result = extract_issue_branches([])
        assert result == {}

    def test_extract_issue_branches_handles_whitespace(self):
        branches = ["  origin/5-add-feature  ", "  origin/12-fix-bug"]
        result = extract_issue_branches(branches)
        assert result == {5: "5-add-feature", 12: "12-fix-bug"}

    def test_extract_issue_branches_ignores_non_numeric(self):
        branches = ["origin/5-add-feature", "origin/feature-branch", "origin/fix-something"]
        result = extract_issue_branches(branches)
        assert result == {5: "5-add-feature"}

    def test_extract_issue_branches_handles_multidigit_numbers(self):
        branches = ["origin/123-large-issue", "origin/9999-huge-number"]
        result = extract_issue_branches(branches)
        assert result == {123: "123-large-issue", 9999: "9999-huge-number"}

    def test_extract_issue_branches_prefers_newest_scratch_branch(self):
        branches = [
            "origin/4057-old-branch",
            "origin/4057-scratch-1774098188",
            "origin/4057-scratch-1774101016-r1",
        ]
        result = extract_issue_branches(branches)
        assert result == {4057: "4057-scratch-1774101016-r1"}


class TestAnalyzeIssue:
    """Test the analyze_issue function."""

    def test_analyze_issue_basic(self):
        """Test basic issue analysis with no branch or session."""
        issue = Issue(number=5, title="Test Issue", labels=[])
        check_session = Mock(return_value=False)

        result = analyze_issue(issue, "owner/repo", {}, check_session)

        assert result.issue == issue
        assert result.has_session is False
        assert result.branch is None
        assert result.has_open_pr is False
        assert result.pr_url is None
        check_session.assert_called_once_with(5)

    def test_analyze_issue_with_session(self):
        """Test issue with active session."""
        issue = Issue(number=5, title="Test Issue", labels=[])
        check_session = Mock(return_value=True)

        result = analyze_issue(issue, "owner/repo", {}, check_session)

        assert result.has_session is True

    def test_analyze_issue_with_branch_and_pr(self):
        """Test issue with branch and open PR."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        issue = Issue(number=5, title="Test Issue", labels=[])
        check_session = Mock(return_value=False)

        mock_pr_tracker = Mock()
        mock_pr_tracker.get_prs_for_branch.return_value = [
            PRInfo(number=10, url="https://github.com/owner/repo/pull/10", title="PR", branch="5-test-issue", labels=[], body="", state="open")
        ]

        result = analyze_issue(
            issue, "owner/repo", {5: "5-test-issue"}, check_session, mock_pr_tracker
        )

        assert result.branch == "5-test-issue"
        assert result.has_open_pr is True
        assert result.pr_url == "https://github.com/owner/repo/pull/10"
        mock_pr_tracker.get_prs_for_branch.assert_called_once_with("5-test-issue", state="open")

    def test_analyze_issue_with_branch_no_pr(self):
        """Test issue with branch but no open PR."""
        issue = Issue(number=5, title="Test Issue", labels=[])
        check_session = Mock(return_value=False)

        mock_pr_tracker = Mock()
        mock_pr_tracker.get_prs_for_branch.return_value = []

        result = analyze_issue(
            issue, "owner/repo", {5: "5-test-issue"}, check_session, mock_pr_tracker
        )

        assert result.branch == "5-test-issue"
        assert result.has_open_pr is False
        assert result.pr_url is None

    def test_analyze_issue_pr_pending_without_branch_uses_issue_pr_lookup(self):
        """Test pr-pending issue recovery when startup has no local branch mapping."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        issue = Issue(number=4057, title="Test Issue", labels=["pr-pending"])
        check_session = Mock(return_value=False)

        mock_pr_tracker = Mock()
        mock_pr_tracker.get_prs_for_issue.return_value = [
            PRInfo(
                number=5337,
                url="https://github.com/owner/repo/pull/5337",
                title="#4057: Test Issue",
                branch="4057-test-issue",
                labels=[],
                body="",
                state="open",
            )
        ]

        result = analyze_issue(issue, "owner/repo", {}, check_session, mock_pr_tracker)

        assert result.branch is None
        assert result.has_open_pr is True
        assert result.pr_url == "https://github.com/owner/repo/pull/5337"
        mock_pr_tracker.get_prs_for_issue.assert_called_once_with(4057, state="open")
        mock_pr_tracker.get_prs_for_branch.assert_not_called()

    def test_analyze_issue_pr_pending_with_current_branch_does_not_reuse_old_issue_pr(self):
        """A known current branch should block issue-level fallback to older PRs."""
        issue = Issue(number=4057, title="Test Issue", labels=["pr-pending"])
        check_session = Mock(return_value=False)

        mock_pr_tracker = Mock()
        mock_pr_tracker.get_prs_for_branch.return_value = []
        mock_pr_tracker.get_prs_for_issue.return_value = [Mock()]

        result = analyze_issue(
            issue,
            "owner/repo",
            {4057: "4057-scratch-2"},
            check_session,
            mock_pr_tracker,
        )

        assert result.branch == "4057-scratch-2"
        assert result.has_open_pr is False
        mock_pr_tracker.get_prs_for_branch.assert_called_once_with("4057-scratch-2", state="open")
        mock_pr_tracker.get_prs_for_issue.assert_not_called()

    def test_analyze_issue_no_pr_tracker_skips_pr_check(self):
        """Test that PR check is skipped when no pr_tracker provided."""
        issue = Issue(number=5, title="Test Issue", labels=[])
        check_session = Mock(return_value=False)

        result = analyze_issue(issue, "owner/repo", {5: "5-test-issue"}, check_session)

        assert result.branch == "5-test-issue"
        assert result.has_open_pr is False
        # No pr_tracker, so no PR check

    def test_analyze_issue_pr_check_exception_ignored(self):
        """Test that exceptions in PR check are caught and ignored."""
        issue = Issue(number=5, title="Test Issue", labels=[])
        check_session = Mock(return_value=False)

        mock_pr_tracker = Mock()
        mock_pr_tracker.get_prs_for_branch.side_effect = Exception("API error")

        result = analyze_issue(
            issue, "owner/repo", {5: "5-test-issue"}, check_session, mock_pr_tracker
        )

        assert result.has_open_pr is False
        assert result.pr_url is None


class TestAnalyzeAllIssues:
    """Test the analyze_all_issues function."""

    @patch("issue_orchestrator.infra.analysis.analyze_issue")
    def test_analyze_all_issues_empty_list(self, mock_analyze):
        """Test analyzing empty issue list."""
        check_session = Mock()

        result = analyze_all_issues([], "owner/repo", {}, check_session)

        assert result == []
        mock_analyze.assert_not_called()

    @patch("issue_orchestrator.infra.analysis.analyze_issue")
    def test_analyze_all_issues_multiple_issues(self, mock_analyze):
        """Test analyzing multiple issues."""
        issue1 = Issue(number=1, title="Issue 1", labels=[])
        issue2 = Issue(number=2, title="Issue 2", labels=[])
        issues = [issue1, issue2]

        state1 = IssueState(issue=issue1)
        state2 = IssueState(issue=issue2)
        mock_analyze.side_effect = [state1, state2]

        check_session = Mock()

        result = analyze_all_issues(
            issues, "owner/repo", {1: "1-issue-1"}, check_session
        )

        assert len(result) == 2
        assert result[0] == state1
        assert result[1] == state2

        assert mock_analyze.call_count == 2
        mock_analyze.assert_any_call(
            issue1, "owner/repo", {1: "1-issue-1"}, check_session, None
        )
        mock_analyze.assert_any_call(
            issue2, "owner/repo", {1: "1-issue-1"}, check_session, None
        )

    @patch("issue_orchestrator.infra.analysis.analyze_issue")
    def test_analyze_all_issues_passes_branches_to_all(
        self, mock_analyze
    ):
        """Test that branches are fetched once and passed to all analyses."""
        issues = [
            Issue(number=1, title="Issue 1", labels=[]),
            Issue(number=2, title="Issue 2", labels=[]),
        ]

        branches = {1: "1-issue-1", 2: "2-issue-2"}

        mock_analyze.side_effect = [
            IssueState(issue=issues[0]),
            IssueState(issue=issues[1]),
        ]

        check_session = Mock()

        analyze_all_issues(issues, "owner/repo", branches, check_session)

        # Same branches dict passed to both analyze calls
        for call in mock_analyze.call_args_list:
            assert call[0][2] == branches  # Third argument is branches


class TestOrphanBranchState:
    """Test the OrphanBranchState dataclass and its properties."""

    def test_suggested_action_delete_when_has_closed_pr(self):
        """Suggest delete when PR merged/closed."""
        state = OrphanBranchState(
            issue_number=5,
            branch_name="5-test",
            has_closed_pr=True,
        )
        assert state.suggested_action == "delete-branch"

    def test_suggested_action_delete_when_issue_closed(self):
        """Suggest delete when issue closed."""
        state = OrphanBranchState(
            issue_number=5,
            branch_name="5-test",
            issue_state="closed",
            has_closed_pr=False,
        )
        assert state.suggested_action == "delete-branch"

    def test_suggested_action_resume_when_open_with_commits(self):
        """Suggest resume work when issue open with commits."""
        state = OrphanBranchState(
            issue_number=5,
            branch_name="5-test",
            issue_state="open",
            commits_ahead=3,
            has_closed_pr=False,
        )
        assert state.suggested_action == "resume-work"

    def test_suggested_action_delete_when_no_commits(self):
        """Suggest delete when branch empty."""
        state = OrphanBranchState(
            issue_number=5,
            branch_name="5-test",
            commits_ahead=0,
            has_closed_pr=False,
        )
        assert state.suggested_action == "delete-branch"

    def test_suggested_action_investigate_when_unknown(self):
        """Suggest investigate for unknown cases."""
        state = OrphanBranchState(
            issue_number=5,
            branch_name="5-test",
            issue_state=None,
            commits_ahead=1,
            has_closed_pr=False,
        )
        assert state.suggested_action == "investigate"

    def test_suggested_action_priority_closed_pr_over_issue_state(self):
        """Closed PR takes priority over issue state."""
        state = OrphanBranchState(
            issue_number=5,
            branch_name="5-test",
            issue_state="open",
            commits_ahead=3,
            has_closed_pr=True,
        )
        assert state.suggested_action == "delete-branch"


class TestAnalyzeOrphanBranches:
    """Test the analyze_orphan_branches function."""

    def test_analyze_orphan_branches_empty(self):
        """Test with no orphan branches."""
        result = analyze_orphan_branches(
            {}, set(), "owner/repo"
        )

        assert result == []

    def test_analyze_orphan_branches_skips_in_progress(self):
        """Test that in-progress issues are skipped."""
        issue_branches = {1: "1-test", 2: "2-test"}
        in_progress = {1}

        result = analyze_orphan_branches(
            issue_branches, in_progress, None
        )

        # Only issue 2 should be analyzed
        assert len(result) == 1
        assert result[0].issue_number == 2

    def test_analyze_orphan_branches_gets_commits_ahead(self):
        """Test getting commits ahead of main."""
        issue_branches = {5: "5-test"}

        result = analyze_orphan_branches(
            issue_branches,
            set(),
            None,
            commits_ahead_fn=lambda branch: 3,
            last_commit_date_fn=lambda branch: "2 days ago",
        )

        assert len(result) == 1
        assert result[0].commits_ahead == 3
        assert result[0].last_commit_date == "2 days ago"

    def test_analyze_orphan_branches_handles_git_errors(self):
        """Test handling git command failures."""
        issue_branches = {5: "5-test"}

        def raise_error(branch: str) -> int:
            raise Exception("git error")
        def raise_error_date(branch: str) -> str:
            raise Exception("git error")

        result = analyze_orphan_branches(
            issue_branches,
            set(),
            None,
            commits_ahead_fn=raise_error,
            last_commit_date_fn=raise_error_date,
        )

        assert len(result) == 1
        assert result[0].commits_ahead == 0  # default
        assert result[0].last_commit_date is None  # default

    def test_analyze_orphan_branches_with_repo_checks_issue_state(self):
        """Test checking issue state via IssueTracker when provided."""
        issue_branches = {5: "5-test"}

        issue_tracker = Mock()
        issue_tracker.get_issue.return_value = Issue(number=5, title="Test Issue", labels=[], state="open")

        result = analyze_orphan_branches(
            issue_branches,
            set(),
            "owner/repo",
            issue_tracker=issue_tracker,
            commits_ahead_fn=lambda branch: 3,
            last_commit_date_fn=lambda branch: "2 days ago",
        )

        assert len(result) == 1
        assert result[0].issue_state == "open"
        assert result[0].issue_title == "Test Issue"

    def test_analyze_orphan_branches_checks_closed_prs(self):
        """Test checking for closed PRs on the branch."""
        issue_branches = {5: "5-test"}

        issue_tracker = Mock()
        issue_tracker.get_issue.return_value = Issue(number=5, title="Test", labels=[], state="open")
        pr_tracker = Mock()
        pr_tracker.get_prs_for_branch.return_value = [
            Mock(number=10, state="merged", url="https://example.com/pr/10"),
        ]

        result = analyze_orphan_branches(
            issue_branches,
            set(),
            "owner/repo",
            issue_tracker=issue_tracker,
            pr_tracker=pr_tracker,
            commits_ahead_fn=lambda branch: 3,
            last_commit_date_fn=lambda branch: "2 days ago",
        )

        assert len(result) == 1
        assert result[0].has_closed_pr is True
        assert result[0].closed_pr_url == "https://example.com/pr/10"

    def test_analyze_orphan_branches_no_closed_prs_when_open(self):
        """Test when only open PRs exist."""
        issue_branches = {5: "5-test"}

        issue_tracker = Mock()
        issue_tracker.get_issue.return_value = Issue(number=5, title="Test", labels=[], state="open")
        pr_tracker = Mock()
        pr_tracker.get_prs_for_branch.return_value = [
            Mock(number=10, state="open", url="https://example.com/pr/10"),
        ]

        result = analyze_orphan_branches(
            issue_branches,
            set(),
            "owner/repo",
            issue_tracker=issue_tracker,
            pr_tracker=pr_tracker,
            commits_ahead_fn=lambda branch: 3,
            last_commit_date_fn=lambda branch: "2 days ago",
        )

        assert result[0].has_closed_pr is False
        assert result[0].closed_pr_url is None

    def test_analyze_orphan_branches_handles_gh_errors(self):
        """Test handling IssueTracker/PRTracker errors gracefully."""
        issue_branches = {5: "5-test"}

        issue_tracker = Mock()
        issue_tracker.get_issue.side_effect = Exception("issue error")
        pr_tracker = Mock()
        pr_tracker.get_prs_for_branch.side_effect = Exception("pr error")

        result = analyze_orphan_branches(
            issue_branches,
            set(),
            "owner/repo",
            issue_tracker=issue_tracker,
            pr_tracker=pr_tracker,
            commits_ahead_fn=lambda branch: 3,
            last_commit_date_fn=lambda branch: "2 days ago",
        )

        assert len(result) == 1
        assert result[0].issue_state is None
        assert result[0].issue_title is None
        assert result[0].has_closed_pr is False

    def test_analyze_orphan_branches_handles_pr_json_exception(self):
        """Test handling PR tracker exception."""
        issue_branches = {5: "5-test"}

        issue_tracker = Mock()
        issue_tracker.get_issue.return_value = Issue(number=5, title="Test", labels=[], state="open")
        pr_tracker = Mock()
        pr_tracker.get_prs_for_branch.side_effect = Exception("JSON parse error")

        result = analyze_orphan_branches(
            issue_branches,
            set(),
            "owner/repo",
            issue_tracker=issue_tracker,
            pr_tracker=pr_tracker,
            commits_ahead_fn=lambda branch: 3,
            last_commit_date_fn=lambda branch: "2 days ago",
        )

        # Should handle exception gracefully
        assert len(result) == 1
        assert result[0].has_closed_pr is False
        assert result[0].closed_pr_url is None

    def test_analyze_orphan_branches_sorts_by_priority(self):
        """Test orphan branches are sorted by action priority and commits."""
        issue_branches = {
            1: "1-resume",  # resume-work
            2: "2-investigate",  # investigate
            3: "3-delete",  # delete-branch
            4: "4-resume-more-commits",  # resume-work with more commits
        }

        commits_map = {
            "1-resume": 2,
            "4-resume-more-commits": 5,
            "2-investigate": 1,
            "3-delete": 0,
        }

        issue_tracker = Mock()
        def issue_lookup(number):
            if number in (1, 4):
                return Issue(number=number, title=f"Resume {number}", labels=[], state="open")
            if number == 3:
                return Issue(number=number, title="Delete", labels=[], state="closed")
            return None
        issue_tracker.get_issue.side_effect = issue_lookup
        pr_tracker = Mock()
        pr_tracker.get_prs_for_branch.return_value = []

        result = analyze_orphan_branches(
            issue_branches,
            set(),
            "owner/repo",
            issue_tracker=issue_tracker,
            pr_tracker=pr_tracker,
            commits_ahead_fn=lambda branch: commits_map.get(branch, 0),
            last_commit_date_fn=lambda branch: "1 day ago",
        )

        # Should be sorted: resume-work (more commits first), investigate, delete
        assert len(result) == 4
        assert result[0].issue_number == 4  # resume-work with 5 commits
        assert result[0].suggested_action == "resume-work"
        assert result[1].issue_number == 1  # resume-work with 2 commits
        assert result[1].suggested_action == "resume-work"
        assert result[2].issue_number == 2  # investigate
        assert result[2].suggested_action == "investigate"
        assert result[3].issue_number == 3  # delete-branch
        assert result[3].suggested_action == "delete-branch"

    def test_analyze_orphan_branches_handles_exception_in_parsing(self):
        """Test handling exceptions when parsing git/gh output."""
        issue_branches = {5: "5-test"}

        result = analyze_orphan_branches(
            issue_branches,
            set(),
            None,
            commits_ahead_fn=lambda branch: 0,
            last_commit_date_fn=lambda branch: (_ for _ in ()).throw(Exception("Git failed")),
        )

        # Should handle gracefully with defaults
        assert len(result) == 1
        assert result[0].commits_ahead == 0
        assert result[0].last_commit_date is None

    def test_analyze_orphan_branches_multiple_branches(self):
        """Test analyzing multiple orphan branches."""
        issue_branches = {5: "5-test", 10: "10-another"}

        result = analyze_orphan_branches(
            issue_branches,
            set(),
            None,
            commits_ahead_fn=lambda branch: 2 if branch == "5-test" else 1,
            last_commit_date_fn=lambda branch: "1 day ago",
        )

        assert len(result) == 2
        # Should be in order by commits (descending)
        assert result[0].issue_number in [5, 10]
        assert result[1].issue_number in [5, 10]
