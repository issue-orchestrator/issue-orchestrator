"""Unit tests for agent_done module.

Tests the refactored agent_done which ONLY writes JSON completion records.
The orchestrator handles all side effects (push, PR, comments, labels).
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytest

from issue_orchestrator.entrypoints.cli_tools.agent_done import (
    AgentStatus,
    REQUIRED_FIELDS,
    STATUS_TO_OUTCOME,
    STATUS_TO_ACTIONS,
    die,
    get_session_id,
    validate_fields,
    format_comment_body,
    build_completion_record,
    write_completion_record,
    write_marker_file,
    main,
)
from issue_orchestrator.domain.models import (
    CompletionOutcome,
    RequestedAction,
    CompletionRecord,
    COMPLETION_RECORD_PATH,
)


class TestAgentStatus:
    """Test the AgentStatus constants."""

    def test_status_values(self):
        """Test all status values are correct."""
        assert AgentStatus.COMPLETED == "completed"
        assert AgentStatus.BLOCKED == "blocked"
        assert AgentStatus.NEEDS_HUMAN == "needs_human"
        assert AgentStatus.APPROVED == "approved"
        assert AgentStatus.CHANGES_REQUESTED == "changes_requested"


class TestRequiredFields:
    """Test REQUIRED_FIELDS constant."""

    def test_completed_required_fields(self):
        """Test required fields for completed status."""
        assert REQUIRED_FIELDS[AgentStatus.COMPLETED] == ["implementation", "problems"]

    def test_blocked_required_fields(self):
        """Test required fields for blocked status."""
        assert REQUIRED_FIELDS[AgentStatus.BLOCKED] == ["reason", "attempted"]

    def test_needs_human_required_fields(self):
        """Test required fields for needs_human status."""
        assert REQUIRED_FIELDS[AgentStatus.NEEDS_HUMAN] == ["question"]

    def test_approved_required_fields(self):
        """Test required fields for approved status include risk."""
        assert REQUIRED_FIELDS[AgentStatus.APPROVED] == ["summary", "risk"]

    def test_changes_requested_required_fields(self):
        """Test required fields for changes_requested status include risk."""
        assert REQUIRED_FIELDS[AgentStatus.CHANGES_REQUESTED] == ["issues", "risk"]


class TestStatusToOutcome:
    """Test STATUS_TO_OUTCOME mapping."""

    def test_completed_maps_to_completed(self):
        """Test completed status maps to COMPLETED outcome."""
        assert STATUS_TO_OUTCOME[AgentStatus.COMPLETED] == CompletionOutcome.COMPLETED

    def test_blocked_maps_to_blocked(self):
        """Test blocked status maps to BLOCKED outcome."""
        assert STATUS_TO_OUTCOME[AgentStatus.BLOCKED] == CompletionOutcome.BLOCKED

    def test_needs_human_maps_to_needs_human(self):
        """Test needs_human status maps to NEEDS_HUMAN outcome."""
        assert STATUS_TO_OUTCOME[AgentStatus.NEEDS_HUMAN] == CompletionOutcome.NEEDS_HUMAN

    def test_approved_maps_to_review_approved(self):
        """Test approved status maps to REVIEW_APPROVED outcome."""
        assert STATUS_TO_OUTCOME[AgentStatus.APPROVED] == CompletionOutcome.REVIEW_APPROVED

    def test_changes_requested_maps_to_review_changes_requested(self):
        """Test changes_requested status maps to REVIEW_CHANGES_REQUESTED outcome."""
        assert STATUS_TO_OUTCOME[AgentStatus.CHANGES_REQUESTED] == CompletionOutcome.REVIEW_CHANGES_REQUESTED


class TestStatusToActions:
    """Test STATUS_TO_ACTIONS mapping."""

    def test_completed_actions(self):
        """Test completed status requests push, PR, and comment."""
        actions = STATUS_TO_ACTIONS[AgentStatus.COMPLETED]
        assert RequestedAction.PUSH_BRANCH in actions
        assert RequestedAction.CREATE_PR in actions
        assert RequestedAction.POST_COMMENT in actions

    def test_blocked_actions(self):
        """Test blocked status requests push, label, and comment."""
        actions = STATUS_TO_ACTIONS[AgentStatus.BLOCKED]
        assert RequestedAction.PUSH_BRANCH in actions
        assert RequestedAction.ADD_BLOCKED_LABEL in actions
        assert RequestedAction.POST_COMMENT in actions

    def test_needs_human_actions(self):
        """Test needs_human status requests push, label, and comment."""
        actions = STATUS_TO_ACTIONS[AgentStatus.NEEDS_HUMAN]
        assert RequestedAction.PUSH_BRANCH in actions
        assert RequestedAction.ADD_NEEDS_HUMAN_LABEL in actions
        assert RequestedAction.POST_COMMENT in actions

    def test_approved_actions(self):
        """Test approved status requests label changes and comment."""
        actions = STATUS_TO_ACTIONS[AgentStatus.APPROVED]
        assert RequestedAction.ADD_CODE_REVIEWED_LABEL in actions
        assert RequestedAction.REMOVE_CODE_REVIEW_LABEL in actions
        assert RequestedAction.POST_COMMENT in actions

    def test_changes_requested_actions(self):
        """Test changes_requested status requests label changes and comment."""
        actions = STATUS_TO_ACTIONS[AgentStatus.CHANGES_REQUESTED]
        assert RequestedAction.ADD_NEEDS_REWORK_LABEL in actions
        assert RequestedAction.REMOVE_CODE_REVIEW_LABEL in actions
        assert RequestedAction.POST_COMMENT in actions


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


class TestGetSessionId:
    """Test the get_session_id function."""

    def test_get_session_id_from_env(self):
        """Test getting session ID from environment variable."""
        with patch.dict(os.environ, {"ORCHESTRATOR_SESSION_ID": "test-session-123"}):
            session_id = get_session_id()
            assert session_id == "test-session-123"

    def test_get_session_id_standalone_fallback(self):
        """Test standalone session ID when env var not set."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove the env var if it exists
            os.environ.pop("ORCHESTRATOR_SESSION_ID", None)
            session_id = get_session_id()
            assert session_id.startswith("standalone-")
            # Should contain date-time pattern
            assert "-" in session_id


