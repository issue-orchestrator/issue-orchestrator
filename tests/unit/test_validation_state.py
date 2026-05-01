"""Tests for validation retry state management."""

import json
import pytest
from pathlib import Path

from issue_orchestrator.infra.validation_state import (
    ValidationState,
    read_validation_state,
    write_validation_state,
    write_validation_errors,
    write_retry_prompt,
    clear_validation_state,
    has_pending_retry,
    get_retry_prompt_path,
    VALIDATION_STATE_FILE,
    VALIDATION_ERRORS_FILE,
    RETRY_PROMPT_FILE,
)


class TestValidationState:
    """Tests for ValidationState dataclass."""

    def test_default_values(self):
        """State has sensible defaults."""
        state = ValidationState()
        assert state.retry_count == 0
        assert state.max_retries == 3
        assert state.can_retry is True
        assert state.retries_remaining == 3

    def test_increment_retry(self):
        """Incrementing creates new state with higher count."""
        state = ValidationState(retry_count=0, max_retries=3)
        new_state = state.increment_retry()

        assert new_state.retry_count == 1
        assert new_state.max_retries == 3
        assert state.retry_count == 0  # Original unchanged

    def test_can_retry_boundary(self):
        """can_retry remains true for the final retry attempt."""
        state = ValidationState(retry_count=2, max_retries=3)
        assert state.can_retry is True

        state = ValidationState(retry_count=3, max_retries=3)
        assert state.can_retry is True

        state = ValidationState(retry_count=4, max_retries=3)
        assert state.can_retry is False

        state = ValidationState(retry_count=0, max_retries=0)
        assert state.can_retry is False

    def test_retries_remaining(self):
        """retries_remaining calculates correctly."""
        state = ValidationState(retry_count=1, max_retries=3)
        assert state.retries_remaining == 2

        state = ValidationState(retry_count=5, max_retries=3)
        assert state.retries_remaining == 0  # Never negative


class TestReadWriteState:
    """Tests for reading/writing validation state."""

    def test_write_then_read(self, tmp_path: Path):
        """Can write and read back state."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        original = ValidationState(
            retry_count=2,
            max_retries=5,
            validation_cmd="make validate",
            last_error="Test failed",
        )
        write_validation_state(worktree, original)

        loaded = read_validation_state(worktree)
        assert loaded is not None
        assert loaded.retry_count == 2
        assert loaded.max_retries == 5
        assert loaded.validation_cmd == "make validate"
        assert loaded.last_error == "Test failed"

    def test_read_nonexistent_returns_none(self, tmp_path: Path):
        """Reading from worktree without state returns None."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        state = read_validation_state(worktree)
        assert state is None

    def test_write_creates_directory(self, tmp_path: Path):
        """Writing creates .issue-orchestrator directory."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        state = ValidationState()
        write_validation_state(worktree, state)

        state_dir = worktree / ".issue-orchestrator"
        assert state_dir.exists()
        assert (state_dir / VALIDATION_STATE_FILE).exists()

    def test_write_sets_timestamps(self, tmp_path: Path):
        """Writing sets created_at and updated_at."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        state = ValidationState()
        write_validation_state(worktree, state)

        loaded = read_validation_state(worktree)
        assert loaded.created_at is not None
        assert loaded.updated_at is not None

    def test_read_corrupted_returns_none(self, tmp_path: Path):
        """Reading corrupted JSON returns None."""
        worktree = tmp_path / "worktree"
        state_dir = worktree / ".issue-orchestrator"
        state_dir.mkdir(parents=True)

        state_file = state_dir / VALIDATION_STATE_FILE
        state_file.write_text("not valid json {{{")

        state = read_validation_state(worktree)
        assert state is None


