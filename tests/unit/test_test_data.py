"""Unit tests for test_data module."""

import json
import pytest
from unittest.mock import MagicMock, patch, call

from issue_orchestrator.test_data import (
    cleanup_test_issues,
    create_test_issues,
    create_issue,
    update_issue,
    close_issue,
    cleanup_issues_by_label,
)


def mock_result(returncode=0, stdout="", stderr=""):
    """Create a mock subprocess result."""
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestCleanupTestIssues:
    """Test the cleanup_test_issues function."""

    @patch("issue_orchestrator.test_data._run_gh")
    def test_cleanup_no_issues(self, mock_run_gh):
        """Test cleanup when no test issues exist."""
        mock_run_gh.return_value = mock_result(stdout="[]")

        result = cleanup_test_issues("owner/repo")

        assert result == 0
        assert mock_run_gh.call_count == 2
        mock_run_gh.assert_any_call(
            ["issue", "list", "--repo", "owner/repo", "--label", "test-data",
             "--state", "open", "--json", "number"]
        )
        mock_run_gh.assert_any_call(
            ["issue", "list", "--repo", "owner/repo", "--label", "agent:e2e-test",
             "--state", "open", "--json", "number"]
        )

    @patch("issue_orchestrator.test_data._run_gh")
    def test_cleanup_single_issue(self, mock_run_gh):
        """Test cleanup when one test issue exists."""
        mock_run_gh.side_effect = [
            mock_result(stdout='[{"number": 42}]'),  # list test-data
            mock_result(),  # close 42
            mock_result(stdout='[]'),  # list agent:e2e-test
        ]

        result = cleanup_test_issues("owner/repo")

        assert result == 1
        assert mock_run_gh.call_count == 3
        mock_run_gh.assert_any_call(
            ["issue", "close", "42", "--repo", "owner/repo",
             "--comment", "Closed by test cleanup."]
        )

    @patch("issue_orchestrator.test_data._run_gh")
    def test_cleanup_multiple_issues(self, mock_run_gh):
        """Test cleanup when multiple test issues exist."""
        mock_run_gh.side_effect = [
            mock_result(stdout='[{"number": 10}, {"number": 20}, {"number": 30}]'),
            mock_result(),  # close 10
            mock_result(),  # close 20
            mock_result(),  # close 30
            mock_result(stdout='[]'),  # list agent:e2e-test
        ]

        result = cleanup_test_issues("owner/repo")

        assert result == 3
        assert mock_run_gh.call_count == 5

    @patch("issue_orchestrator.test_data._run_gh")
    def test_cleanup_list_command_fails(self, mock_run_gh):
        """Test cleanup when the list command fails."""
        mock_run_gh.return_value = mock_result(returncode=1, stderr="Error")

        result = cleanup_test_issues("owner/repo")

        assert result == 0
        assert mock_run_gh.call_count == 2

    @patch("issue_orchestrator.test_data._run_gh")
    def test_cleanup_with_different_repo(self, mock_run_gh):
        """Test cleanup with different repository formats."""
        mock_run_gh.return_value = mock_result(stdout="[]")

        cleanup_test_issues("myorg/myrepo")

        mock_run_gh.assert_any_call(
            ["issue", "list", "--repo", "myorg/myrepo", "--label", "test-data",
             "--state", "open", "--json", "number"]
        )


class TestCreateIssue:
    """Test the create_issue function."""

    @patch("issue_orchestrator.test_data._wait_for_issue_visible")
    @patch("issue_orchestrator.test_data._run_gh")
    def test_create_issue_basic(self, mock_run_gh, mock_wait):
        """Test basic issue creation."""
        mock_run_gh.return_value = mock_result(
            stdout="https://github.com/owner/repo/issues/123"
        )

        result = create_issue("owner/repo", "Test title", ["label1", "label2"])

        assert result == 123
        # Should ensure labels exist then create issue
        assert mock_run_gh.call_count >= 3  # 2 labels + 1 create

    @patch("issue_orchestrator.test_data._wait_for_issue_visible")
    @patch("issue_orchestrator.test_data._run_gh")
    def test_create_issue_waits_for_visibility(self, mock_run_gh, mock_wait):
        """Test that create_issue waits for visibility by default."""
        mock_run_gh.return_value = mock_result(
            stdout="https://github.com/owner/repo/issues/456"
        )

        create_issue("owner/repo", "Test", ["label"])

        mock_wait.assert_called_once_with("owner/repo", 456, 30)

    @patch("issue_orchestrator.test_data._wait_for_issue_visible")
    @patch("issue_orchestrator.test_data._run_gh")
    def test_create_issue_skip_wait(self, mock_run_gh, mock_wait):
        """Test that wait_visible=False skips waiting."""
        mock_run_gh.return_value = mock_result(
            stdout="https://github.com/owner/repo/issues/789"
        )

        create_issue("owner/repo", "Test", ["label"], wait_visible=False)

        mock_wait.assert_not_called()

    @patch("issue_orchestrator.test_data._run_gh")
    def test_create_issue_failure_raises(self, mock_run_gh):
        """Test that creation failure raises RuntimeError."""
        mock_run_gh.return_value = mock_result(returncode=1, stderr="Error")

        with pytest.raises(RuntimeError, match="Failed to create issue"):
            create_issue("owner/repo", "Test", ["label"], wait_visible=False)