class TestValidateFields:
    """Test the validate_fields function."""

    def _make_args(self, **kwargs):
        """Helper to create argparse.Namespace with given values."""
        defaults = {
            "implementation": None,
            "problems": None,
            "reason": None,
            "attempted": None,
            "blocked_by": None,
            "when_unblocked": None,
            "question": None,
            "context": None,
            "options": None,
            "default": None,
            "summary": None,
            "issues": None,
            "risk": None,
            "checks": None,
            "checks_needed": None,
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_validate_completed_with_all_fields(self):
        """Test validation passes for completed with all required fields."""
        args = self._make_args(
            implementation="Added feature",
            problems="None",
        )
        # Should not raise
        validate_fields(AgentStatus.COMPLETED, args)

    def test_validate_completed_missing_implementation(self):
        """Test validation fails when implementation is missing."""
        args = self._make_args(problems="None")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.COMPLETED, args)

    def test_validate_completed_missing_problems(self):
        """Test validation fails when problems is missing."""
        args = self._make_args(implementation="Added feature")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.COMPLETED, args)

    def test_validate_completed_empty_implementation(self):
        """Test validation fails when implementation is empty string."""
        args = self._make_args(
            implementation="   ",
            problems="None",
        )
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.COMPLETED, args)

    def test_validate_blocked_with_all_fields(self):
        """Test validation passes for blocked with all required fields."""
        args = self._make_args(
            reason="Waiting for API",
            attempted="Tried workaround",
        )
        validate_fields(AgentStatus.BLOCKED, args)

    def test_validate_blocked_missing_reason(self):
        """Test validation fails when reason is missing."""
        args = self._make_args(attempted="Tried workaround")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.BLOCKED, args)

    def test_validate_blocked_missing_attempted(self):
        """Test validation fails when attempted is missing."""
        args = self._make_args(reason="Waiting for API")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.BLOCKED, args)

    def test_validate_needs_human_with_question(self):
        """Test validation passes for needs_human with question."""
        args = self._make_args(question="Which approach?")
        validate_fields(AgentStatus.NEEDS_HUMAN, args)

    def test_validate_needs_human_missing_question(self):
        """Test validation fails when question is missing."""
        args = self._make_args()
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.NEEDS_HUMAN, args)

    def test_validate_approved_with_all_fields(self):
        """Test validation passes for approved with summary and risk."""
        args = self._make_args(
            summary="Code looks good",
            risk="low",
        )
        validate_fields(AgentStatus.APPROVED, args)

    def test_validate_approved_missing_summary(self):
        """Test validation fails when summary is missing."""
        args = self._make_args(risk="low")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.APPROVED, args)

    def test_validate_approved_missing_risk(self):
        """Test validation fails when risk is missing."""
        args = self._make_args(summary="LGTM")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.APPROVED, args)

    def test_validate_changes_requested_with_all_fields(self):
        """Test validation passes for changes_requested with issues and risk."""
        args = self._make_args(
            issues="Need more tests",
            risk="medium",
        )
        validate_fields(AgentStatus.CHANGES_REQUESTED, args)

    def test_validate_changes_requested_missing_issues(self):
        """Test validation fails when issues is missing."""
        args = self._make_args(risk="high")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.CHANGES_REQUESTED, args)

    def test_validate_changes_requested_missing_risk(self):
        """Test validation fails when risk is missing."""
        args = self._make_args(issues="Security issue")
        with pytest.raises(SystemExit):
            validate_fields(AgentStatus.CHANGES_REQUESTED, args)


