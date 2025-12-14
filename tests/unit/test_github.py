"""Unit tests for GitHub operations."""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from issue_orchestrator.github import (
    GitHubError,
    _run_gh,
    _run_gh_json,
    get_repo,
    list_issues,
    add_label,
    remove_label,
    add_comment,
    get_open_prs_for_branch,
    get_issue_comments,
    get_latest_blocked_info,
    get_latest_needs_human_info,
    BlockedInfo,
    NeedsHumanInfo,
    _extract_field,
    _extract_issue_numbers,
    _extract_numbered_list,
)
from issue_orchestrator.models import Issue


class TestRunGh:
    """Test _run_gh helper function."""

    @patch("issue_orchestrator.github.subprocess.run")
    def test_run_gh_success(self, mock_run):
        """Test successful gh command execution."""
        mock_run.return_value = Mock(returncode=0, stdout="output", stderr="")

        result = _run_gh(["issue", "list"])

        assert result == "output"
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == ["gh", "issue", "list"]

    @patch("issue_orchestrator.github.subprocess.run")
    def test_run_gh_with_repo(self, mock_run):
        """Test gh command with repo argument."""
        mock_run.return_value = Mock(returncode=0, stdout="output", stderr="")

        result = _run_gh(["issue", "list"], repo="owner/repo")

        assert result == "output"
        args = mock_run.call_args[0][0]
        assert args == ["gh", "issue", "list", "--repo", "owner/repo"]

    @patch("issue_orchestrator.github.subprocess.run")
    def test_run_gh_failure(self, mock_run):
        """Test gh command failure raises GitHubError."""
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="error message")

        with pytest.raises(GitHubError, match="gh command failed: error message"):
            _run_gh(["issue", "list"])

    @patch("issue_orchestrator.github.subprocess.run")
    def test_run_gh_captures_output(self, mock_run):
        """Test that subprocess.run is called with correct parameters."""
        mock_run.return_value = Mock(returncode=0, stdout="output", stderr="")

        _run_gh(["pr", "view"])

        mock_run.assert_called_once()
        kwargs = mock_run.call_args[1]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True


class TestRunGhJson:
    """Test _run_gh_json helper function."""

    @patch("issue_orchestrator.github._run_gh")
    def test_run_gh_json_success(self, mock_run_gh):
        """Test successful JSON parsing."""
        mock_run_gh.return_value = '{"key": "value"}'

        result = _run_gh_json(["issue", "view", "123"])

        assert result == {"key": "value"}

    @patch("issue_orchestrator.github._run_gh")
    def test_run_gh_json_list(self, mock_run_gh):
        """Test parsing JSON list."""
        mock_run_gh.return_value = '[{"number": 1}, {"number": 2}]'

        result = _run_gh_json(["issue", "list"])

        assert result == [{"number": 1}, {"number": 2}]

    @patch("issue_orchestrator.github._run_gh")
    def test_run_gh_json_invalid_json(self, mock_run_gh):
        """Test invalid JSON raises GitHubError."""
        mock_run_gh.return_value = "not valid json"

        with pytest.raises(GitHubError, match="Failed to parse gh JSON output"):
            _run_gh_json(["issue", "view", "123"])

    @patch("issue_orchestrator.github._run_gh")
    def test_run_gh_json_propagates_github_error(self, mock_run_gh):
        """Test that GitHubError from _run_gh is propagated."""
        mock_run_gh.side_effect = GitHubError("gh failed")

        with pytest.raises(GitHubError, match="gh failed"):
            _run_gh_json(["issue", "list"])


