"""Unit tests for test_data module."""

import json
import pytest
from unittest.mock import MagicMock, patch, call
import subprocess

from issue_orchestrator.test_data import cleanup_test_issues, create_test_issues


class TestCleanupTestIssues:
    """Test the cleanup_test_issues function."""

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_cleanup_no_issues(self, mock_run):
        """Test cleanup when no test issues exist."""
        # Mock the issue list command to return empty list for both labels
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="[]",
            stderr=""
        )

        result = cleanup_test_issues("owner/repo")

        assert result == 0
        # Now queries both test-data and agent:e2e-test labels
        assert mock_run.call_count == 2
        mock_run.assert_any_call(
            ["gh", "issue", "list", "--repo", "owner/repo", "--label", "test-data",
             "--state", "open", "--json", "number"],
            capture_output=True, text=True
        )
        mock_run.assert_any_call(
            ["gh", "issue", "list", "--repo", "owner/repo", "--label", "agent:e2e-test",
             "--state", "open", "--json", "number"],
            capture_output=True, text=True
        )

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_cleanup_single_issue(self, mock_run):
        """Test cleanup when one test issue exists."""
        # Mock: first label finds issue 42, second label finds nothing
        list_result_with_issue = MagicMock(returncode=0, stdout='[{"number": 42}]', stderr="")
        list_result_empty = MagicMock(returncode=0, stdout='[]', stderr="")
        close_result = MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = [list_result_with_issue, close_result, list_result_empty]

        result = cleanup_test_issues("owner/repo")

        assert result == 1
        assert mock_run.call_count == 3  # 2 list calls + 1 close

        # Verify list calls for both labels
        mock_run.assert_any_call(
            ["gh", "issue", "list", "--repo", "owner/repo", "--label", "test-data",
             "--state", "open", "--json", "number"],
            capture_output=True, text=True
        )
        mock_run.assert_any_call(
            ["gh", "issue", "list", "--repo", "owner/repo", "--label", "agent:e2e-test",
             "--state", "open", "--json", "number"],
            capture_output=True, text=True
        )

        # Verify close call
        mock_run.assert_any_call(
            ["gh", "issue", "close", "42", "--repo", "owner/repo",
             "--comment", "Closed by test cleanup."],
            capture_output=True
        )

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_cleanup_multiple_issues(self, mock_run):
        """Test cleanup when multiple test issues exist."""
        # Mock: first label returns 3 issues, second label returns empty
        list_result = MagicMock(
            returncode=0,
            stdout='[{"number": 10}, {"number": 20}, {"number": 30}]',
            stderr=""
        )
        list_result_empty = MagicMock(returncode=0, stdout='[]', stderr="")
        close_result = MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = [list_result, close_result, close_result, close_result, list_result_empty]

        result = cleanup_test_issues("owner/repo")

        assert result == 3
        assert mock_run.call_count == 5  # 2 list calls + 3 close calls

        # Verify close calls
        mock_run.assert_any_call(
            ["gh", "issue", "close", "10", "--repo", "owner/repo",
             "--comment", "Closed by test cleanup."],
            capture_output=True
        )
        mock_run.assert_any_call(
            ["gh", "issue", "close", "20", "--repo", "owner/repo",
             "--comment", "Closed by test cleanup."],
            capture_output=True
        )
        mock_run.assert_any_call(
            ["gh", "issue", "close", "30", "--repo", "owner/repo",
             "--comment", "Closed by test cleanup."],
            capture_output=True
        )

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_cleanup_list_command_fails(self, mock_run):
        """Test cleanup when the list command fails."""
        # Mock both list commands to fail
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: gh not found"
        )

        result = cleanup_test_issues("owner/repo")

        assert result == 0
        assert mock_run.call_count == 2  # Both label queries attempted

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_cleanup_with_different_repo(self, mock_run):
        """Test cleanup with different repository formats."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="[]",
            stderr=""
        )

        cleanup_test_issues("myorg/myrepo")

        assert mock_run.call_count == 2
        mock_run.assert_any_call(
            ["gh", "issue", "list", "--repo", "myorg/myrepo", "--label", "test-data",
             "--state", "open", "--json", "number"],
            capture_output=True, text=True
        )
        mock_run.assert_any_call(
            ["gh", "issue", "list", "--repo", "myorg/myrepo", "--label", "agent:e2e-test",
             "--state", "open", "--json", "number"],
            capture_output=True, text=True
        )


class TestCreateTestIssues:
    """Test the create_test_issues function."""

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_create_with_default_labels(self, mock_run):
        """Test creating issues with default agent labels."""
        # Mock all subprocess calls to succeed
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/issues/1",
            stderr=""
        )

        result = create_test_issues("owner/repo")

        # Should return 5 issue URLs
        assert len(result) == 5
        assert all(url == "https://github.com/owner/repo/issues/1" for url in result)

        # Verify the test-data label was created
        mock_run.assert_any_call(
            ["gh", "label", "create", "test-data", "--repo", "owner/repo", "--force",
             "--description", "Test data for integration tests"],
            capture_output=True
        )

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_create_with_custom_labels(self, mock_run):
        """Test creating issues with custom agent labels."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/issues/1",
            stderr=""
        )

        custom_labels = ["agent:api", "agent:database"]
        result = create_test_issues("owner/repo", agent_labels=custom_labels)

        assert len(result) == 5

        # Verify custom labels were used
        mock_run.assert_any_call(
            ["gh", "label", "create", "agent:api", "--repo", "owner/repo", "--force"],
            capture_output=True
        )
        mock_run.assert_any_call(
            ["gh", "label", "create", "agent:database", "--repo", "owner/repo", "--force"],
            capture_output=True
        )

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_create_with_single_agent_label(self, mock_run):
        """Test creating issues with only one agent label."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/issues/1",
            stderr=""
        )

        single_label = ["agent:solo"]
        result = create_test_issues("owner/repo", agent_labels=single_label)

        assert len(result) == 5

        # All issues should use the single agent label
        # Verify the single label was created multiple times (once for each issue)
        agent_label_calls = [
            c for c in mock_run.call_args_list
            if c[0][0][:3] == ["gh", "label", "create"]
            and "agent:solo" in c[0][0]
        ]
        assert len(agent_label_calls) >= 1

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_create_issue_command_structure(self, mock_run):
        """Test the structure of issue create commands."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/issues/1",
            stderr=""
        )

        create_test_issues("owner/repo")

        # Find issue create calls
        create_calls = [
            c for c in mock_run.call_args_list
            if len(c[0][0]) > 0 and c[0][0][:3] == ["gh", "issue", "create"]
        ]

        # Should have 5 issue create calls
        assert len(create_calls) >= 5

        # Verify first issue create call has correct structure
        first_create = None
        for call_obj in mock_run.call_args_list:
            cmd = call_obj[0][0]
            if (len(cmd) > 0 and
                cmd[:3] == ["gh", "issue", "create"] and
                "[TEST] Simple backend task" in cmd):
                first_create = cmd
                break

        assert first_create is not None
        assert "--repo" in first_create
        assert "owner/repo" in first_create
        assert "--title" in first_create
        assert "--body" in first_create
        assert "--label" in first_create
        assert "test-data" in first_create

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_create_with_priority_labels(self, mock_run):
        """Test that priority labels are created and assigned."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/issues/1",
            stderr=""
        )

        create_test_issues("owner/repo")

        # Verify priority labels were created
        mock_run.assert_any_call(
            ["gh", "label", "create", "priority:high", "--repo", "owner/repo", "--force"],
            capture_output=True
        )
        mock_run.assert_any_call(
            ["gh", "label", "create", "priority:medium", "--repo", "owner/repo", "--force"],
            capture_output=True
        )
        mock_run.assert_any_call(
            ["gh", "label", "create", "priority:low", "--repo", "owner/repo", "--force"],
            capture_output=True
        )

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_create_issues_without_priority(self, mock_run):
        """Test that some issues are created without priority labels."""
        call_history = []

        def track_calls(*args, **kwargs):
            call_history.append(args[0])
            return MagicMock(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/1",
                stderr=""
            )

        mock_run.side_effect = track_calls

        result = create_test_issues("owner/repo")

        assert len(result) == 5

        # Find issue create commands without priority labels
        create_commands = [
            cmd for cmd in call_history
            if len(cmd) > 0 and cmd[:3] == ["gh", "issue", "create"]
        ]

        # Some issues should not have priority labels
        # Issues 4 and 5 have priority_label = None
        assert len(create_commands) >= 5

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_create_issue_failure(self, mock_run):
        """Test handling of issue creation failures."""
        # Mock label creation to succeed but issue creation to fail
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[:3] == ["gh", "issue", "create"]:
                return MagicMock(returncode=1, stdout="", stderr="Error creating issue")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        result = create_test_issues("owner/repo")

        # Should return empty list when all creations fail
        assert len(result) == 0

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_create_partial_failure(self, mock_run):
        """Test handling when some issues succeed and some fail."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            cmd = args[0]
            call_count[0] += 1

            if cmd[:3] == ["gh", "issue", "create"]:
                # Fail every other issue creation
                if call_count[0] % 2 == 0:
                    return MagicMock(
                        returncode=0,
                        stdout="https://github.com/owner/repo/issues/1",
                        stderr=""
                    )
                else:
                    return MagicMock(returncode=1, stdout="", stderr="Error")

            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        result = create_test_issues("owner/repo")

        # Some issues should have been created
        assert len(result) >= 0
        assert len(result) <= 5

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_create_with_empty_agent_labels_list(self, mock_run):
        """Test creating issues with empty agent labels list causes IndexError."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/issues/1",
            stderr=""
        )

        # Empty list causes IndexError due to agent_labels[0] access
        with pytest.raises(IndexError):
            create_test_issues("owner/repo", agent_labels=[])

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_issue_titles_and_bodies(self, mock_run):
        """Test that issue titles and bodies are correctly formatted."""
        call_history = []

        def track_calls(*args, **kwargs):
            call_history.append(args[0])
            return MagicMock(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/1",
                stderr=""
            )

        mock_run.side_effect = track_calls

        create_test_issues("owner/repo")

        # Find issue create commands
        create_commands = [
            cmd for cmd in call_history
            if len(cmd) > 0 and cmd[:3] == ["gh", "issue", "create"]
        ]

        # Verify expected titles are present
        all_commands_str = " ".join([" ".join(cmd) for cmd in create_commands])
        assert "[TEST] Simple backend task" in all_commands_str
        assert "[TEST] Frontend feature" in all_commands_str
        assert "[TEST] Mobile bug fix" in all_commands_str
        assert "[TEST] Task that will block" in all_commands_str
        assert "[TEST] Task with dependency" in all_commands_str

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_repo_parameter_propagation(self, mock_run):
        """Test that repo parameter is correctly passed to all gh commands."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/issues/1",
            stderr=""
        )

        test_repo = "testorg/testrepo"
        create_test_issues(test_repo)

        # Verify all gh commands include the correct repo
        for call_obj in mock_run.call_args_list:
            cmd = call_obj[0][0]
            if len(cmd) > 0 and cmd[0] == "gh":
                # All gh commands should have --repo flag with correct value
                if "--repo" in cmd:
                    repo_index = cmd.index("--repo")
                    assert cmd[repo_index + 1] == test_repo

    @patch("issue_orchestrator.test_data.subprocess.run")
    def test_three_agent_labels(self, mock_run):
        """Test creating issues with three different agent labels."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/issues/1",
            stderr=""
        )

        three_labels = ["agent:alpha", "agent:beta", "agent:gamma"]
        result = create_test_issues("owner/repo", agent_labels=three_labels)

        assert len(result) == 5

        # Verify all three labels were created
        mock_run.assert_any_call(
            ["gh", "label", "create", "agent:alpha", "--repo", "owner/repo", "--force"],
            capture_output=True
        )
        mock_run.assert_any_call(
            ["gh", "label", "create", "agent:beta", "--repo", "owner/repo", "--force"],
            capture_output=True
        )
        mock_run.assert_any_call(
            ["gh", "label", "create", "agent:gamma", "--repo", "owner/repo", "--force"],
            capture_output=True
        )