class TestFormatCommentBody:
    """Test the format_comment_body function."""

    def _make_args(self, **kwargs):
        """Helper to create argparse.Namespace with given values."""
        defaults = {
            "implementation": None,
            "problems": None,
            "reason": None,
            "attempted": None,
            "blocked_by": None,
            "when_unblocked": None,
            "question": None,
            "context": None,
            "options": None,
            "default": None,
            "summary": None,
            "issues": None,
            "risk": None,
            "checks": None,
            "checks_needed": None,
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_format_completed_comment(self):
        """Test formatting completed comment."""
        args = self._make_args(
            implementation="Added user authentication",
            problems="None",
        )
        comment = format_comment_body(AgentStatus.COMPLETED, args)
        assert "## Implementation" in comment
        assert "Added user authentication" in comment
        assert "## Problems Encountered" in comment
        assert "None" in comment

    def test_format_blocked_comment_basic(self):
        """Test formatting basic blocked comment."""
        args = self._make_args(
            reason="Need API credentials",
            attempted="Checked environment variables",
            blocked_by=None,
            when_unblocked=None,
        )
        comment = format_comment_body(AgentStatus.BLOCKED, args)
        assert "## Blocked" in comment
        assert "**Reason:** Need API credentials" in comment
        assert "**Attempted:** Checked environment variables" in comment

    def test_format_blocked_comment_with_blocked_by(self):
        """Test formatting blocked comment with blocked_by issues."""
        args = self._make_args(
            reason="Depends on other work",
            attempted="Tried to work around it",
            blocked_by=[123, 456],
        )
        comment = format_comment_body(AgentStatus.BLOCKED, args)
        assert "**Blocked by:** #123, #456" in comment

    def test_format_blocked_comment_with_when_unblocked(self):
        """Test formatting blocked comment with when_unblocked hint."""
        args = self._make_args(
            reason="Waiting for API docs",
            attempted="Searched existing docs",
            when_unblocked="Implement auth flow using new endpoints",
        )
        comment = format_comment_body(AgentStatus.BLOCKED, args)
        assert "**When unblocked:** Implement auth flow using new endpoints" in comment

    def test_format_needs_human_comment_basic(self):
        """Test formatting basic needs_human comment."""
        args = self._make_args(
            question="Should we use approach A or B?",
        )
        comment = format_comment_body(AgentStatus.NEEDS_HUMAN, args)
        assert "## Needs Human Input" in comment
        assert "**Question:** Should we use approach A or B?" in comment

    def test_format_needs_human_comment_with_options(self):
        """Test formatting needs_human comment with options."""
        args = self._make_args(
            question="Which framework?",
            options=["React", "Vue", "Angular"],
        )
        comment = format_comment_body(AgentStatus.NEEDS_HUMAN, args)
        assert "**Options:**" in comment
        assert "1. React" in comment
        assert "2. Vue" in comment
        assert "3. Angular" in comment

    def test_format_needs_human_comment_with_default(self):
        """Test formatting needs_human comment with default action."""
        args = self._make_args(
            question="Proceed with deployment?",
            default="Will deploy in 24 hours",
        )
        comment = format_comment_body(AgentStatus.NEEDS_HUMAN, args)
        assert "**Default if no response:** Will deploy in 24 hours" in comment

    def test_format_approved_comment(self):
        """Test formatting approved comment with structured verdict."""
        args = self._make_args(
            summary="Code is clean and well-tested",
            risk="low",
            checks=["tests_added", "follows_patterns"],
        )
        comment = format_comment_body(AgentStatus.APPROVED, args)
        assert "## Code Review Approved" in comment
        assert "Code is clean and well-tested" in comment
        assert "**Verdict:** `approve`" in comment
        assert "`low`" in comment
        assert "`tests_added`" in comment

    def test_format_changes_requested_comment(self):
        """Test formatting changes_requested comment with structured verdict."""
        args = self._make_args(
            issues="Missing error handling in auth module",
            risk="medium",
            checks_needed=["error_handling", "tests_added"],
        )
        comment = format_comment_body(AgentStatus.CHANGES_REQUESTED, args)
        assert "## Changes Requested" in comment
        assert "Missing error handling in auth module" in comment
        assert "**Verdict:** `request_changes`" in comment
        assert "`medium`" in comment
        assert "`error_handling`" in comment


class TestBuildCompletionRecord:
    """Test the build_completion_record function."""

    def _make_args(self, **kwargs):
        """Helper to create argparse.Namespace with given values."""
        defaults = {
            "implementation": None,
            "problems": None,
            "reason": None,
            "attempted": None,
            "blocked_by": None,
            "when_unblocked": None,
            "question": None,
            "context": None,
            "options": None,
            "default": None,
            "summary": None,
            "issues": None,
            "risk": None,
            "checks": None,
            "checks_needed": None,
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_build_completed_record(self):
        """Test building completion record for completed status."""
        args = self._make_args(
            implementation="Added user auth",
            problems="None",
        )
        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.COMPLETED, args)

        assert record.session_id == "test-session"
        assert record.outcome == CompletionOutcome.COMPLETED
        assert record.implementation == "Added user auth"
        assert record.problems == "None"
        assert RequestedAction.PUSH_BRANCH in record.requested_actions
        assert RequestedAction.CREATE_PR in record.requested_actions

    def test_build_blocked_record(self):
        """Test building completion record for blocked status."""
        args = self._make_args(
            reason="Need API key",
            attempted="Checked env",
            blocked_by=[123],
            when_unblocked="Implement auth",
        )
        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.BLOCKED, args)

        assert record.outcome == CompletionOutcome.BLOCKED
        assert record.blocked_reason == "Need API key"
        assert record.attempted == "Checked env"
        assert record.blocked_by == [123]
        assert record.when_unblocked == "Implement auth"
        assert RequestedAction.ADD_BLOCKED_LABEL in record.requested_actions

    def test_build_needs_human_record(self):
        """Test building completion record for needs_human status."""
        args = self._make_args(
            question="Which approach?",
            context="Two options available",
            options=["A", "B"],
            default="A",
        )
        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.NEEDS_HUMAN, args)

        assert record.outcome == CompletionOutcome.NEEDS_HUMAN
        assert record.question == "Which approach?"
        assert record.context == "Two options available"
        assert record.options == ["A", "B"]
        assert record.default_action == "A"
        assert RequestedAction.ADD_NEEDS_HUMAN_LABEL in record.requested_actions

    def test_build_approved_record(self):
        """Test building completion record for approved status."""
        args = self._make_args(
            summary="LGTM",
            risk="low",
            checks=["tests_added"],
        )
        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.APPROVED, args)

        assert record.outcome == CompletionOutcome.REVIEW_APPROVED
        assert record.review_summary == "LGTM"
        assert record.risk_level == "low"
        assert record.checks_passed == ["tests_added"]
        assert RequestedAction.ADD_CODE_REVIEWED_LABEL in record.requested_actions

    def test_build_changes_requested_record(self):
        """Test building completion record for changes_requested status."""
        args = self._make_args(
            issues="Missing tests",
            risk="high",
            checks_needed=["tests_added", "error_handling"],
        )
        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.CHANGES_REQUESTED, args)

        assert record.outcome == CompletionOutcome.REVIEW_CHANGES_REQUESTED
        assert record.review_issues == "Missing tests"
        assert record.risk_level == "high"
        assert record.checks_needed == ["tests_added", "error_handling"]
        assert RequestedAction.ADD_NEEDS_REWORK_LABEL in record.requested_actions