class TestGetRepo:
    """Test get_repo function."""

    @patch("issue_orchestrator.github.subprocess.run")
    def test_get_repo_https_url(self, mock_run):
        """Test extracting repo from HTTPS URL."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="https://github.com/owner/repo.git\n",
            stderr=""
        )

        result = get_repo()

        assert result == "owner/repo"

    @patch("issue_orchestrator.github.subprocess.run")
    def test_get_repo_https_url_no_git_suffix(self, mock_run):
        """Test HTTPS URL without .git suffix."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="https://github.com/owner/repo\n",
            stderr=""
        )

        result = get_repo()

        assert result == "owner/repo"

    @patch("issue_orchestrator.github.subprocess.run")
    def test_get_repo_ssh_url(self, mock_run):
        """Test extracting repo from SSH URL."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="git@github.com:owner/repo.git\n",
            stderr=""
        )

        result = get_repo()

        assert result == "owner/repo"

    @patch("issue_orchestrator.github.subprocess.run")
    def test_get_repo_ssh_url_no_git_suffix(self, mock_run):
        """Test SSH URL without .git suffix."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="git@github.com:owner/repo\n",
            stderr=""
        )

        result = get_repo()

        assert result == "owner/repo"

    @patch("issue_orchestrator.github.subprocess.run")
    def test_get_repo_git_command_fails(self, mock_run):
        """Test git command failure raises GitHubError."""
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="not a git repo")

        with pytest.raises(GitHubError, match="Could not determine repository"):
            get_repo()

    @patch("issue_orchestrator.github.subprocess.run")
    def test_get_repo_unrecognized_url(self, mock_run):
        """Test unrecognized URL format raises GitHubError."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="https://gitlab.com/owner/repo.git\n",
            stderr=""
        )

        with pytest.raises(GitHubError, match="Unrecognized GitHub remote URL"):
            get_repo()

    @patch("issue_orchestrator.github.subprocess.run")
    def test_get_repo_exception_handling(self, mock_run):
        """Test exception handling wraps non-GitHubError exceptions."""
        mock_run.side_effect = RuntimeError("unexpected error")

        with pytest.raises(GitHubError, match="Failed to get repository"):
            get_repo()


class TestListIssues:
    """Test list_issues function."""

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_issues_basic(self, mock_run_gh_json):
        """Test basic issue listing."""
        mock_run_gh_json.return_value = [
            {
                "number": 1,
                "title": "Test Issue",
                "labels": [{"name": "bug"}],
                "state": "open",
                "body": "Test body",
                "milestone": None,
            }
        ]

        issues = list_issues()

        assert len(issues) == 1
        assert isinstance(issues[0], Issue)
        assert issues[0].number == 1
        assert issues[0].title == "Test Issue"
        assert issues[0].labels == ["bug"]
        assert issues[0].state == "open"
        assert issues[0].body == "Test body"
        assert issues[0].milestone is None

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_issues_with_milestone(self, mock_run_gh_json):
        """Test issue with milestone."""
        mock_run_gh_json.return_value = [
            {
                "number": 2,
                "title": "Milestone Issue",
                "labels": [],
                "state": "open",
                "body": None,
                "milestone": {"title": "v1.0"},
            }
        ]

        issues = list_issues()

        assert len(issues) == 1
        assert issues[0].milestone == "v1.0"

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_issues_with_labels_filter(self, mock_run_gh_json):
        """Test issue listing with label filter."""
        mock_run_gh_json.return_value = []

        list_issues(labels=["bug", "priority:high"])

        args = mock_run_gh_json.call_args[0][0]
        assert "--label" in args
        assert "bug" in args
        assert "priority:high" in args

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_issues_with_state(self, mock_run_gh_json):
        """Test issue listing with state filter."""
        mock_run_gh_json.return_value = []

        list_issues(state="closed")

        args = mock_run_gh_json.call_args[0][0]
        assert "--state" in args
        assert "closed" in args

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_issues_with_milestone_filter(self, mock_run_gh_json):
        """Test issue listing with milestone filter."""
        mock_run_gh_json.return_value = []

        list_issues(milestone="v1.0")

        args = mock_run_gh_json.call_args[0][0]
        assert "--milestone" in args
        assert "v1.0" in args

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_issues_with_repo(self, mock_run_gh_json):
        """Test issue listing with repo argument."""
        mock_run_gh_json.return_value = []

        list_issues(repo="owner/repo")

        # repo is passed as positional argument to _run_gh_json
        assert mock_run_gh_json.call_args[0][1] == "owner/repo"

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_issues_empty_labels(self, mock_run_gh_json):
        """Test issue with empty labels list."""
        mock_run_gh_json.return_value = [
            {
                "number": 3,
                "title": "No Labels",
                "labels": [],
                "state": "open",
                "body": None,
                "milestone": None,
            }
        ]

        issues = list_issues()

        assert len(issues) == 1
        assert issues[0].labels == []

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_issues_multiple_labels(self, mock_run_gh_json):
        """Test issue with multiple labels."""
        mock_run_gh_json.return_value = [
            {
                "number": 4,
                "title": "Multi Label",
                "labels": [{"name": "bug"}, {"name": "priority:high"}, {"name": "agent:web"}],
                "state": "open",
                "body": None,
                "milestone": None,
            }
        ]

        issues = list_issues()

        assert len(issues) == 1
        assert issues[0].labels == ["bug", "priority:high", "agent:web"]

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_issues_empty_result(self, mock_run_gh_json):
        """Test empty issue list."""
        mock_run_gh_json.return_value = []

        issues = list_issues()

        assert issues == []

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_issues_non_list_result(self, mock_run_gh_json):
        """Test non-list JSON result returns empty list."""
        mock_run_gh_json.return_value = {"error": "something"}

        issues = list_issues()

        assert issues == []

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_issues_github_error_propagated(self, mock_run_gh_json):
        """Test GitHubError is propagated."""
        mock_run_gh_json.side_effect = GitHubError("gh failed")

        with pytest.raises(GitHubError, match="gh failed"):
            list_issues()

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_issues_exception_wrapped(self, mock_run_gh_json):
        """Test unexpected exceptions are wrapped in GitHubError."""
        mock_run_gh_json.side_effect = KeyError("number")

        with pytest.raises(GitHubError, match="Failed to list issues"):
            list_issues()


class TestAddLabel:
    """Test add_label function."""

    @patch("issue_orchestrator.github._run_gh")
    def test_add_label_success(self, mock_run_gh):
        """Test successful label addition."""
        mock_run_gh.return_value = ""

        add_label(issue_number=123, label="bug")

        args = mock_run_gh.call_args[0][0]
        assert args == ["issue", "edit", "123", "--add-label", "bug"]

    @patch("issue_orchestrator.github._run_gh")
    def test_add_label_with_repo(self, mock_run_gh):
        """Test label addition with repo argument."""
        mock_run_gh.return_value = ""

        add_label(repo="owner/repo", issue_number=123, label="bug")

        # repo is passed as positional argument to _run_gh
        assert mock_run_gh.call_args[0][1] == "owner/repo"

    def test_add_label_missing_issue_number(self):
        """Test ValueError when issue_number is None."""
        with pytest.raises(ValueError, match="issue_number is required"):
            add_label(issue_number=None, label="bug")

    def test_add_label_missing_label(self):
        """Test ValueError when label is None."""
        with pytest.raises(ValueError, match="label is required"):
            add_label(issue_number=123, label=None)

    @patch("issue_orchestrator.github._run_gh")
    def test_add_label_github_error_propagated(self, mock_run_gh):
        """Test GitHubError is propagated."""
        mock_run_gh.side_effect = GitHubError("gh failed")

        with pytest.raises(GitHubError, match="gh failed"):
            add_label(issue_number=123, label="bug")

    @patch("issue_orchestrator.github._run_gh")
    def test_add_label_exception_wrapped(self, mock_run_gh):
        """Test unexpected exceptions are wrapped in GitHubError."""
        mock_run_gh.side_effect = RuntimeError("unexpected")

        with pytest.raises(GitHubError, match="Failed to add label"):
            add_label(issue_number=123, label="bug")


class TestRemoveLabel:
    """Test remove_label function."""

    @patch("issue_orchestrator.github._run_gh")
    def test_remove_label_success(self, mock_run_gh):
        """Test successful label removal."""
        mock_run_gh.return_value = ""

        remove_label(issue_number=123, label="bug")

        args = mock_run_gh.call_args[0][0]
        assert args == ["issue", "edit", "123", "--remove-label", "bug"]

    @patch("issue_orchestrator.github._run_gh")
    def test_remove_label_with_repo(self, mock_run_gh):
        """Test label removal with repo argument."""
        mock_run_gh.return_value = ""

        remove_label(repo="owner/repo", issue_number=123, label="bug")

        # repo is passed as positional argument to _run_gh
        assert mock_run_gh.call_args[0][1] == "owner/repo"

    def test_remove_label_missing_issue_number(self):
        """Test ValueError when issue_number is None."""
        with pytest.raises(ValueError, match="issue_number is required"):
            remove_label(issue_number=None, label="bug")

    def test_remove_label_missing_label(self):
        """Test ValueError when label is None."""
        with pytest.raises(ValueError, match="label is required"):
            remove_label(issue_number=123, label=None)

    @patch("issue_orchestrator.github._run_gh")
    def test_remove_label_github_error_propagated(self, mock_run_gh):
        """Test GitHubError is propagated."""
        mock_run_gh.side_effect = GitHubError("gh failed")

        with pytest.raises(GitHubError, match="gh failed"):
            remove_label(issue_number=123, label="bug")

    @patch("issue_orchestrator.github._run_gh")
    def test_remove_label_exception_wrapped(self, mock_run_gh):
        """Test unexpected exceptions are wrapped in GitHubError."""
        mock_run_gh.side_effect = RuntimeError("unexpected")

        with pytest.raises(GitHubError, match="Failed to remove label"):
            remove_label(issue_number=123, label="bug")


class TestAddComment:
    """Test add_comment function."""

    @patch("issue_orchestrator.github._run_gh")
    def test_add_comment_success(self, mock_run_gh):
        """Test successful comment addition."""
        mock_run_gh.return_value = ""

        add_comment(issue_number=123, body="Test comment")

        args = mock_run_gh.call_args[0][0]
        assert args == ["issue", "comment", "123", "--body", "Test comment"]

    @patch("issue_orchestrator.github._run_gh")
    def test_add_comment_with_repo(self, mock_run_gh):
        """Test comment addition with repo argument."""
        mock_run_gh.return_value = ""

        add_comment(repo="owner/repo", issue_number=123, body="Test comment")

        # repo is passed as positional argument to _run_gh
        assert mock_run_gh.call_args[0][1] == "owner/repo"

    def test_add_comment_missing_issue_number(self):
        """Test ValueError when issue_number is None."""
        with pytest.raises(ValueError, match="issue_number is required"):
            add_comment(issue_number=None, body="Test")

    def test_add_comment_missing_body(self):
        """Test ValueError when body is None."""
        with pytest.raises(ValueError, match="body is required"):
            add_comment(issue_number=123, body=None)

    @patch("issue_orchestrator.github._run_gh")
    def test_add_comment_github_error_propagated(self, mock_run_gh):
        """Test GitHubError is propagated."""
        mock_run_gh.side_effect = GitHubError("gh failed")

        with pytest.raises(GitHubError, match="gh failed"):
            add_comment(issue_number=123, body="Test")

    @patch("issue_orchestrator.github._run_gh")
    def test_add_comment_exception_wrapped(self, mock_run_gh):
        """Test unexpected exceptions are wrapped in GitHubError."""
        mock_run_gh.side_effect = RuntimeError("unexpected")

        with pytest.raises(GitHubError, match="Failed to add comment"):
            add_comment(issue_number=123, body="Test")


class TestGetOpenPrsForBranch:
    """Test get_open_prs_for_branch function."""

    @patch("issue_orchestrator.github._run_gh_json")
    def test_get_open_prs_success(self, mock_run_gh_json):
        """Test successful PR retrieval."""
        mock_run_gh_json.return_value = [
            {"number": 1, "title": "Test PR", "url": "https://github.com/owner/repo/pull/1"}
        ]

        prs = get_open_prs_for_branch(branch="feature-branch")

        assert len(prs) == 1
        assert prs[0]["number"] == 1
        assert prs[0]["title"] == "Test PR"

    @patch("issue_orchestrator.github._run_gh_json")
    def test_get_open_prs_with_repo(self, mock_run_gh_json):
        """Test PR retrieval with repo argument."""
        mock_run_gh_json.return_value = []

        get_open_prs_for_branch(repo="owner/repo", branch="feature-branch")

        args = mock_run_gh_json.call_args[0][0]
        assert args == ["pr", "list", "--head", "feature-branch", "--state", "open", "--json", "number,title,url"]
        # repo is passed as positional argument to _run_gh_json
        assert mock_run_gh_json.call_args[0][1] == "owner/repo"

    @patch("issue_orchestrator.github._run_gh_json")
    def test_get_open_prs_empty_result(self, mock_run_gh_json):
        """Test empty PR list."""
        mock_run_gh_json.return_value = []

        prs = get_open_prs_for_branch(branch="feature-branch")

        assert prs == []

    @patch("issue_orchestrator.github._run_gh_json")
    def test_get_open_prs_non_list_result(self, mock_run_gh_json):
        """Test non-list JSON result returns empty list."""
        mock_run_gh_json.return_value = {"error": "something"}

        prs = get_open_prs_for_branch(branch="feature-branch")

        assert prs == []

    def test_get_open_prs_missing_branch(self):
        """Test ValueError when branch is None."""
        with pytest.raises(ValueError, match="branch is required"):
            get_open_prs_for_branch(branch=None)

    @patch("issue_orchestrator.github._run_gh_json")
    def test_get_open_prs_github_error_propagated(self, mock_run_gh_json):
        """Test GitHubError is propagated."""
        mock_run_gh_json.side_effect = GitHubError("gh failed")

        with pytest.raises(GitHubError, match="gh failed"):
            get_open_prs_for_branch(branch="feature-branch")

    @patch("issue_orchestrator.github._run_gh_json")
    def test_get_open_prs_exception_wrapped(self, mock_run_gh_json):
        """Test unexpected exceptions are wrapped in GitHubError."""
        mock_run_gh_json.side_effect = RuntimeError("unexpected")

        with pytest.raises(GitHubError, match="Failed to get open PRs"):
            get_open_prs_for_branch(branch="feature-branch")


class TestGetIssueComments:
    """Test get_issue_comments function."""

    @patch("issue_orchestrator.github._run_gh_json")
    def test_get_issue_comments_success(self, mock_run_gh_json):
        """Test successful comment retrieval."""
        mock_run_gh_json.return_value = {
            "comments": [
                {"body": "First comment", "url": "https://github.com/..."},
                {"body": "Second comment", "url": "https://github.com/..."},
            ]
        }

        comments = get_issue_comments(repo=None, issue_number=123)

        assert len(comments) == 2
        assert comments[0]["body"] == "First comment"
        assert comments[1]["body"] == "Second comment"

    @patch("issue_orchestrator.github._run_gh_json")
    def test_get_issue_comments_empty(self, mock_run_gh_json):
        """Test issue with no comments."""
        mock_run_gh_json.return_value = {"comments": []}

        comments = get_issue_comments(repo=None, issue_number=123)

        assert comments == []

    @patch("issue_orchestrator.github._run_gh_json")
    def test_get_issue_comments_missing_field(self, mock_run_gh_json):
        """Test issue view output without comments field."""
        mock_run_gh_json.return_value = {}

        comments = get_issue_comments(repo=None, issue_number=123)

        assert comments == []

    @patch("issue_orchestrator.github._run_gh_json")
    def test_get_issue_comments_with_repo(self, mock_run_gh_json):
        """Test comment retrieval with repo argument."""
        mock_run_gh_json.return_value = {"comments": []}

        get_issue_comments(repo="owner/repo", issue_number=123)

        args = mock_run_gh_json.call_args[0][0]
        assert args == ["issue", "view", "123", "--json", "comments"]
        # repo is passed as positional argument to _run_gh_json
        assert mock_run_gh_json.call_args[0][1] == "owner/repo"


class TestExtractField:
    """Test _extract_field helper function."""

    def test_extract_field_basic(self):
        """Test basic field extraction."""
        body = "**Reason:** This is blocked\n**Other:** value"
        result = _extract_field(body, "Reason")
        assert result == "This is blocked"

    def test_extract_field_multiline(self):
        """Test multiline field value."""
        body = "**Reason:** Line 1\nLine 2\nLine 3\n**Next:** value"
        result = _extract_field(body, "Reason")
        assert result == "Line 1\nLine 2\nLine 3"

    def test_extract_field_with_heading_after(self):
        """Test field followed by heading."""
        body = "**Reason:** Some text\n## Next Section"
        result = _extract_field(body, "Reason")
        assert result == "Some text"

    def test_extract_field_at_end(self):
        """Test field at end of body."""
        body = "**Reason:** Final value"
        result = _extract_field(body, "Reason")
        assert result == "Final value"

    def test_extract_field_not_found(self):
        """Test field not found returns None."""
        body = "**Other:** value"
        result = _extract_field(body, "Reason")
        assert result is None

    def test_extract_field_case_insensitive(self):
        """Test field extraction is case insensitive."""
        body = "**reason:** value"
        result = _extract_field(body, "Reason")
        assert result == "value"

    def test_extract_field_with_double_newline(self):
        """Test field followed by double newline."""
        body = "**Reason:** Some text\n\nNew paragraph"
        result = _extract_field(body, "Reason")
        assert result == "Some text"


class TestExtractIssueNumbers:
    """Test _extract_issue_numbers helper function."""

    def test_extract_issue_numbers_single(self):
        """Test extracting single issue number."""
        body = "**Blocked by:** #123"
        result = _extract_issue_numbers(body, "Blocked by")
        assert result == [123]

    def test_extract_issue_numbers_multiple(self):
        """Test extracting multiple issue numbers."""
        body = "**Blocked by:** #123, #456, #789"
        result = _extract_issue_numbers(body, "Blocked by")
        assert result == [123, 456, 789]

    def test_extract_issue_numbers_with_text(self):
        """Test extracting issues with surrounding text."""
        body = "**Blocked by:** Waiting for #123 and #456 to be resolved"
        result = _extract_issue_numbers(body, "Blocked by")
        assert result == [123, 456]

    def test_extract_issue_numbers_none_found(self):
        """Test no issue numbers found."""
        body = "**Blocked by:** No issues"
        result = _extract_issue_numbers(body, "Blocked by")
        assert result == []

    def test_extract_issue_numbers_field_not_found(self):
        """Test field not found returns empty list."""
        body = "**Other:** #123"
        result = _extract_issue_numbers(body, "Blocked by")
        assert result == []


class TestExtractNumberedList:
    """Test _extract_numbered_list helper function."""

    def test_extract_numbered_list_basic(self):
        """Test basic numbered list extraction."""
        body = "**Options:**\n1. First option\n2. Second option\n3. Third option"
        result = _extract_numbered_list(body, "Options")
        assert result == ["First option", "Second option", "Third option"]

    def test_extract_numbered_list_with_spaces(self):
        """Test numbered list with extra spaces."""
        body = "**Options:**\n1.   First option  \n2.  Second option"
        result = _extract_numbered_list(body, "Options")
        assert result == ["First option", "Second option"]

    def test_extract_numbered_list_multiline_items(self):
        """Test numbered list items on single lines."""
        body = "**Options:**\n1. First option\n2. Second option"
        result = _extract_numbered_list(body, "Options")
        assert result == ["First option", "Second option"]

    def test_extract_numbered_list_not_found(self):
        """Test field not found returns empty list."""
        body = "**Other:** text"
        result = _extract_numbered_list(body, "Options")
        assert result == []

    def test_extract_numbered_list_no_list_after_field(self):
        """Test field without numbered list."""
        body = "**Options:**\nSome text without numbers"
        result = _extract_numbered_list(body, "Options")
        assert result == []


class TestGetLatestBlockedInfo:
    """Test get_latest_blocked_info function."""

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_blocked_info_success(self, mock_get_comments):
        """Test successful parsing of blocked info."""
        mock_get_comments.return_value = [
            {
                "body": """## 🚧 Blocked