class TestUpdateIssue:
    """Test the update_issue function."""

    @patch("issue_orchestrator.test_data._run_gh")
    def test_update_add_labels(self, mock_run_gh):
        """Test adding labels to an issue."""
        mock_run_gh.return_value = mock_result()

        update_issue("owner/repo", 123, add_labels=["priority:high", "urgent"])

        # Should ensure labels exist then add them
        mock_run_gh.assert_any_call(
            ["issue", "edit", "123", "--repo", "owner/repo",
             "--add-label", "priority:high,urgent"]
        )

    @patch("issue_orchestrator.test_data._run_gh")
    def test_update_remove_labels(self, mock_run_gh):
        """Test removing labels from an issue."""
        mock_run_gh.return_value = mock_result()

        update_issue("owner/repo", 123, remove_labels=["old-label"])

        mock_run_gh.assert_any_call(
            ["issue", "edit", "123", "--repo", "owner/repo",
             "--remove-label", "old-label"]
        )


class TestCloseIssue:
    """Test the close_issue function."""

    @patch("issue_orchestrator.test_data._run_gh")
    def test_close_issue_basic(self, mock_run_gh):
        """Test basic issue closing."""
        mock_run_gh.return_value = mock_result()

        close_issue("owner/repo", 123)

        mock_run_gh.assert_called_once_with(
            ["issue", "close", "123", "--repo", "owner/repo"]
        )

    @patch("issue_orchestrator.test_data._run_gh")
    def test_close_issue_with_comment(self, mock_run_gh):
        """Test closing with a comment."""
        mock_run_gh.return_value = mock_result()

        close_issue("owner/repo", 123, comment="Done!")

        mock_run_gh.assert_called_once_with(
            ["issue", "close", "123", "--repo", "owner/repo",
             "--comment", "Done!"]
        )


class TestCleanupIssuesByLabel:
    """Test the cleanup_issues_by_label function."""

    @patch("issue_orchestrator.test_data._run_gh")
    def test_cleanup_by_label(self, mock_run_gh):
        """Test cleaning up issues by specific label."""
        mock_run_gh.side_effect = [
            mock_result(stdout='[{"number": 1}, {"number": 2}]'),  # list
            mock_result(),  # close 1
            mock_result(),  # close 2
        ]

        result = cleanup_issues_by_label("owner/repo", "e2e:test_foo")

        assert result == 2
        mock_run_gh.assert_any_call(
            ["issue", "list", "--repo", "owner/repo", "--label", "e2e:test_foo",
             "--state", "open", "--json", "number"]
        )


class TestCreateTestIssues:
    """Test the create_test_issues function."""

    @patch("issue_orchestrator.test_data._wait_for_issue_visible")
    @patch("issue_orchestrator.test_data._run_gh")
    def test_create_with_default_labels(self, mock_run_gh, mock_wait):
        """Test creating issues with default agent labels."""
        # Return different issue numbers for each create
        issue_num = [0]
        def mock_create(*args):
            if args[0][0] == "issue" and args[0][1] == "create":
                issue_num[0] += 1
                return mock_result(stdout=f"https://github.com/owner/repo/issues/{issue_num[0]}")
            return mock_result()

        mock_run_gh.side_effect = mock_create

        result = create_test_issues("owner/repo")

        # Should return 5 issue numbers
        assert len(result) == 5
        assert all(isinstance(n, int) for n in result)

    @patch("issue_orchestrator.test_data._wait_for_issue_visible")
    @patch("issue_orchestrator.test_data._run_gh")
    def test_create_with_custom_labels(self, mock_run_gh, mock_wait):
        """Test creating issues with custom agent labels."""
        issue_num = [0]
        def mock_create(*args):
            if args[0][0] == "issue" and args[0][1] == "create":
                issue_num[0] += 1
                return mock_result(stdout=f"https://github.com/owner/repo/issues/{issue_num[0]}")
            return mock_result()

        mock_run_gh.side_effect = mock_create

        custom_labels = ["agent:api", "agent:database"]
        result = create_test_issues("owner/repo", agent_labels=custom_labels)

        assert len(result) == 5

    @patch("issue_orchestrator.test_data._wait_for_issue_visible")
    @patch("issue_orchestrator.test_data._run_gh")
    def test_three_agent_labels(self, mock_run_gh, mock_wait):
        """Test that three agent labels are distributed across issues."""
        issue_num = [0]
        def mock_create(*args):
            if args[0][0] == "issue" and args[0][1] == "create":
                issue_num[0] += 1
                return mock_result(stdout=f"https://github.com/owner/repo/issues/{issue_num[0]}")
            return mock_result()

        mock_run_gh.side_effect = mock_create

        labels = ["agent:one", "agent:two", "agent:three"]
        result = create_test_issues("owner/repo", agent_labels=labels)

        assert len(result) == 5
