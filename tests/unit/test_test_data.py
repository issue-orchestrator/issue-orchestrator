"""Unit tests for test_data module."""

from unittest.mock import Mock, patch, call

import pytest

from issue_orchestrator.testing.support.test_data import (
    cleanup_test_issues,
    create_test_issues,
    create_issue,
    update_issue,
    close_issue,
    cleanup_issues_by_label,
)


def _mock_issue(number: int) -> Mock:
    """Create a mock Issue object with the given number."""
    issue = Mock()
    issue.number = number
    return issue


class TestCleanupTestIssues:
    """Test the cleanup_test_issues function."""

    def test_cleanup_no_issues(self):
        """Test cleanup when no test issues exist."""
        adapter = Mock()
        adapter.list_issues.return_value = []

        with patch("issue_orchestrator.testing.support.test_data._adapter_for", return_value=adapter):
            result = cleanup_test_issues("owner/repo")

        assert result == 0
        assert adapter.list_issues.call_count == 2

    def test_cleanup_single_issue(self):
        """Test cleanup when one test issue exists."""
        adapter = Mock()
        adapter.list_issues.side_effect = [
            [_mock_issue(42)],
            [],
        ]

        with patch("issue_orchestrator.testing.support.test_data._adapter_for", return_value=adapter):
            result = cleanup_test_issues("owner/repo")

        assert result == 1
        adapter.add_comment.assert_called_once_with(42, "Closed by test cleanup.")
        adapter.update_issue_state.assert_called_once_with(42, "closed")

    def test_cleanup_multiple_issues(self):
        """Test cleanup when multiple test issues exist."""
        adapter = Mock()
        adapter.list_issues.side_effect = [
            [_mock_issue(10), _mock_issue(20), _mock_issue(30)],
            [],
        ]

        with patch("issue_orchestrator.testing.support.test_data._adapter_for", return_value=adapter):
            result = cleanup_test_issues("owner/repo")

        assert result == 3
        assert adapter.add_comment.call_count == 3
        assert adapter.update_issue_state.call_count == 3


class TestCreateIssue:
    """Test the create_issue function."""

    def test_create_issue_basic(self):
        """Test basic issue creation."""
        client = Mock()
        client.create_issue.return_value = 123

        with patch("issue_orchestrator.testing.support.test_data._adapter_for", return_value=client):
            with patch("issue_orchestrator.testing.support.test_data._wait_for_issue_visible"):
                result = create_issue("owner/repo", "Test title", ["label1", "label2"])

        assert result == 123
        client.create_label.assert_has_calls([call("label1", force=True), call("label2", force=True)])

    def test_create_issue_waits_for_visibility(self):
        """Test that create_issue waits for visibility by default."""
        client = Mock()
        client.create_issue.return_value = 456

        with patch("issue_orchestrator.testing.support.test_data._adapter_for", return_value=client):
            with patch("issue_orchestrator.testing.support.test_data._wait_for_issue_visible") as mock_wait:
                create_issue("owner/repo", "Test", ["label"])

        mock_wait.assert_called_once_with("owner/repo", 456, ["label"], None)

    def test_create_issue_skip_wait(self):
        """Test that wait_visible=False skips waiting."""
        client = Mock()
        client.create_issue.return_value = 789

        with patch("issue_orchestrator.testing.support.test_data._adapter_for", return_value=client):
            with patch("issue_orchestrator.testing.support.test_data._wait_for_issue_visible") as mock_wait:
                create_issue("owner/repo", "Test", ["label"], wait_visible=False)

        mock_wait.assert_not_called()

    def test_create_issue_failure_raises(self):
        """Test that creation failure raises RuntimeError."""
        client = Mock()
        client.create_issue.return_value = None

        with patch("issue_orchestrator.testing.support.test_data._adapter_for", return_value=client):
            with pytest.raises(RuntimeError, match="Failed to create issue"):
                create_issue("owner/repo", "Test", ["label"], wait_visible=False)


class TestUpdateIssue:
    """Test the update_issue function."""

    def test_update_add_labels(self):
        """Test adding labels to an issue."""
        client = Mock()

        with patch("issue_orchestrator.testing.support.test_data._adapter_for", return_value=client):
            update_issue("owner/repo", 123, add_labels=["priority:high", "urgent"])

        client.create_label.assert_has_calls([
            call("priority:high", force=True),
            call("urgent", force=True),
        ])
        client.add_label.assert_has_calls([
            call(123, "priority:high"),
            call(123, "urgent"),
        ])

    def test_update_remove_labels(self):
        """Test removing labels from an issue."""
        client = Mock()

        with patch("issue_orchestrator.testing.support.test_data._adapter_for", return_value=client):
            update_issue("owner/repo", 123, remove_labels=["old-label"])

        client.remove_label.assert_called_once_with(123, "old-label")


class TestCloseIssue:
    """Test the close_issue function."""

    def test_close_issue_basic(self):
        """Test basic issue closing."""
        client = Mock()

        with patch("issue_orchestrator.testing.support.test_data._adapter_for", return_value=client):
            close_issue("owner/repo", 123)

        client.update_issue_state.assert_called_once_with(123, "closed")

    def test_close_issue_with_comment(self):
        """Test closing with a comment."""
        client = Mock()

        with patch("issue_orchestrator.testing.support.test_data._adapter_for", return_value=client):
            close_issue("owner/repo", 123, comment="Done!")

        client.add_comment.assert_called_once_with(123, "Done!")
        client.update_issue_state.assert_called_once_with(123, "closed")


class TestCleanupIssuesByLabel:
    """Test the cleanup_issues_by_label function."""

    def test_cleanup_by_label(self):
        """Test cleaning up issues by specific label."""
        adapter = Mock()
        adapter.list_issues.return_value = [_mock_issue(1), _mock_issue(2)]

        with patch("issue_orchestrator.testing.support.test_data._adapter_for", return_value=adapter):
            with patch("issue_orchestrator.testing.support.test_data.close_issue") as mock_close:
                result = cleanup_issues_by_label("owner/repo", "e2e:test_foo")

        assert result == 2
        mock_close.assert_has_calls([
            call("owner/repo", 1, "Cleaned up by test: e2e:test_foo"),
            call("owner/repo", 2, "Cleaned up by test: e2e:test_foo"),
        ], any_order=True)


class TestCreateTestIssues:
    """Test the create_test_issues function."""

    def test_create_with_default_labels(self):
        """Test creating issues with default agent labels."""
        with patch("issue_orchestrator.testing.support.test_data.create_issue", side_effect=[1, 2, 3, 4, 5]):
            result = create_test_issues("owner/repo")

        assert result == [1, 2, 3, 4, 5]

    def test_create_with_custom_labels(self):
        """Test creating issues with custom agent labels."""
        with patch("issue_orchestrator.testing.support.test_data.create_issue", side_effect=[1, 2, 3, 4, 5]):
            custom_labels = ["agent:api", "agent:database"]
            result = create_test_issues("owner/repo", agent_labels=custom_labels)

        assert len(result) == 5