**Reason:** Missing API key
**Blocked by:** #123, #456
**Attempted:** Tried to continue without it
**Unblock action:** Get API key from admin
""",
                "url": "https://github.com/owner/repo/issues/1#issuecomment-1",
                "createdAt": "2024-01-01T12:00:00Z",
            }
        ]

        info = get_latest_blocked_info(repo=None, issue_number=1)

        assert info is not None
        assert info.reason == "Missing API key"
        assert info.blocked_by == [123, 456]
        assert info.attempted == "Tried to continue without it"
        assert info.unblock_action == "Get API key from admin"
        assert info.comment_url == "https://github.com/owner/repo/issues/1#issuecomment-1"
        assert info.timestamp == "2024-01-01T12:00:00Z"

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_blocked_info_no_emoji(self, mock_get_comments):
        """Test parsing blocked section without emoji."""
        mock_get_comments.return_value = [
            {
                "body": """## Blocked

**Reason:** Test reason
**Attempted:** Test attempt
**Unblock action:** Test action
""",
                "url": "https://url",
                "createdAt": "2024-01-01T12:00:00Z",
            }
        ]

        info = get_latest_blocked_info(repo=None, issue_number=1)

        assert info is not None
        assert info.reason == "Test reason"

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_blocked_info_latest_comment(self, mock_get_comments):
        """Test that latest comment is parsed (reversed order)."""
        mock_get_comments.return_value = [
            {
                "body": """## 🚧 Blocked
