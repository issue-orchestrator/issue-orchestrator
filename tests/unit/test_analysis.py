"""Unit tests for issue state analysis."""

import subprocess
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from issue_orchestrator.analysis import (
    IssueState,
    OrphanBranchState,
    get_issue_branches,
    analyze_issue,
    analyze_all_issues,
    analyze_orphan_branches,
)
from issue_orchestrator.models import Issue


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


class TestGetIssueBranches:
    """Test the get_issue_branches function."""

    @patch("subprocess.run")
    def test_get_issue_branches_success(self, mock_run):
        """Successfully parse branches from git output."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="  origin/5-add-feature\n  origin/12-fix-bug\n  origin/main\n",
        )

        result = get_issue_branches(Path("/fake/repo"))

        assert result == {5: "5-add-feature", 12: "12-fix-bug"}
        mock_run.assert_called_once_with(
            ["git", "branch", "-r", "--list", "origin/*"],
            capture_output=True,
            text=True,
            cwd=Path("/fake/repo"),
        )

    @patch("subprocess.run")
    def test_get_issue_branches_no_branches(self, mock_run):
        """Handle no issue branches found."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="  origin/main\n  origin/develop\n",
        )

        result = get_issue_branches(Path("/fake/repo"))

        assert result == {}

    @patch("subprocess.run")
    def test_get_issue_branches_empty_output(self, mock_run):
        """Handle empty git output."""
        mock_run.return_value = Mock(returncode=0, stdout="")

        result = get_issue_branches(Path("/fake/repo"))

        assert result == {}

    @patch("subprocess.run")
    def test_get_issue_branches_with_whitespace(self, mock_run):
        """Handle branches with extra whitespace."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="  origin/5-add-feature  \n  origin/12-fix-bug\n",
        )

        result = get_issue_branches(Path("/fake/repo"))

        assert result == {5: "5-add-feature", 12: "12-fix-bug"}

    @patch("subprocess.run")
    def test_get_issue_branches_ignores_non_numeric(self, mock_run):
        """Ignore branches that don't start with a number."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="  origin/5-add-feature\n  origin/feature-branch\n  origin/fix-something\n",
        )

        result = get_issue_branches(Path("/fake/repo"))

        assert result == {5: "5-add-feature"}

    @patch("subprocess.run")
    def test_get_issue_branches_handles_exception(self, mock_run):
        """Return empty dict on exception."""
        mock_run.side_effect = Exception("Git command failed")

        result = get_issue_branches(Path("/fake/repo"))

        assert result == {}

    @patch("subprocess.run")
    def test_get_issue_branches_handles_multidigit_numbers(self, mock_run):
        """Handle branches with multi-digit issue numbers."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="  origin/123-large-issue\n  origin/9999-huge-number\n",
        )

        result = get_issue_branches(Path("/fake/repo"))

        assert result == {123: "123-large-issue", 9999: "9999-huge-number"}


class TestAnalyzeIssue:
    """Test the analyze_issue function."""

    @patch("issue_orchestrator.analysis.get_open_prs_for_branch")
    def test_analyze_issue_basic(self, mock_get_prs):
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

    @patch("issue_orchestrator.analysis.get_open_prs_for_branch")
    def test_analyze_issue_with_session(self, mock_get_prs):
        """Test issue with active session."""
        issue = Issue(number=5, title="Test Issue", labels=[])
        check_session = Mock(return_value=True)

        result = analyze_issue(issue, "owner/repo", {}, check_session)

        assert result.has_session is True

    @patch("issue_orchestrator.analysis.get_open_prs_for_branch")
    def test_analyze_issue_with_branch_and_pr(self, mock_get_prs):
        """Test issue with branch and open PR."""
        issue = Issue(number=5, title="Test Issue", labels=[])
        check_session = Mock(return_value=False)

        mock_get_prs.return_value = [
            {"number": 10, "url": "https://github.com/owner/repo/pull/10"}
        ]

        result = analyze_issue(
            issue, "owner/repo", {5: "5-test-issue"}, check_session
        )

        assert result.branch == "5-test-issue"
        assert result.has_open_pr is True
        assert result.pr_url == "https://github.com/owner/repo/pull/10"
        mock_get_prs.assert_called_once_with("owner/repo", "5-test-issue")

    @patch("issue_orchestrator.analysis.get_open_prs_for_branch")
    def test_analyze_issue_with_branch_no_pr(self, mock_get_prs):
        """Test issue with branch but no open PR."""
        issue = Issue(number=5, title="Test Issue", labels=[])
        check_session = Mock(return_value=False)

        mock_get_prs.return_value = []

        result = analyze_issue(
            issue, "owner/repo", {5: "5-test-issue"}, check_session
        )

        assert result.branch == "5-test-issue"
        assert result.has_open_pr is False
        assert result.pr_url is None

    @patch("issue_orchestrator.analysis.get_open_prs_for_branch")
    def test_analyze_issue_no_repo_skips_pr_check(self, mock_get_prs):
        """Test that PR check is skipped when no repo provided."""
        issue = Issue(number=5, title="Test Issue", labels=[])
        check_session = Mock(return_value=False)

        result = analyze_issue(issue, None, {5: "5-test-issue"}, check_session)

        assert result.branch == "5-test-issue"
        assert result.has_open_pr is False
        mock_get_prs.assert_not_called()

    @patch("issue_orchestrator.analysis.get_open_prs_for_branch")
    def test_analyze_issue_pr_check_exception_ignored(self, mock_get_prs):
        """Test that exceptions in PR check are caught and ignored."""
        issue = Issue(number=5, title="Test Issue", labels=[])
        check_session = Mock(return_value=False)

        mock_get_prs.side_effect = Exception("API error")

        result = analyze_issue(
            issue, "owner/repo", {5: "5-test-issue"}, check_session
        )

        assert result.has_open_pr is False
        assert result.pr_url is None


class TestAnalyzeAllIssues:
    """Test the analyze_all_issues function."""

    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.analysis.analyze_issue")
    def test_analyze_all_issues_empty_list(self, mock_analyze, mock_get_branches):
        """Test analyzing empty issue list."""
        mock_get_branches.return_value = {}
        check_session = Mock()

        result = analyze_all_issues([], "owner/repo", Path("/repo"), check_session)

        assert result == []
        mock_get_branches.assert_called_once_with(Path("/repo"))
        mock_analyze.assert_not_called()

    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.analysis.analyze_issue")
    def test_analyze_all_issues_multiple_issues(
        self, mock_analyze, mock_get_branches
    ):
        """Test analyzing multiple issues."""
        issue1 = Issue(number=1, title="Issue 1", labels=[])
        issue2 = Issue(number=2, title="Issue 2", labels=[])
        issues = [issue1, issue2]

        mock_get_branches.return_value = {1: "1-issue-1"}

        state1 = IssueState(issue=issue1)
        state2 = IssueState(issue=issue2)
        mock_analyze.side_effect = [state1, state2]

        check_session = Mock()

        result = analyze_all_issues(
            issues, "owner/repo", Path("/repo"), check_session
        )

        assert len(result) == 2
        assert result[0] == state1
        assert result[1] == state2

        assert mock_analyze.call_count == 2
        mock_analyze.assert_any_call(
            issue1, "owner/repo", {1: "1-issue-1"}, check_session
        )
        mock_analyze.assert_any_call(
            issue2, "owner/repo", {1: "1-issue-1"}, check_session
        )

    @patch("issue_orchestrator.analysis.get_issue_branches")
    @patch("issue_orchestrator.analysis.analyze_issue")
    def test_analyze_all_issues_passes_branches_to_all(
        self, mock_analyze, mock_get_branches
    ):
        """Test that branches are fetched once and passed to all analyses."""
        issues = [
            Issue(number=1, title="Issue 1", labels=[]),
            Issue(number=2, title="Issue 2", labels=[]),
        ]

        branches = {1: "1-issue-1", 2: "2-issue-2"}
        mock_get_branches.return_value = branches

        mock_analyze.side_effect = [
            IssueState(issue=issues[0]),
            IssueState(issue=issues[1]),
        ]

        check_session = Mock()

        analyze_all_issues(issues, "owner/repo", Path("/repo"), check_session)

        # Branches fetched only once
        mock_get_branches.assert_called_once()

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

    @patch("subprocess.run")
    def test_analyze_orphan_branches_empty(self, mock_run):
        """Test with no orphan branches."""
        result = analyze_orphan_branches(
            {}, set(), "owner/repo", Path("/repo")
        )

        assert result == []
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_analyze_orphan_branches_skips_in_progress(self, mock_run):
        """Test that in-progress issues are skipped."""
        issue_branches = {1: "1-test", 2: "2-test"}
        in_progress = {1}

        result = analyze_orphan_branches(
            issue_branches, in_progress, None, Path("/repo")
        )

        # Only issue 2 should be analyzed
        assert len(result) == 1
        assert result[0].issue_number == 2

    @patch("subprocess.run")
    def test_analyze_orphan_branches_gets_commits_ahead(self, mock_run):
        """Test getting commits ahead of main."""
        issue_branches = {5: "5-test"}

        mock_run.side_effect = [
            Mock(returncode=0, stdout="3\n"),  # commits ahead
            Mock(returncode=0, stdout="2 days ago\n"),  # last commit date
        ]

        result = analyze_orphan_branches(
            issue_branches, set(), None, Path("/repo")
        )

        assert len(result) == 1
        assert result[0].commits_ahead == 3
        assert result[0].last_commit_date == "2 days ago"

        # Check git commands were called
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0][0][0] == [
            "git", "rev-list", "--count", "origin/main..origin/5-test"
        ]
        assert mock_run.call_args_list[1][0][0] == [
            "git", "log", "-1", "--format=%cr", "origin/5-test"
        ]

    @patch("subprocess.run")
    def test_analyze_orphan_branches_handles_git_errors(self, mock_run):
        """Test handling git command failures."""
        issue_branches = {5: "5-test"}

        mock_run.side_effect = [
            Mock(returncode=1, stdout=""),  # commits ahead fails
            Mock(returncode=1, stdout=""),  # last commit date fails
        ]

        result = analyze_orphan_branches(
            issue_branches, set(), None, Path("/repo")
        )

        assert len(result) == 1
        assert result[0].commits_ahead == 0  # default
        assert result[0].last_commit_date is None  # default

    @patch("subprocess.run")
    def test_analyze_orphan_branches_with_repo_checks_issue_state(self, mock_run):
        """Test checking issue state via gh CLI when repo provided."""
        issue_branches = {5: "5-test"}

        mock_run.side_effect = [
            Mock(returncode=0, stdout="3\n"),  # commits ahead
            Mock(returncode=0, stdout="2 days ago\n"),  # last commit date
            Mock(
                returncode=0,
                stdout='{"state": "OPEN", "title": "Test Issue"}\n'
            ),  # gh issue view
            Mock(returncode=0, stdout="[]\n"),  # gh pr list
        ]

        result = analyze_orphan_branches(
            issue_branches, set(), "owner/repo", Path("/repo")
        )

        assert len(result) == 1
        assert result[0].issue_state == "open"
        assert result[0].issue_title == "Test Issue"

        # Check gh commands
        gh_calls = [call for call in mock_run.call_args_list if "gh" in call[0][0]]
        assert len(gh_calls) == 2

    @patch("subprocess.run")
    def test_analyze_orphan_branches_checks_closed_prs(self, mock_run):
        """Test checking for closed PRs on the branch."""
        issue_branches = {5: "5-test"}

        mock_run.side_effect = [
            Mock(returncode=0, stdout="3\n"),  # commits ahead
            Mock(returncode=0, stdout="2 days ago\n"),  # last commit date
            Mock(
                returncode=0,
                stdout='{"state": "OPEN", "title": "Test"}\n'
            ),  # issue state
            Mock(
                returncode=0,
                stdout='[{"number": 10, "state": "MERGED", "url": "https://example.com/pr/10"}]\n'
            ),  # pr list
        ]

        result = analyze_orphan_branches(
            issue_branches, set(), "owner/repo", Path("/repo")
        )

        assert len(result) == 1
        assert result[0].has_closed_pr is True
        assert result[0].closed_pr_url == "https://example.com/pr/10"

    @patch("subprocess.run")
    def test_analyze_orphan_branches_no_closed_prs_when_open(self, mock_run):
        """Test when only open PRs exist."""
        issue_branches = {5: "5-test"}

        mock_run.side_effect = [
            Mock(returncode=0, stdout="3\n"),
            Mock(returncode=0, stdout="2 days ago\n"),
            Mock(returncode=0, stdout='{"state": "OPEN", "title": "Test"}\n'),
            Mock(
                returncode=0,
                stdout='[{"number": 10, "state": "OPEN", "url": "https://example.com/pr/10"}]\n'
            ),
        ]

        result = analyze_orphan_branches(
            issue_branches, set(), "owner/repo", Path("/repo")
        )

        assert result[0].has_closed_pr is False
        assert result[0].closed_pr_url is None

    @patch("subprocess.run")
    def test_analyze_orphan_branches_handles_gh_errors(self, mock_run):
        """Test handling gh CLI errors gracefully."""
        issue_branches = {5: "5-test"}

        mock_run.side_effect = [
            Mock(returncode=0, stdout="3\n"),
            Mock(returncode=0, stdout="2 days ago\n"),
            Mock(returncode=1, stdout=""),  # gh issue view fails
            Mock(returncode=1, stdout=""),  # gh pr list fails
        ]

        result = analyze_orphan_branches(
            issue_branches, set(), "owner/repo", Path("/repo")
        )

        assert len(result) == 1
        assert result[0].issue_state is None
        assert result[0].issue_title is None
        assert result[0].has_closed_pr is False

    @patch("subprocess.run")
    def test_analyze_orphan_branches_handles_pr_json_exception(self, mock_run):
        """Test handling JSON parsing exception in PR list."""
        issue_branches = {5: "5-test"}

        mock_run.side_effect = [
            Mock(returncode=0, stdout="3\n"),  # commits ahead
            Mock(returncode=0, stdout="2 days ago\n"),  # last commit date
            Mock(returncode=0, stdout='{"state": "OPEN", "title": "Test"}\n'),  # issue state
            Exception("JSON parse error"),  # PR list throws exception
        ]

        result = analyze_orphan_branches(
            issue_branches, set(), "owner/repo", Path("/repo")
        )

        # Should handle exception gracefully
        assert len(result) == 1
        assert result[0].has_closed_pr is False
        assert result[0].closed_pr_url is None

    @patch("subprocess.run")
    def test_analyze_orphan_branches_sorts_by_priority(self, mock_run):
        """Test orphan branches are sorted by action priority and commits."""
        issue_branches = {
            1: "1-resume",  # resume-work
            2: "2-investigate",  # investigate
            3: "3-delete",  # delete-branch
            4: "4-resume-more-commits",  # resume-work with more commits
        }

        # Mock responses for each branch
        def mock_git_responses(*args, **kwargs):
            cmd = args[0]
            if "rev-list" in cmd:
                if "1-resume" in cmd[3]:
                    return Mock(returncode=0, stdout="2\n")
                elif "4-resume-more-commits" in cmd[3]:
                    return Mock(returncode=0, stdout="5\n")
                elif "2-investigate" in cmd[3]:
                    return Mock(returncode=0, stdout="1\n")
                else:
                    return Mock(returncode=0, stdout="0\n")
            elif "log" in cmd:
                return Mock(returncode=0, stdout="1 day ago\n")
            elif "gh" in cmd[0] and "issue" in cmd[1]:
                if "1" in cmd[3]:
                    return Mock(returncode=0, stdout='{"state": "OPEN", "title": "Resume 1"}\n')
                elif "4" in cmd[3]:
                    return Mock(returncode=0, stdout='{"state": "OPEN", "title": "Resume 4"}\n')
                elif "2" in cmd[3]:
                    return Mock(returncode=0, stdout='{"state": null, "title": null}\n')
                else:
                    return Mock(returncode=0, stdout='{"state": "CLOSED", "title": "Delete"}\n')
            elif "gh" in cmd[0] and "pr" in cmd[1]:
                return Mock(returncode=0, stdout="[]\n")

        mock_run.side_effect = mock_git_responses

        result = analyze_orphan_branches(
            issue_branches, set(), "owner/repo", Path("/repo")
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

    @patch("subprocess.run")
    def test_analyze_orphan_branches_handles_exception_in_parsing(self, mock_run):
        """Test handling exceptions when parsing git/gh output."""
        issue_branches = {5: "5-test"}

        # First call succeeds, second raises exception
        mock_run.side_effect = [
            Mock(returncode=0, stdout="invalid\n"),  # not a number
            Exception("Git failed"),  # exception on second call
        ]

        result = analyze_orphan_branches(
            issue_branches, set(), None, Path("/repo")
        )

        # Should handle gracefully with defaults
        assert len(result) == 1
        assert result[0].commits_ahead == 0
        assert result[0].last_commit_date is None

    @patch("subprocess.run")
    def test_analyze_orphan_branches_multiple_branches(self, mock_run):
        """Test analyzing multiple orphan branches."""
        issue_branches = {5: "5-test", 10: "10-another"}

        call_count = [0]

        def mock_responses(*args, **kwargs):
            cmd = args[0]
            call_count[0] += 1

            if "rev-list" in cmd:
                if "5-test" in cmd[3]:
                    return Mock(returncode=0, stdout="2\n")
                else:
                    return Mock(returncode=0, stdout="1\n")
            elif "log" in cmd:
                return Mock(returncode=0, stdout="1 day ago\n")

        mock_run.side_effect = mock_responses

        result = analyze_orphan_branches(
            issue_branches, set(), None, Path("/repo")
        )

        assert len(result) == 2
        # Should be in order by commits (descending)
        assert result[0].issue_number in [5, 10]
        assert result[1].issue_number in [5, 10]