class TestWriteValidationErrors:
    """Tests for writing validation error output."""

    def test_write_errors(self, tmp_path: Path):
        """Writes formatted error file."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        path = write_validation_errors(
            worktree,
            validation_cmd="pytest tests/",
            stdout="Running tests...",
            stderr="FAILED: test_foo.py::test_bar",
            exit_code=1,
        )

        assert path.exists()
        content = path.read_text()
        assert "pytest tests/" in content
        assert "Exit code: 1" in content
        assert "FAILED: test_foo.py::test_bar" in content
        assert "Running tests..." in content


class TestWriteRetryPrompt:
    """Tests for writing retry prompt."""

    def test_write_retry_prompt(self, tmp_path: Path):
        """Writes formatted retry prompt."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        path = write_retry_prompt(
            worktree,
            original_prompt="Fix the login bug",
            validation_cmd="make validate",
            validation_error="TypeError: undefined is not a function",
            retry_count=1,
            max_retries=3,
        )

        assert path.exists()
        content = path.read_text()
        assert "Attempt 2/4" in content  # retry_count=1 means this is attempt 2
        assert "Fix the login bug" in content
        assert "make validate" in content
        assert "TypeError: undefined" in content
        assert "coding-done completed" in content

    def test_retry_prompt_truncates_long_errors(self, tmp_path: Path):
        """Long errors are truncated in prompt."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        long_error = "x" * 5000
        path = write_retry_prompt(
            worktree,
            original_prompt="Task",
            validation_cmd="cmd",
            validation_error=long_error,
            retry_count=0,
            max_retries=3,
        )

        content = path.read_text()
        # Error (5000 chars) is truncated by _truncate_with_tail to ~4000 chars,
        # so total content must be well under 5000 + template chrome.
        assert len(content) < len(long_error) + 2000
        assert "truncated" in content

    def test_custom_template_from_file(self, tmp_path: Path):
        """Custom template loaded from file."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        repo_root = tmp_path / "repo"
        repo_root.mkdir()

        # Create custom template
        template_dir = repo_root / ".prompts"
        template_dir.mkdir()
        template_file = template_dir / "retry.md"
        template_file.write_text(
            "CUSTOM RETRY\n"
            "Task: {original_task}\n"
            "Cmd: {validation_cmd}\n"
            "Error: {error_summary}\n"
            "Attempt {retry_count} of {max_retries}\n"
        )

        path = write_retry_prompt(
            worktree,
            original_prompt="Fix bug",
            validation_cmd="make test",
            validation_error="AssertionError",
            retry_count=1,
            max_retries=3,
            template_path=".prompts/retry.md",
            repo_root=repo_root,
        )

        content = path.read_text()
        assert "CUSTOM RETRY" in content
        assert "Task: Fix bug" in content
        assert "Cmd: make test" in content
        assert "Error: AssertionError" in content
        assert "Attempt 2 of 4" in content  # 1-based display

    def test_missing_template_uses_default(self, tmp_path: Path):
        """Missing template file falls back to default."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        repo_root = tmp_path / "repo"
        repo_root.mkdir()

        path = write_retry_prompt(
            worktree,
            original_prompt="Fix bug",
            validation_cmd="make test",
            validation_error="Error",
            retry_count=0,
            max_retries=3,
            template_path=".prompts/nonexistent.md",
            repo_root=repo_root,
        )

        content = path.read_text()
        # Should use default template which contains completion command instructions
        assert "coding-done completed" in content
        assert "coding-done blocked" in content

    def test_default_template_includes_blocked_option(self, tmp_path: Path):
        """Default template includes completion command blocked option."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        path = write_retry_prompt(
            worktree,
            original_prompt="Task",
            validation_cmd="cmd",
            validation_error="Error",
            retry_count=0,
            max_retries=3,
        )

        content = path.read_text()
        assert "coding-done blocked" in content
        assert "cannot fix" in content.lower() or "unable" in content.lower()


class TestClearValidationState:
    """Tests for clearing validation state."""

    def test_clear_removes_state_and_prompt(self, tmp_path: Path):
        """Clear removes state file and retry prompt."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Create state and prompt
        write_validation_state(worktree, ValidationState())
        write_retry_prompt(worktree, "task", "cmd", "error", 0, 3)
        write_validation_errors(worktree, "cmd", "out", "err", 1)

        state_file = worktree / ".issue-orchestrator" / VALIDATION_STATE_FILE
        prompt_file = worktree / ".issue-orchestrator" / RETRY_PROMPT_FILE
        errors_file = worktree / ".issue-orchestrator" / VALIDATION_ERRORS_FILE

        assert state_file.exists()
        assert prompt_file.exists()
        assert errors_file.exists()

        clear_validation_state(worktree)

        assert not state_file.exists()
        assert not prompt_file.exists()
        assert errors_file.exists()  # Kept for debugging

    def test_clear_handles_missing_files(self, tmp_path: Path):
        """Clear handles already-missing files gracefully."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Should not raise
        clear_validation_state(worktree)