**Reason:** Old reason
**Attempted:** Old attempt
**Unblock action:** Old action
""",
                "url": "https://url1",
                "createdAt": "2024-01-01T12:00:00Z",
            },
            {
                "body": """## 🚧 Blocked
**Reason:** New reason
**Attempted:** New attempt
**Unblock action:** New action
""",
                "url": "https://url2",
                "createdAt": "2024-01-02T12:00:00Z",
            },
        ]

        info = get_latest_blocked_info(repo=None, issue_number=1)

        assert info is not None
        assert info.reason == "New reason"

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_blocked_info_no_blocked_by(self, mock_get_comments):
        """Test blocked info without blocked_by field."""
        mock_get_comments.return_value = [
            {
                "body": """## 🚧 Blocked
**Reason:** Test
**Attempted:** Test
**Unblock action:** Test
""",
                "url": "https://url",
                "createdAt": "2024-01-01T12:00:00Z",
            }
        ]

        info = get_latest_blocked_info(repo=None, issue_number=1)

        assert info is not None
        assert info.blocked_by == []

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_blocked_info_not_found(self, mock_get_comments):
        """Test no blocked section found."""
        mock_get_comments.return_value = [
            {"body": "Regular comment", "url": "https://url", "createdAt": "2024-01-01T12:00:00Z"}
        ]

        info = get_latest_blocked_info(repo=None, issue_number=1)

        assert info is None

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_blocked_info_empty_comments(self, mock_get_comments):
        """Test empty comments list."""
        mock_get_comments.return_value = []

        info = get_latest_blocked_info(repo=None, issue_number=1)

        assert info is None

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_blocked_info_defaults(self, mock_get_comments):
        """Test default values when fields are missing."""
        mock_get_comments.return_value = [
            {
                "body": "## 🚧 Blocked\n",
                "url": "https://url",
                "createdAt": "2024-01-01T12:00:00Z",
            }
        ]

        info = get_latest_blocked_info(repo=None, issue_number=1)

        assert info is not None
        assert info.reason == "Unknown"
        assert info.blocked_by == []
        assert info.attempted == ""
        assert info.unblock_action == ""


class TestGetLatestNeedsHumanInfo:
    """Test get_latest_needs_human_info function."""

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_needs_human_info_success(self, mock_get_comments):
        """Test successful parsing of needs-human info."""
        mock_get_comments.return_value = [
            {
                "body": """## ❓ Needs Human