class TestWriteCompletionRecord:
    """Test the write_completion_record function."""

    def test_write_completion_record_creates_file(self, tmp_path):
        """Test that write_completion_record creates the JSON file."""
        # Create a fake git repo structure
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        record = CompletionRecord(
            session_id="test-session",
            timestamp="2024-01-01T12:00:00",
            outcome=CompletionOutcome.COMPLETED,
            summary="Test summary",
            requested_actions=[RequestedAction.PUSH_BRANCH],
        )

        # Change to tmp_path and write
        original_cwd = Path.cwd()
        original_completion_path = os.environ.pop("ORCHESTRATOR_COMPLETION_PATH", None)
        try:
            os.chdir(tmp_path)
            output_path = write_completion_record(record)

            assert output_path.exists()
            assert str(output_path).endswith(COMPLETION_RECORD_PATH)

            # Verify JSON content
            with open(output_path) as f:
                data = json.load(f)
            assert data["session_id"] == "test-session"
            assert data["outcome"] == "completed"
        finally:
            os.chdir(original_cwd)
            if original_completion_path is not None:
                os.environ["ORCHESTRATOR_COMPLETION_PATH"] = original_completion_path

    def test_write_completion_record_creates_directory(self, tmp_path):
        """Test that write_completion_record creates .issue-orchestrator directory."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        record = CompletionRecord(
            session_id="test-session",
            timestamp="2024-01-01T12:00:00",
            outcome=CompletionOutcome.BLOCKED,
            summary="Test",
            requested_actions=[],
        )

        original_cwd = Path.cwd()
        original_completion_path = os.environ.pop("ORCHESTRATOR_COMPLETION_PATH", None)
        try:
            os.chdir(tmp_path)
            write_completion_record(record)

            assert (tmp_path / ".issue-orchestrator").exists()
            assert (tmp_path / ".issue-orchestrator").is_dir()
        finally:
            os.chdir(original_cwd)
            if original_completion_path is not None:
                os.environ["ORCHESTRATOR_COMPLETION_PATH"] = original_completion_path


class TestWriteMarkerFile:
    """Test the write_marker_file function."""

    def test_write_marker_file_creates_file(self, tmp_path):
        """Test that write_marker_file creates the marker file."""
        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            write_marker_file("completed")

            marker_path = tmp_path / ".agent-done-marker"
            assert marker_path.exists()

            content = marker_path.read_text()
            assert "agent-done completed called at" in content
        finally:
            os.chdir(original_cwd)


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
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                main()

                captured = capsys.readouterr()
                assert "DRY RUN" in captured.out
                assert "completed" in captured.out

    def test_main_blocked_dry_run(self, capsys):
        """Test dry run mode for blocked status."""
        with patch('sys.argv', [
            'agent-done', 'blocked',
            '--reason', 'Need API key',
            '--attempted', 'Checked env vars',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                main()

                captured = capsys.readouterr()
                assert "DRY RUN" in captured.out
                assert "blocked" in captured.out

    def test_main_needs_human_dry_run(self, capsys):
        """Test dry run mode for needs_human status."""
        with patch('sys.argv', [
            'agent-done', 'needs_human',
            '--question', 'Which approach?',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                main()

                captured = capsys.readouterr()
                assert "DRY RUN" in captured.out
                assert "needs_human" in captured.out

    def test_main_approved_dry_run(self, capsys):
        """Test dry run mode for approved status."""
        with patch('sys.argv', [
            'agent-done', 'approved',
            '--summary', 'LGTM',
            '--risk', 'low',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                main()

                captured = capsys.readouterr()
                assert "DRY RUN" in captured.out
                assert "review_approved" in captured.out

    def test_main_changes_requested_dry_run(self, capsys):
        """Test dry run mode for changes_requested status."""
        with patch('sys.argv', [
            'agent-done', 'changes_requested',
            '--issues', 'Missing tests',
            '--risk', 'medium',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                main()

                captured = capsys.readouterr()
                assert "DRY RUN" in captured.out
                assert "review_changes_requested" in captured.out

    def test_main_missing_required_field(self):
        """Test error when required field is missing."""
        with patch('sys.argv', [
            'agent-done', 'completed',
            '--implementation', 'Added feature'
            # Missing --problems
        ]):
            with pytest.raises(SystemExit):
                main()

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

    def test_main_writes_completion_record(self, tmp_path):
        """Test that main writes completion record to file."""
        # Create fake git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        original_cwd = Path.cwd()
        original_completion_path = os.environ.pop("ORCHESTRATOR_COMPLETION_PATH", None)
        try:
            os.chdir(tmp_path)

            with patch('sys.argv', [
                'agent-done', 'completed',
                '--implementation', 'Added feature',
                '--problems', 'None',
            ]):
                with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                    main()

            # Check file was written
            record_path = tmp_path / COMPLETION_RECORD_PATH
            assert record_path.exists()

            with open(record_path) as f:
                data = json.load(f)
            assert data["session_id"] == "test-123"
            assert data["outcome"] == "completed"
            assert data["implementation"] == "Added feature"
        finally:
            os.chdir(original_cwd)
            if original_completion_path is not None:
                os.environ["ORCHESTRATOR_COMPLETION_PATH"] = original_completion_path

    def test_main_writes_marker_file(self, tmp_path):
        """Test that main writes marker file."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            with patch('sys.argv', [
                'agent-done', 'blocked',
                '--reason', 'Need API',
                '--attempted', 'Checked env',
            ]):
                with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                    main()

            # Check marker file was written
            marker_path = tmp_path / ".agent-done-marker"
            assert marker_path.exists()
            content = marker_path.read_text()
            assert "agent-done blocked called at" in content
        finally:
            os.chdir(original_cwd)