class TestHasPendingRetry:
    """Tests for crash recovery detection."""

    def test_no_state_means_no_pending(self, tmp_path: Path):
        """No state file means no pending retry."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        assert has_pending_retry(worktree) is False

    def test_state_with_retries_remaining(self, tmp_path: Path):
        """State with retries remaining is pending."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        state = ValidationState(retry_count=1, max_retries=3)
        write_validation_state(worktree, state)

        assert has_pending_retry(worktree) is True

    def test_state_at_max_retry_is_pending_final_attempt(self, tmp_path: Path):
        """A queued final retry attempt is still recoverable."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        state = ValidationState(retry_count=3, max_retries=3)
        write_validation_state(worktree, state)

        assert has_pending_retry(worktree) is True

    def test_state_past_max_retries_not_pending(self, tmp_path: Path):
        """State past max retries is not pending."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        state = ValidationState(retry_count=4, max_retries=3)
        write_validation_state(worktree, state)

        assert has_pending_retry(worktree) is False


class TestGetRetryPromptPath:
    """Tests for getting retry prompt path."""

    def test_returns_none_if_no_prompt(self, tmp_path: Path):
        """Returns None if no retry prompt exists."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        assert get_retry_prompt_path(worktree) is None

    def test_returns_path_if_exists(self, tmp_path: Path):
        """Returns path if retry prompt exists."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        write_retry_prompt(worktree, "task", "cmd", "error", 0, 3)

        path = get_retry_prompt_path(worktree)
        assert path is not None
        assert path.exists()


class TestCrashRecoveryScenarios:
    """Integration tests for crash recovery scenarios."""

    def test_recover_mid_retry_session(self, tmp_path: Path):
        """Simulate crash during retry and recovery."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Simulate: validation failed, retry started, then crash
        state = ValidationState(
            retry_count=1,
            max_retries=3,
            validation_cmd="make test",
            last_error="AssertionError in test_foo",
        )
        write_validation_state(worktree, state)
        write_validation_errors(worktree, "make test", "", "AssertionError", 1)
        write_retry_prompt(worktree, "Original task", "make test", "AssertionError", 1, 3)

        # Simulate: orchestrator restarts
        # It should detect pending retry
        assert has_pending_retry(worktree) is True

        recovered_state = read_validation_state(worktree)
        assert recovered_state is not None
        assert recovered_state.retry_count == 1
        assert recovered_state.can_retry is True

        prompt_path = get_retry_prompt_path(worktree)
        assert prompt_path is not None

    def test_recover_max_retries_exhausted(self, tmp_path: Path):
        """Crash after max retries - should not retry again."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Simulate: all retries exhausted before crash
        state = ValidationState(retry_count=4, max_retries=3)
        write_validation_state(worktree, state)

        # On recovery, should NOT be pending
        assert has_pending_retry(worktree) is False

    def test_recover_no_state_is_fresh_start(self, tmp_path: Path):
        """No state file means fresh start (not mid-retry)."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # No validation state - this is either:
        # - Fresh issue
        # - Validation passed (state cleared)

        assert has_pending_retry(worktree) is False
        assert read_validation_state(worktree) is None

    def test_full_retry_cycle_then_success(self, tmp_path: Path):
        """Full cycle: fail -> retry -> succeed -> state cleared."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Initial validation failure
        state = ValidationState(
            retry_count=0,
            max_retries=3,
            validation_cmd="make test",
        )
        write_validation_state(worktree, state)
        write_validation_errors(worktree, "make test", "", "Error 1", 1)

        assert has_pending_retry(worktree) is True

        # Retry 1 - still fails
        state = state.increment_retry()
        write_validation_state(worktree, state)

        assert has_pending_retry(worktree) is True
        assert state.retry_count == 1

        # Retry 2 - succeeds!
        # On success, clear state
        clear_validation_state(worktree)

        assert has_pending_retry(worktree) is False
        assert read_validation_state(worktree) is None

        # Errors file kept for debugging
        errors_file = worktree / ".issue-orchestrator" / VALIDATION_ERRORS_FILE
        assert errors_file.exists()