**Question:** Which approach should we use?
**Context:** We have two options available
**Options:**
1. Option A
2. Option B
3. Option C
**Default if no response:** Use Option A
""",
                "url": "https://github.com/owner/repo/issues/1#issuecomment-1",
                "createdAt": "2024-01-01T12:00:00Z",
            }
        ]

        info = get_latest_needs_human_info(repo=None, issue_number=1)

        assert info is not None
        assert info.question == "Which approach should we use?"
        assert info.context == "We have two options available"
        assert info.options == ["Option A", "Option B", "Option C"]
        assert info.default_action == "Use Option A"
        assert info.comment_url == "https://github.com/owner/repo/issues/1#issuecomment-1"
        assert info.timestamp == "2024-01-01T12:00:00Z"

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_needs_human_info_no_emoji(self, mock_get_comments):
        """Test parsing needs-human section without emoji."""
        mock_get_comments.return_value = [
            {
                "body": """## Needs Human
**Question:** Test question?
**Context:** Test context
**Options:**
1. Option 1
**Default if no response:** Option 1
""",
                "url": "https://url",
                "createdAt": "2024-01-01T12:00:00Z",
            }
        ]

        info = get_latest_needs_human_info(repo=None, issue_number=1)

        assert info is not None
        assert info.question == "Test question?"

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_needs_human_info_latest_comment(self, mock_get_comments):
        """Test that latest comment is parsed."""
        mock_get_comments.return_value = [
            {
                "body": """## ❓ Needs Human