class TestShortFlags:
    """Test short flag versions."""

    def test_short_flags_completed(self, capsys):
        """Test using short flags for completed status."""
        with patch('sys.argv', [
            'agent-done', 'completed',
            '-i', 'Implementation text',
            '-p', 'No problems',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                main()

                captured = capsys.readouterr()
                assert "Implementation text" in captured.out

    def test_short_flags_blocked(self, capsys):
        """Test using short flags for blocked status."""
        with patch('sys.argv', [
            'agent-done', 'blocked',
            '-r', 'Reason text',
            '-a', 'Attempted text',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                main()

                captured = capsys.readouterr()
                assert "Reason text" in captured.out

    def test_short_flags_needs_human(self, capsys):
        """Test using short flags for needs_human status."""
        with patch('sys.argv', [
            'agent-done', 'needs_human',
            '-q', 'Question text',
            '-c', 'Context text',
            '--dry-run'
        ]):
            with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                main()

                captured = capsys.readouterr()
                assert "Question text" in captured.out


class TestCompletionRecordSerialization:
    """Test that completion records can be serialized and deserialized."""

    def test_roundtrip_completed(self):
        """Test serialization roundtrip for completed record."""
        args = argparse.Namespace(
            implementation="Added feature X",
            problems="None encountered",
            reason=None,
            attempted=None,
            blocked_by=None,
            when_unblocked=None,
            question=None,
            context=None,
            options=None,
            default=None,
            summary=None,
            issues=None,
            risk=None,
            checks=None,
            checks_needed=None,
        )

        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.COMPLETED, args)

        # Serialize and deserialize
        data = record.to_dict()
        restored = CompletionRecord.from_dict(data)

        assert restored.session_id == record.session_id
        assert restored.outcome == record.outcome
        assert restored.implementation == record.implementation
        assert restored.problems == record.problems
        assert len(restored.requested_actions) == len(record.requested_actions)

    def test_roundtrip_blocked(self):
        """Test serialization roundtrip for blocked record."""
        args = argparse.Namespace(
            implementation=None,
            problems=None,
            reason="Waiting for API",
            attempted="Checked docs",
            blocked_by=[123, 456],
            when_unblocked="Implement after API ready",
            question=None,
            context=None,
            options=None,
            default=None,
            summary=None,
            issues=None,
            risk=None,
            checks=None,
            checks_needed=None,
        )

        with patch("issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id", return_value="test-session"):
            record = build_completion_record(AgentStatus.BLOCKED, args)

        data = record.to_dict()
        restored = CompletionRecord.from_dict(data)

        assert restored.blocked_reason == record.blocked_reason
        assert restored.blocked_by == record.blocked_by
        assert restored.when_unblocked == record.when_unblocked


class TestAgentGateIntegration:
    """Test agent gate validation integration in the main function."""

    def test_agent_gate_runs_when_configured(self, tmp_path, capsys):
        """Test that agent gate validation runs when configured."""
        # Create fake git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (tmp_path / "README.md").write_text("test")

        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=tmp_path, capture_output=True)

        # Create config with passing validation
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "echo 'ok'"
  timeout_seconds: 10
""")

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            with patch('sys.argv', [
                'agent-done', 'completed',
                '--implementation', 'Added feature',
                '--problems', 'None',
                '--verbose'
            ]):
                with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                    main()

            captured = capsys.readouterr()
            assert "Validation passed" in captured.out
        finally:
            os.chdir(original_cwd)

    def test_validation_failure_exits_with_error(self, tmp_path, capsys):
        """Test that validation failure exits with error and writes diagnostics."""
        # Create fake git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (tmp_path / "README.md").write_text("test")

        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=tmp_path, capture_output=True)

        # Create config with failing validation
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "exit 1"
  timeout_seconds: 10
""")

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            with patch('sys.argv', [
                'agent-done', 'completed',
                '--implementation', 'Added feature',
                '--problems', 'None',
                '--verbose'
            ]):
                with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                    with pytest.raises(SystemExit) as exc_info:
                        main()
                    # Should exit with error code 1
                    assert exc_info.value.code == 1

            captured = capsys.readouterr()
            # Should print error about failure
            assert "Validation failed" in captured.out
            # Should NOT write completion record (exited before that)
            assert "Completion record written to" not in captured.out
            # Should write validation details to session output
            manifest_path = (
                tmp_path
                / ".issue-orchestrator"
                / "sessions"
                / "test-123"
                / "manifest.json"
            )
            assert manifest_path.exists()
            manifest = json.loads(manifest_path.read_text())
            assert manifest.get("validation_record_path")
            assert manifest.get("validation_status") == "failed"
        finally:
            os.chdir(original_cwd)

    def test_validation_skipped_with_flag(self, tmp_path, capsys):
        """Test that validation can be skipped with --skip-validation."""
        # Create fake git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (tmp_path / "README.md").write_text("test")

        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=tmp_path, capture_output=True)

        # Create config with validation that would fail
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "exit 1"
  timeout_seconds: 10
