"""Unit tests for agent_done module."""

import argparse
import subprocess
import sys
from unittest.mock import Mock, patch, call
import pytest

from issue_orchestrator.agent_done import (
    Status,
    CompletionData,
    REQUIRED_FIELDS,
    die,
    get_issue_number,
    get_repo,
    validate_fields,
    format_completion_comment,
    format_blocked_comment,
    format_needs_human_comment,
    post_comment,
    add_label,
    add_trailers_to_commit,
    git_push,
    create_pr,
    update_comment_with_pr,
    main,
)


class TestStatus:
    """Test the Status enum."""

    def test_status_values(self):
        """Test all status values are correct."""
        assert Status.COMPLETED.value == "completed"
        assert Status.BLOCKED.value == "blocked"
        assert Status.NEEDS_HUMAN.value == "needs_human"

    def test_status_from_string(self):
        """Test creating Status from string value."""
        assert Status("completed") == Status.COMPLETED
        assert Status("blocked") == Status.BLOCKED
        assert Status("needs_human") == Status.NEEDS_HUMAN


class TestRequiredFields:
    """Test REQUIRED_FIELDS constant."""

    def test_completed_required_fields(self):
        """Test required fields for completed status."""
        assert REQUIRED_FIELDS[Status.COMPLETED] == ["implementation", "problems"]

    def test_blocked_required_fields(self):
        """Test required fields for blocked status."""
        assert REQUIRED_FIELDS[Status.BLOCKED] == ["reason", "attempted"]

    def test_needs_human_required_fields(self):
        """Test required fields for needs_human status."""
        assert REQUIRED_FIELDS[Status.NEEDS_HUMAN] == ["question"]


class TestCompletionData:
    """Test the CompletionData dataclass."""

    def test_completion_data_creation(self):
        """Test basic completion data creation."""
        data = CompletionData(
            status=Status.COMPLETED,
            implementation="Added feature X",
            problems="None",
        )
        assert data.status == Status.COMPLETED
        assert data.implementation == "Added feature X"
        assert data.problems == "None"

    def test_completion_data_defaults(self):
        """Test completion data with default values."""
        data = CompletionData(status=Status.BLOCKED)
        assert data.status == Status.BLOCKED
        assert data.implementation is None
        assert data.problems is None
        assert data.reason is None
        assert data.attempted is None
        assert data.blocked_by is None
        assert data.question is None
        assert data.context is None
        assert data.options is None
        assert data.default_action is None

    def test_completion_data_blocked_with_blocked_by(self):
        """Test blocked completion with blocked_by issues."""
        data = CompletionData(
            status=Status.BLOCKED,
            reason="Waiting for API access",
            attempted="Tried local testing",
            blocked_by=[123, 456],
        )
        assert data.blocked_by == [123, 456]

    def test_completion_data_needs_human_with_options(self):
        """Test needs_human completion with options."""
        data = CompletionData(
            status=Status.NEEDS_HUMAN,
            question="Which approach?",
            options=["Option A", "Option B"],
            default_action="Option A",
        )
        assert data.options == ["Option A", "Option B"]
        assert data.default_action == "Option A"


class TestDie:
    """Test the die function."""

    def test_die_exits_with_1(self):
        """Test die exits with status code 1."""
        with pytest.raises(SystemExit) as exc_info:
            die("Test error message")
        assert exc_info.value.code == 1

    def test_die_prints_to_stderr(self, capsys):
        """Test die prints error message to stderr."""
        with pytest.raises(SystemExit):
            die("Test error message")
        captured = capsys.readouterr()
        assert "ERROR: Test error message" in captured.err
        assert "Use --help for usage information" in captured.err