**Question:** Old question?
**Context:** Old
**Options:**
1. Old option
**Default if no response:** Old
""",
                "url": "https://url1",
                "createdAt": "2024-01-01T12:00:00Z",
            },
            {
                "body": """## ❓ Needs Human
**Question:** New question?
**Context:** New
**Options:**
1. New option
**Default if no response:** New
""",
                "url": "https://url2",
                "createdAt": "2024-01-02T12:00:00Z",
            },
        ]

        info = get_latest_needs_human_info(repo=None, issue_number=1)

        assert info is not None
        assert info.question == "New question?"

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_needs_human_info_not_found(self, mock_get_comments):
        """Test no needs-human section found."""
        mock_get_comments.return_value = [
            {"body": "Regular comment", "url": "https://url", "createdAt": "2024-01-01T12:00:00Z"}
        ]

        info = get_latest_needs_human_info(repo=None, issue_number=1)

        assert info is None

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_needs_human_info_empty_comments(self, mock_get_comments):
        """Test empty comments list."""
        mock_get_comments.return_value = []

        info = get_latest_needs_human_info(repo=None, issue_number=1)

        assert info is None

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_needs_human_info_defaults(self, mock_get_comments):
        """Test default values when fields are missing."""
        mock_get_comments.return_value = [
            {
                "body": "## ❓ Needs Human\n",
                "url": "https://url",
                "createdAt": "2024-01-01T12:00:00Z",
            }
        ]

        info = get_latest_needs_human_info(repo=None, issue_number=1)

        assert info is not None
        assert info.question == "Unknown question"
        assert info.context == ""
        assert info.options == []
        assert info.default_action == ""

    @patch("issue_orchestrator.github.get_issue_comments")
    def test_get_latest_needs_human_info_empty_options(self, mock_get_comments):
        """Test needs-human with no options listed."""
        mock_get_comments.return_value = [
            {
                "body": """## ❓ Needs Human