""")

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            with patch('sys.argv', [
                'agent-done', 'completed',
                '--implementation', 'Added feature',
                '--problems', 'None',
                '--skip-validation'
            ]):
                with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                    main()

            captured = capsys.readouterr()
            # Should NOT print validation failure (skipped)
            assert "Validation failed" not in captured.out
            # Should still write completion record
            assert "Completion record written to" in captured.out
            # Should not show validation status
            assert "Running validation" not in captured.out
        finally:
            os.chdir(original_cwd)

    def test_validation_failure_shows_stderr_inline(self, tmp_path, capsys):
        """Test that validation failure shows the actual error output inline.

        This verifies Claude can see what failed without reading separate files.
        """
        # Create fake git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (tmp_path / "README.md").write_text("test")

        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=tmp_path, capture_output=True)

        # Create config with validation that outputs error to stderr
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        # This command outputs to stderr and exits with error
        config_path.write_text("""
validation:
  cmd: "echo 'FAILED test_something.py::test_case - AssertionError' >&2 && exit 1"
  timeout_seconds: 10
""")

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            with patch('sys.argv', [
                'agent-done', 'completed',
                '--implementation', 'Added feature',
                '--problems', 'None',
            ]):
                with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                    with pytest.raises(SystemExit) as exc_info:
                        main()
                    assert exc_info.value.code == 1

            captured = capsys.readouterr()

            # Verify the new output format
            assert "VALIDATION FAILED" in captured.out
            assert "agent-done cannot complete" in captured.out
            assert "--- STDERR (what failed) ---" in captured.out
            assert "FAILED test_something.py::test_case - AssertionError" in captured.out
            assert "--- END STDERR ---" in captured.out
            assert "TO FIX:" in captured.out
            assert 'agent-done blocked --reason "Validation failing:' in captured.out
        finally:
            os.chdir(original_cwd)

    def test_blocked_status_skips_validation(self, tmp_path, capsys):
        """Test that blocked status skips validation entirely.

        This is important because if tests fail and agent can't fix them,
        they should be able to report blocked without validation blocking them.
        """
        # Create fake git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (tmp_path / "README.md").write_text("test")

        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial"], cwd=tmp_path, capture_output=True)

        # Create config with validation that would fail
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "exit 1"
  timeout_seconds: 10
""")

        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)

            with patch('sys.argv', [
                'agent-done', 'blocked',
                '--reason', 'Tests failing and cannot fix',
                '--attempted', 'Tried multiple fixes',
            ]):
                with patch('issue_orchestrator.entrypoints.cli_tools.agent_done.get_session_id', return_value='test-123'):
                    # Should NOT raise - blocked skips validation
                    main()

            captured = capsys.readouterr()
            # Should indicate validation was skipped
            assert "Skipping validation for 'blocked' status" in captured.out
            # Should still write completion record
            assert "Completion record written to" in captured.out
        finally:
            os.chdir(original_cwd)