class TestGetIssueNumber:
    """Test the get_issue_number function."""

    def test_get_issue_number_success(self):
        """Test extracting issue number from branch name."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="123-fix-bug\n"
            )
            issue_number = get_issue_number()
            assert issue_number == 123

    def test_get_issue_number_multi_digit(self):
        """Test extracting multi-digit issue number."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="9876-implement-feature\n"
            )
            issue_number = get_issue_number()
            assert issue_number == 9876

    def test_get_issue_number_git_failure(self):
        """Test error when git command fails."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=1)
            with pytest.raises(SystemExit):
                get_issue_number()

    def test_get_issue_number_invalid_branch(self):
        """Test error when branch doesn't match pattern."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="feature-branch\n"
            )
            with pytest.raises(SystemExit):
                get_issue_number()

    def test_get_issue_number_no_hyphen_after_number(self):
        """Test error when branch has number but no hyphen."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="123feature\n"
            )
            with pytest.raises(SystemExit):
                get_issue_number()


class TestGetRepo:
    """Test the get_repo function."""

    def test_get_repo_success(self):
        """Test getting repo from gh CLI."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="owner/repo\n"
            )
            repo = get_repo()
            assert repo == "owner/repo"

    def test_get_repo_failure(self):
        """Test error when gh command fails."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=1)
            with pytest.raises(SystemExit):
                get_repo()


class TestValidateFields:
    """Test the validate_fields function."""

    def test_validate_completed_with_all_fields(self):
        """Test validation passes for completed with all required fields."""
        data = CompletionData(
            status=Status.COMPLETED,
            implementation="Added feature",
            problems="None",
        )
        # Should not raise
        validate_fields(data)

    def test_validate_completed_missing_implementation(self):
        """Test validation fails when implementation is missing."""
        data = CompletionData(
            status=Status.COMPLETED,
            problems="None",
        )
        with pytest.raises(SystemExit):
            validate_fields(data)

    def test_validate_completed_missing_problems(self):
        """Test validation fails when problems is missing."""
        data = CompletionData(
            status=Status.COMPLETED,
            implementation="Added feature",
        )
        with pytest.raises(SystemExit):
            validate_fields(data)

    def test_validate_completed_empty_implementation(self):
        """Test validation fails when implementation is empty string."""
        data = CompletionData(
            status=Status.COMPLETED,
            implementation="   ",
            problems="None",
        )
        with pytest.raises(SystemExit):
            validate_fields(data)

    def test_validate_blocked_with_all_fields(self):
        """Test validation passes for blocked with all required fields."""
        data = CompletionData(
            status=Status.BLOCKED,
            reason="Waiting for API",
            attempted="Tried workaround",
        )
        validate_fields(data)

    def test_validate_blocked_missing_reason(self):
        """Test validation fails when reason is missing."""
        data = CompletionData(
            status=Status.BLOCKED,
            attempted="Tried workaround",
        )
        with pytest.raises(SystemExit):
            validate_fields(data)

    def test_validate_blocked_missing_attempted(self):
        """Test validation fails when attempted is missing."""
        data = CompletionData(
            status=Status.BLOCKED,
            reason="Waiting for API",
        )
        with pytest.raises(SystemExit):
            validate_fields(data)

    def test_validate_needs_human_with_question(self):
        """Test validation passes for needs_human with question."""
        data = CompletionData(
            status=Status.NEEDS_HUMAN,
            question="Which approach?",
        )
        validate_fields(data)

    def test_validate_needs_human_missing_question(self):
        """Test validation fails when question is missing."""
        data = CompletionData(status=Status.NEEDS_HUMAN)
        with pytest.raises(SystemExit):
            validate_fields(data)

    def test_validate_needs_human_empty_question(self):
        """Test validation fails when question is empty."""
        data = CompletionData(
            status=Status.NEEDS_HUMAN,
            question="",
        )
        with pytest.raises(SystemExit):
            validate_fields(data)


class TestFormatCompletionComment:
    """Test the format_completion_comment function."""

    def test_format_completion_comment_basic(self):
        """Test formatting basic completion comment."""
        data = CompletionData(
            status=Status.COMPLETED,
            implementation="Added user authentication",
            problems="None",
        )
        comment = format_completion_comment(data)
        assert "## Implementation" in comment
        assert "Added user authentication" in comment
        assert "## Problems Encountered" in comment
        assert "None" in comment
        assert "## Pull Request" in comment
        assert "<PR_LINK_PLACEHOLDER>" in comment

    def test_format_completion_comment_with_problems(self):
        """Test formatting completion comment with actual problems."""
        data = CompletionData(
            status=Status.COMPLETED,
            implementation="Fixed bug in payment processing",
            problems="Had to refactor the entire payment module",
        )
        comment = format_completion_comment(data)
        assert "Fixed bug in payment processing" in comment
        assert "Had to refactor the entire payment module" in comment


class TestFormatBlockedComment:
    """Test the format_blocked_comment function."""

    def test_format_blocked_comment_basic(self):
        """Test formatting basic blocked comment."""
        data = CompletionData(
            status=Status.BLOCKED,
            reason="Need API credentials",
            attempted="Checked environment variables",
        )
        comment = format_blocked_comment(data)
        assert "## Blocked" in comment
        assert "**Reason:** Need API credentials" in comment
        assert "**Attempted:** Checked environment variables" in comment
        assert "**Unblock action:** Need API credentials" in comment

    def test_format_blocked_comment_with_blocked_by(self):
        """Test formatting blocked comment with blocked_by issues."""
        data = CompletionData(
            status=Status.BLOCKED,
            reason="Depends on other work",
            attempted="Tried to work around it",
            blocked_by=[123, 456],
        )
        comment = format_blocked_comment(data)
        assert "**Blocked by:** #123, #456" in comment
        assert "**Reason:** Depends on other work" in comment
        assert "**Attempted:** Tried to work around it" in comment

    def test_format_blocked_comment_single_blocker(self):
        """Test formatting blocked comment with single blocked_by issue."""
        data = CompletionData(
            status=Status.BLOCKED,
            reason="Waiting for #789",
            attempted="Nothing to attempt",
            blocked_by=[789],
        )
        comment = format_blocked_comment(data)
        assert "**Blocked by:** #789" in comment


class TestFormatNeedsHumanComment:
    """Test the format_needs_human_comment function."""

    def test_format_needs_human_comment_basic(self):
        """Test formatting basic needs_human comment."""
        data = CompletionData(
            status=Status.NEEDS_HUMAN,
            question="Should we use approach A or B?",
        )
        comment = format_needs_human_comment(data)
        assert "## Needs Human Input" in comment
        assert "**Question:** Should we use approach A or B?" in comment

    def test_format_needs_human_comment_with_context(self):
        """Test formatting needs_human comment with context."""
        data = CompletionData(
            status=Status.NEEDS_HUMAN,
            question="Which database?",
            context="We need to choose between SQL and NoSQL",
        )
        comment = format_needs_human_comment(data)
        assert "**Question:** Which database?" in comment
        assert "**Context:** We need to choose between SQL and NoSQL" in comment

    def test_format_needs_human_comment_with_options(self):
        """Test formatting needs_human comment with options."""
        data = CompletionData(
            status=Status.NEEDS_HUMAN,
            question="Which framework?",
            options=["React", "Vue", "Angular"],
        )
        comment = format_needs_human_comment(data)
        assert "**Options:**" in comment
        assert "1. React" in comment
        assert "2. Vue" in comment
        assert "3. Angular" in comment

    def test_format_needs_human_comment_with_default(self):
        """Test formatting needs_human comment with default action."""
        data = CompletionData(
            status=Status.NEEDS_HUMAN,
            question="Proceed with deployment?",
            default_action="Will deploy in 24 hours",
        )
        comment = format_needs_human_comment(data)
        assert "**Default if no response:** Will deploy in 24 hours" in comment

    def test_format_needs_human_comment_full(self):
        """Test formatting needs_human comment with all fields."""
        data = CompletionData(
            status=Status.NEEDS_HUMAN,
            question="Choose authentication method",
            context="We support multiple auth methods",
            options=["OAuth", "JWT", "Session cookies"],
            default_action="Use OAuth",
        )
        comment = format_needs_human_comment(data)
        assert "**Question:** Choose authentication method" in comment
        assert "**Context:** We support multiple auth methods" in comment
        assert "**Options:**" in comment
        assert "1. OAuth" in comment
        assert "2. JWT" in comment
        assert "3. Session cookies" in comment
        assert "**Default if no response:** Use OAuth" in comment


class TestPostComment:
    """Test the post_comment function."""

    def test_post_comment_success(self):
        """Test posting comment successfully."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/123#issuecomment-456\n"
            )
            url = post_comment("owner/repo", 123, "Test comment")
            assert url == "https://github.com/owner/repo/issues/123#issuecomment-456"
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == ["gh", "issue", "comment", "123", "--body", "Test comment"]

    def test_post_comment_failure(self):
        """Test error when posting comment fails."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=1,
                stderr="API error"
            )
            with pytest.raises(SystemExit):
                post_comment("owner/repo", 123, "Test comment")


class TestAddLabel:
    """Test the add_label function."""

    def test_add_label_success(self, capsys):
        """Test adding label successfully."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0)
            add_label("owner/repo", 123, "blocked")
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == ["gh", "issue", "edit", "123", "--add-label", "blocked"]

    def test_add_label_failure(self, capsys):
        """Test warning when adding label fails."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=1,
                stderr="Label not found"
            )
            add_label("owner/repo", 123, "nonexistent")
            captured = capsys.readouterr()
            assert "Warning" in captured.err
            assert "Could not add label 'nonexistent'" in captured.err


class TestAddTrailersToCommit:
    """Test the add_trailers_to_commit function."""

    def test_add_trailers_completed(self, capsys):
        """Test adding trailers for completed status."""
        with patch('subprocess.run') as mock_run:
            # First call: get commit message
            # Second call: amend commit
            mock_run.side_effect = [
                Mock(returncode=0, stdout="Original commit message\n"),
                Mock(returncode=0),
            ]

            data = CompletionData(
                status=Status.COMPLETED,
                implementation="Added feature X",
                problems="None",
            )
            add_trailers_to_commit(data)

            assert mock_run.call_count == 2
            # Check git log call
            assert mock_run.call_args_list[0][0][0] == ["git", "log", "-1", "--format=%B"]
            # Check git commit --amend call
            amend_call = mock_run.call_args_list[1][0][0]
            assert amend_call[0:3] == ["git", "commit", "--amend"]
            assert "Agent-Status: completed" in amend_call[-1]
            assert "Agent-Implementation: Added feature X" in amend_call[-1]
            assert "Agent-Problems: None" in amend_call[-1]

    def test_add_trailers_blocked(self, capsys):
        """Test adding trailers for blocked status."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout="Commit message\n"),
                Mock(returncode=0),
            ]

            data = CompletionData(
                status=Status.BLOCKED,
                reason="API access needed",
                attempted="Checked docs",
                blocked_by=[456],
            )
            add_trailers_to_commit(data)

            amend_call = mock_run.call_args_list[1][0][0]
            assert "Agent-Status: blocked" in amend_call[-1]
            assert "Agent-Reason: API access needed" in amend_call[-1]
            assert "Agent-Attempted: Checked docs" in amend_call[-1]
            assert "Agent-Blocked-By: 456" in amend_call[-1]

    def test_add_trailers_needs_human(self, capsys):
        """Test adding trailers for needs_human status."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout="Commit message\n"),
                Mock(returncode=0),
            ]

            data = CompletionData(
                status=Status.NEEDS_HUMAN,
                question="Which approach?",
                context="Need to decide",
            )
            add_trailers_to_commit(data)

            amend_call = mock_run.call_args_list[1][0][0]
            assert "Agent-Status: needs_human" in amend_call[-1]
            assert "Agent-Question: Which approach?" in amend_call[-1]
            assert "Agent-Context: Need to decide" in amend_call[-1]

    def test_add_trailers_already_present(self, capsys):
        """Test skipping when trailers already exist."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="Commit message\n\nAgent-Status: completed\n"
            )

            data = CompletionData(
                status=Status.COMPLETED,
                implementation="Feature",
                problems="None",
            )
            add_trailers_to_commit(data)

            # Should only call git log, not git commit --amend
            assert mock_run.call_count == 1
            captured = capsys.readouterr()
            assert "Trailers already present" in captured.out

    def test_add_trailers_git_log_failure(self):
        """Test error when git log fails."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=1)

            data = CompletionData(
                status=Status.COMPLETED,
                implementation="Feature",
                problems="None",
            )
            with pytest.raises(SystemExit):
                add_trailers_to_commit(data)

    def test_add_trailers_git_amend_failure(self):
        """Test error when git commit --amend fails."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout="Commit\n"),
                Mock(returncode=1, stderr="Amend failed"),
            ]

            data = CompletionData(
                status=Status.COMPLETED,
                implementation="Feature",
                problems="None",
            )
            with pytest.raises(SystemExit):
                add_trailers_to_commit(data)

    def test_add_trailers_blocked_multiple_blockers(self):
        """Test adding trailers with multiple blocking issues."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout="Commit\n"),
                Mock(returncode=0),
            ]

            data = CompletionData(
                status=Status.BLOCKED,
                reason="Multiple dependencies",
                attempted="Checked all",
                blocked_by=[123, 456, 789],
            )
            add_trailers_to_commit(data)

            amend_call = mock_run.call_args_list[1][0][0]
            assert "Agent-Blocked-By: 123,456,789" in amend_call[-1]


class TestGitPush:
    """Test the git_push function."""

    def test_git_push_success(self, capsys):
        """Test pushing successfully."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout="feature-branch\n"),
                Mock(returncode=0),
            ]
            git_push()

            assert mock_run.call_count == 2
            # Check git push call
            push_call = mock_run.call_args_list[1][0][0]
            assert push_call == ["git", "push", "-u", "origin", "feature-branch"]

            captured = capsys.readouterr()
            assert "Pushed branch 'feature-branch' to origin" in captured.out

    def test_git_push_failure(self):
        """Test error when push fails."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout="feature-branch\n"),
                Mock(returncode=1, stderr="Push rejected"),
            ]
            with pytest.raises(SystemExit):
                git_push()


class TestCreatePR:
    """Test the create_pr function."""

    def test_create_pr_success(self):
        """Test creating PR successfully."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="https://github.com/owner/repo/pull/123\n"
            )
            pr_url = create_pr(456, "Fix issue #456")
            assert pr_url == "https://github.com/owner/repo/pull/123"

            args = mock_run.call_args[0][0]
            assert args[:4] == ["gh", "pr", "create", "--title"]
            assert "Fix issue #456" in args
            # The body is passed as a single argument containing the full text
            assert any("Closes #456" in str(arg) for arg in args)

    def test_create_pr_already_exists(self):
        """Test handling when PR already exists."""
        with patch('subprocess.run') as mock_run:
            # First call fails with "already exists"
            # Second call gets existing PR URL
            mock_run.side_effect = [
                Mock(returncode=1, stderr="already exists"),
                Mock(returncode=0, stdout="https://github.com/owner/repo/pull/999\n"),
            ]
            pr_url = create_pr(456, "Fix issue #456")
            assert pr_url == "https://github.com/owner/repo/pull/999"

    def test_create_pr_failure(self):
        """Test error when PR creation fails."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=1,
                stderr="Permission denied"
            )
            with pytest.raises(SystemExit):
                create_pr(456, "Fix issue #456")

    def test_create_pr_already_exists_but_cant_get_url(self):
        """Test error when PR exists but can't get URL."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = [
                Mock(returncode=1, stderr="already exists"),
                Mock(returncode=1, stderr="Not found"),
            ]
            with pytest.raises(SystemExit):
                create_pr(456, "Fix issue #456")