**Question:** Test?
**Context:** Test
**Options:**
**Default if no response:** Continue
""",
                "url": "https://url",
                "createdAt": "2024-01-01T12:00:00Z",
            }
        ]

        info = get_latest_needs_human_info(repo=None, issue_number=1)

        assert info is not None
        assert info.options == []


class TestListPrsWithLabel:
    """Test list_prs_with_label function."""

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_prs_with_label_success(self, mock_run_gh_json):
        """Test successful PR listing by label."""
        from issue_orchestrator.github import list_prs_with_label

        mock_run_gh_json.return_value = [
            {"number": 1, "title": "PR 1", "url": "https://github.com/owner/repo/pull/1"},
            {"number": 2, "title": "PR 2", "url": "https://github.com/owner/repo/pull/2"},
        ]

        prs = list_prs_with_label("owner/repo", "needs-review")

        assert len(prs) == 2
        assert prs[0]["number"] == 1
        assert prs[1]["number"] == 2

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_prs_with_label_empty(self, mock_run_gh_json):
        """Test empty PR list."""
        from issue_orchestrator.github import list_prs_with_label

        mock_run_gh_json.return_value = []

        prs = list_prs_with_label("owner/repo", "needs-review")

        assert prs == []

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_prs_with_label_error_returns_empty(self, mock_run_gh_json):
        """Test that errors return empty list."""
        from issue_orchestrator.github import list_prs_with_label

        mock_run_gh_json.side_effect = GitHubError("gh failed")

        prs = list_prs_with_label("owner/repo", "needs-review")

        assert prs == []

    @patch("issue_orchestrator.github._run_gh_json")
    def test_list_prs_with_label_non_list_returns_empty(self, mock_run_gh_json):
        """Test non-list result returns empty list."""
        from issue_orchestrator.github import list_prs_with_label

        mock_run_gh_json.return_value = {"error": "something"}

        prs = list_prs_with_label("owner/repo", "needs-review")

        assert prs == []


class TestCreateIssue:
    """Test create_issue function."""

    @patch("issue_orchestrator.github._run_gh")
    def test_create_issue_success(self, mock_run_gh):
        """Test successful issue creation."""
        from issue_orchestrator.github import create_issue

        mock_run_gh.return_value = "https://github.com/owner/repo/issues/42\n"

        issue_number = create_issue(
            "owner/repo",
            title="Test Issue",
            body="Test body",
            labels=["bug", "priority:high"],
        )

        assert issue_number == 42

    @patch("issue_orchestrator.github._run_gh")
    def test_create_issue_no_labels(self, mock_run_gh):
        """Test issue creation without labels."""
        from issue_orchestrator.github import create_issue

        mock_run_gh.return_value = "https://github.com/owner/repo/issues/123\n"

        issue_number = create_issue(
            "owner/repo",
            title="Simple Issue",
            body="Body",
        )

        assert issue_number == 123
        args = mock_run_gh.call_args[0][0]
        assert "--label" not in args

    @patch("issue_orchestrator.github._run_gh")
    def test_create_issue_error_returns_none(self, mock_run_gh):
        """Test that errors return None."""
        from issue_orchestrator.github import create_issue

        mock_run_gh.side_effect = GitHubError("gh failed")

        issue_number = create_issue(
            "owner/repo",
            title="Test",
            body="Body",
        )

        assert issue_number is None

    @patch("issue_orchestrator.github._run_gh")
    def test_create_issue_invalid_output_returns_none(self, mock_run_gh):
        """Test that invalid output returns None."""
        from issue_orchestrator.github import create_issue

        mock_run_gh.return_value = "unexpected output"

        issue_number = create_issue(
            "owner/repo",
            title="Test",
            body="Body",
        )

        assert issue_number is None