class TestUpdateCommentWithPR:
    """Test the update_comment_with_pr function."""

    def test_update_comment_with_pr_valid_url(self, capsys):
        """Test updating comment with PR URL."""
        comment_url = "https://github.com/owner/repo/issues/123#issuecomment-456"
        pr_url = "https://github.com/owner/repo/pull/789"
        original_body = "## Implementation\n\n<PR_LINK_PLACEHOLDER>"

        update_comment_with_pr("owner/repo", comment_url, pr_url, original_body)

        captured = capsys.readouterr()
        assert "PR created:" in captured.out
        assert pr_url in captured.out

    def test_update_comment_with_pr_invalid_url(self, capsys):
        """Test handling invalid comment URL."""
        comment_url = "https://github.com/owner/repo/issues/123"
        pr_url = "https://github.com/owner/repo/pull/789"
        original_body = "Body"

        update_comment_with_pr("owner/repo", comment_url, pr_url, original_body)

        captured = capsys.readouterr()
        assert "Could not update comment" in captured.err


class TestMain:
    """Test the main function."""

    def test_main_completed_dry_run(self, capsys):
        """Test dry run mode for completed status."""
        with patch('sys.argv', [
            'agent-done', 'completed',
            '--implementation', 'Added feature',
            '--problems', 'None',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.agent_done.get_issue_number', return_value=123):
                with patch('issue_orchestrator.agent_done.get_repo', return_value='owner/repo'):
                    main()

                    captured = capsys.readouterr()
                    assert "DRY RUN" in captured.out
                    assert "Added feature" in captured.out
                    assert "Issue: #123" in captured.out

    def test_main_completed_success(self, capsys):
        """Test successful completion workflow."""
        with patch('sys.argv', [
            'agent-done', 'completed',
            '--implementation', 'Fixed bug',
            '--problems', 'None'
        ]):
            with patch('issue_orchestrator.agent_done.get_issue_number', return_value=456):
                with patch('issue_orchestrator.agent_done.get_repo', return_value='owner/repo'):
                    with patch('issue_orchestrator.agent_done.add_trailers_to_commit'):
                        with patch('issue_orchestrator.agent_done.git_push'):
                            with patch('issue_orchestrator.agent_done.create_pr', return_value='https://pr.url'):
                                with patch('issue_orchestrator.agent_done.post_comment'):
                                    main()

                                    captured = capsys.readouterr()
                                    assert "COMPLETED" in captured.out
                                    assert "https://pr.url" in captured.out

    def test_main_blocked_success(self, capsys):
        """Test successful blocked workflow."""
        with patch('sys.argv', [
            'agent-done', 'blocked',
            '--reason', 'Need API key',
            '--attempted', 'Checked env vars'
        ]):
            with patch('issue_orchestrator.agent_done.get_issue_number', return_value=789):
                with patch('issue_orchestrator.agent_done.get_repo', return_value='owner/repo'):
                    with patch('issue_orchestrator.agent_done.add_trailers_to_commit'):
                        with patch('issue_orchestrator.agent_done.git_push'):
                            with patch('issue_orchestrator.agent_done.add_label'):
                                with patch('issue_orchestrator.agent_done.post_comment'):
                                    main()

                                    captured = capsys.readouterr()
                                    assert "BLOCKED" in captured.out

    def test_main_blocked_with_blocked_by(self, capsys):
        """Test blocked workflow with blocked_by issues."""
        with patch('sys.argv', [
            'agent-done', 'blocked',
            '--reason', 'Depends on #123',
            '--attempted', 'Nothing',
            '--blocked-by', '123', '456'
        ]):
            with patch('issue_orchestrator.agent_done.get_issue_number', return_value=789):
                with patch('issue_orchestrator.agent_done.get_repo', return_value='owner/repo'):
                    with patch('issue_orchestrator.agent_done.add_trailers_to_commit'):
                        with patch('issue_orchestrator.agent_done.git_push'):
                            with patch('issue_orchestrator.agent_done.add_label'):
                                with patch('issue_orchestrator.agent_done.post_comment'):
                                    main()

                                    captured = capsys.readouterr()
                                    assert "BLOCKED" in captured.out

    def test_main_needs_human_success(self, capsys):
        """Test successful needs_human workflow."""
        with patch('sys.argv', [
            'agent-done', 'needs_human',
            '--question', 'Which approach?'
        ]):
            with patch('issue_orchestrator.agent_done.get_issue_number', return_value=111):
                with patch('issue_orchestrator.agent_done.get_repo', return_value='owner/repo'):
                    with patch('issue_orchestrator.agent_done.add_trailers_to_commit'):
                        with patch('issue_orchestrator.agent_done.git_push'):
                            with patch('issue_orchestrator.agent_done.add_label'):
                                with patch('issue_orchestrator.agent_done.post_comment'):
                                    main()

                                    captured = capsys.readouterr()
                                    assert "NEEDS HUMAN" in captured.out

    def test_main_needs_human_with_options(self, capsys):
        """Test needs_human with options and default."""
        with patch('sys.argv', [
            'agent-done', 'needs_human',
            '--question', 'Choose DB',
            '--context', 'Need storage',
            '--options', 'MySQL', 'PostgreSQL',
            '--default', 'PostgreSQL'
        ]):
            with patch('issue_orchestrator.agent_done.get_issue_number', return_value=222):
                with patch('issue_orchestrator.agent_done.get_repo', return_value='owner/repo'):
                    with patch('issue_orchestrator.agent_done.add_trailers_to_commit'):
                        with patch('issue_orchestrator.agent_done.git_push'):
                            with patch('issue_orchestrator.agent_done.add_label'):
                                with patch('issue_orchestrator.agent_done.post_comment'):
                                    main()

                                    captured = capsys.readouterr()
                                    assert "NEEDS HUMAN" in captured.out

    def test_main_missing_required_field(self):
        """Test error when required field is missing."""
        with patch('sys.argv', [
            'agent-done', 'completed',
            '--implementation', 'Added feature'
            # Missing --problems
        ]):
            with pytest.raises(SystemExit):
                main()

    def test_main_short_flags(self, capsys):
        """Test using short flag versions."""
        with patch('sys.argv', [
            'agent-done', 'completed',
            '-i', 'Implementation text',
            '-p', 'No problems',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.agent_done.get_issue_number', return_value=123):
                with patch('issue_orchestrator.agent_done.get_repo', return_value='owner/repo'):
                    main()

                    captured = capsys.readouterr()
                    assert "Implementation text" in captured.out

    def test_main_invalid_status(self):
        """Test error with invalid status."""
        with patch('sys.argv', ['agent-done', 'invalid']):
            with pytest.raises(SystemExit):
                main()

    def test_main_help_flag(self):
        """Test --help flag displays usage."""
        with patch('sys.argv', ['agent-done', '--help']):
            with pytest.raises(SystemExit) as exc_info:
                main()
            # argparse exits with 0 for --help
            assert exc_info.value.code == 0

    def test_main_no_args(self):
        """Test error when no arguments provided."""
        with patch('sys.argv', ['agent-done']):
            with pytest.raises(SystemExit):
                main()


class TestMainIntegration:
    """Integration-style tests for main function."""

    def test_completed_workflow_integration(self, capsys):
        """Test complete workflow for completed status."""
        args = [
            'agent-done', 'completed',
            '--implementation', 'Added OAuth support',
            '--problems', 'Had to update dependencies',
        ]

        with patch('sys.argv', args):
            with patch('subprocess.run') as mock_run:
                # Setup subprocess mocks for various git/gh calls
                mock_run.side_effect = [
                    # get_issue_number: git branch --show-current
                    Mock(returncode=0, stdout="123-add-oauth\n"),
                    # get_repo: gh repo view
                    Mock(returncode=0, stdout="owner/repo\n"),
                    # add_trailers_to_commit: git log
                    Mock(returncode=0, stdout="Initial commit\n"),
                    # add_trailers_to_commit: git commit --amend
                    Mock(returncode=0),
                    # git_push: git branch --show-current
                    Mock(returncode=0, stdout="123-add-oauth\n"),
                    # git_push: git push
                    Mock(returncode=0),
                    # create_pr: gh pr create
                    Mock(returncode=0, stdout="https://github.com/owner/repo/pull/5\n"),
                    # post_comment: gh issue comment
                    Mock(returncode=0, stdout="https://github.com/owner/repo/issues/123#issuecomment-999\n"),
                ]

                main()

                captured = capsys.readouterr()
                assert "Issue: #123" in captured.out
                assert "Status: completed" in captured.out
                assert "COMPLETED" in captured.out
                assert "https://github.com/owner/repo/pull/5" in captured.out

    def test_blocked_workflow_integration(self, capsys):
        """Test complete workflow for blocked status."""
        args = [
            'agent-done', 'blocked',
            '--reason', 'Waiting for API credentials',
            '--attempted', 'Tried using test credentials',
            '--blocked-by', '456',
        ]

        with patch('sys.argv', args):
            with patch('subprocess.run') as mock_run:
                mock_run.side_effect = [
                    # get_issue_number
                    Mock(returncode=0, stdout="789-api-integration\n"),
                    # get_repo
                    Mock(returncode=0, stdout="test/project\n"),
                    # add_trailers: git log
                    Mock(returncode=0, stdout="WIP commit\n"),
                    # add_trailers: git commit --amend
                    Mock(returncode=0),
                    # git_push: git branch
                    Mock(returncode=0, stdout="789-api-integration\n"),
                    # git_push: git push
                    Mock(returncode=0),
                    # add_label: gh issue edit
                    Mock(returncode=0),
                    # post_comment: gh issue comment
                    Mock(returncode=0, stdout="https://github.com/test/project/issues/789#issuecomment-111\n"),
                ]

                main()

                captured = capsys.readouterr()
                assert "Issue: #789" in captured.out
                assert "Status: blocked" in captured.out
                assert "BLOCKED" in captured.out

    def test_needs_human_workflow_integration(self, capsys):
        """Test complete workflow for needs_human status."""
        args = [
            'agent-done', 'needs_human',
            '--question', 'Should I refactor or patch?',
            '--context', 'Found legacy code that needs fixing',
            '--options', 'Full refactor', 'Quick patch',
            '--default', 'Quick patch for now',
        ]

        with patch('sys.argv', args):
            with patch('subprocess.run') as mock_run:
                mock_run.side_effect = [
                    # get_issue_number
                    Mock(returncode=0, stdout="999-legacy-fix\n"),
                    # get_repo
                    Mock(returncode=0, stdout="acme/legacy\n"),
                    # add_trailers: git log
                    Mock(returncode=0, stdout="Investigating\n"),
                    # add_trailers: git commit --amend
                    Mock(returncode=0),
                    # git_push: git branch
                    Mock(returncode=0, stdout="999-legacy-fix\n"),
                    # git_push: git push
                    Mock(returncode=0),
                    # add_label: gh issue edit
                    Mock(returncode=0),
                    # post_comment: gh issue comment
                    Mock(returncode=0, stdout="https://github.com/acme/legacy/issues/999#issuecomment-222\n"),
                ]

                main()

                captured = capsys.readouterr()
                assert "Issue: #999" in captured.out
                assert "Status: needs_human" in captured.out
                assert "NEEDS HUMAN" in captured.out


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_completion_data_with_none_strings(self):
        """Test that None string fields are properly handled in validation."""
        data = CompletionData(
            status=Status.COMPLETED,
            implementation=None,
            problems="None",
        )
        with pytest.raises(SystemExit):
            validate_fields(data)

    def test_whitespace_only_fields(self):
        """Test that whitespace-only fields are treated as empty."""
        data = CompletionData(
            status=Status.COMPLETED,
            implementation="   \t\n   ",
            problems="None",
        )
        with pytest.raises(SystemExit):
            validate_fields(data)

    def test_format_blocked_comment_empty_blocked_by_list(self):
        """Test blocked comment with empty blocked_by list."""
        data = CompletionData(
            status=Status.BLOCKED,
            reason="Some reason",
            attempted="Something",
            blocked_by=[],
        )
        comment = format_blocked_comment(data)
        # Empty list should not add blocked by line
        assert "**Blocked by:**" not in comment

    def test_format_needs_human_empty_options_list(self):
        """Test needs_human comment with empty options list."""
        data = CompletionData(
            status=Status.NEEDS_HUMAN,
            question="Question?",
            options=[],
        )
        comment = format_needs_human_comment(data)
        # Empty options list should not show options section (based on actual behavior)
        assert "**Options:**" not in comment

    def test_git_commands_with_special_characters_in_branch(self):
        """Test handling branch names with special characters."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="123-fix/bug-with-slashes\n"
            )
            issue_number = get_issue_number()
            assert issue_number == 123

    def test_blocked_by_single_vs_multiple(self):
        """Test blocked_by formatting with single and multiple issues."""
        # Single issue
        data_single = CompletionData(
            status=Status.BLOCKED,
            reason="Reason",
            attempted="Attempt",
            blocked_by=[100],
        )
        comment = format_blocked_comment(data_single)
        assert "**Blocked by:** #100" in comment

        # Multiple issues
        data_multi = CompletionData(
            status=Status.BLOCKED,
            reason="Reason",
            attempted="Attempt",
            blocked_by=[100, 200, 300],
        )
        comment = format_blocked_comment(data_multi)
        assert "**Blocked by:** #100, #200, #300" in comment

    def test_pr_url_replacement_in_comment(self):
        """Test that PR URL placeholder is replaced correctly."""
        data = CompletionData(
            status=Status.COMPLETED,
            implementation="Work done",
            problems="None",
        )
        comment = format_completion_comment(data)
        assert "<PR_LINK_PLACEHOLDER>" in comment

        # Simulate replacement
        updated = comment.replace("<PR_LINK_PLACEHOLDER>", "https://github.com/owner/repo/pull/1")
        assert "https://github.com/owner/repo/pull/1" in updated
        assert "<PR_LINK_PLACEHOLDER>" not in updated
