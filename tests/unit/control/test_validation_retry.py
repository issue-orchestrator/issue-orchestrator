"""Tests for ValidationRetryController."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator._vendor.agent_runner import AgentRunner, RunResult, RunSpec
from issue_orchestrator.control.validation_retry import (
    RETRY_PROMPT_TEMPLATE,
    ValidationResult,
    ValidationRetryController,
)
from issue_orchestrator.infra.config import RetryConfig
from issue_orchestrator.execution.command_runner import LocalCommandRunner


@pytest.fixture
def retry_config() -> RetryConfig:
    """Create a test retry config."""
    return RetryConfig(
        max_validation_retries=3,
        validation_error_file="validation-errors.txt",
    )


@pytest.fixture
def mock_runner() -> MagicMock:
    """Create a mock AgentRunner."""
    return MagicMock(spec=AgentRunner)


@pytest.fixture
def command_runner() -> LocalCommandRunner:
    """Create a real command runner for validation tests."""
    return LocalCommandRunner()


@pytest.fixture
def controller(mock_runner: MagicMock, retry_config: RetryConfig, command_runner: LocalCommandRunner) -> ValidationRetryController:
    """Create a controller with mocked dependencies."""
    return ValidationRetryController(runner=mock_runner, config=retry_config, command_runner=command_runner)


@pytest.fixture
def successful_run_result() -> RunResult:
    """Create a successful run result."""
    return RunResult(
        exit_code=0,
        stderr="",
        duration_seconds=10.0,
        timed_out=False,
        command=["agent", "run"],
    )


@pytest.fixture
def failed_run_result() -> RunResult:
    """Create a failed run result."""
    return RunResult(
        exit_code=1,
        stderr="Agent crashed",
        duration_seconds=5.0,
        timed_out=False,
        command=["agent", "run"],
    )


@pytest.fixture
def timeout_run_result() -> RunResult:
    """Create a timeout run result."""
    return RunResult(
        exit_code=None,
        stderr="",
        duration_seconds=300.0,
        timed_out=True,
        command=["agent", "run"],
    )


class TestValidationRetryController:
    """Tests for ValidationRetryController."""

    def test_happy_path_validation_passes(
        self,
        controller: ValidationRetryController,
        mock_runner: MagicMock,
        successful_run_result: RunResult,
        tmp_path: Path,
    ) -> None:
        """Test that when validation passes, we return success."""
        mock_runner.run.return_value = successful_run_result

        spec = RunSpec(
            command=["agent", "run"],
            working_dir=tmp_path,
            timeout_seconds=300,
            output_dir=tmp_path / "output",
        )

        with patch.object(controller, "run_validation_command") as mock_validate:
            mock_validate.return_value = (0, "All tests passed", "")

            result = controller.run_with_validation(
                spec=spec,
                validation_cmd="make validate",
                validation_timeout_seconds=60,
                session_output_dir=tmp_path / "session",
                original_prompt="Fix the bug",
                build_retry_spec=lambda p: spec,
            )

        assert result.validation_passed
        assert result.retry_count == 0
        assert mock_runner.run.call_count == 1

    def test_no_validation_configured(
        self,
        controller: ValidationRetryController,
        mock_runner: MagicMock,
        successful_run_result: RunResult,
        tmp_path: Path,
    ) -> None:
        """Test that when validation is not configured, we skip it."""
        mock_runner.run.return_value = successful_run_result

        spec = RunSpec(
            command=["agent", "run"],
            working_dir=tmp_path,
            timeout_seconds=300,
            output_dir=tmp_path / "output",
        )

        result = controller.run_with_validation(
            spec=spec,
            validation_cmd=None,  # No validation
            validation_timeout_seconds=60,
            session_output_dir=tmp_path / "session",
            original_prompt="Fix the bug",
            build_retry_spec=lambda p: spec,
        )

        assert result.validation_passed
        assert result.retry_count == 0

    def test_agent_fails_no_validation(
        self,
        controller: ValidationRetryController,
        mock_runner: MagicMock,
        failed_run_result: RunResult,
        tmp_path: Path,
    ) -> None:
        """Test that when agent fails, we don't run validation."""
        mock_runner.run.return_value = failed_run_result

        spec = RunSpec(
            command=["agent", "run"],
            working_dir=tmp_path,
            timeout_seconds=300,
            output_dir=tmp_path / "output",
        )

        with patch.object(controller, "run_validation_command") as mock_validate:
            result = controller.run_with_validation(
                spec=spec,
                validation_cmd="make validate",
                validation_timeout_seconds=60,
                session_output_dir=tmp_path / "session",
                original_prompt="Fix the bug",
                build_retry_spec=lambda p: spec,
            )

        assert not result.validation_passed
        assert result.retry_count == 0
        mock_validate.assert_not_called()

    def test_agent_timeout_no_validation(
        self,
        controller: ValidationRetryController,
        mock_runner: MagicMock,
        timeout_run_result: RunResult,
        tmp_path: Path,
    ) -> None:
        """Test that when agent times out, we don't run validation."""
        mock_runner.run.return_value = timeout_run_result

        spec = RunSpec(
            command=["agent", "run"],
            working_dir=tmp_path,
            timeout_seconds=300,
            output_dir=tmp_path / "output",
        )

        with patch.object(controller, "run_validation_command") as mock_validate:
            result = controller.run_with_validation(
                spec=spec,
                validation_cmd="make validate",
                validation_timeout_seconds=60,
                session_output_dir=tmp_path / "session",
                original_prompt="Fix the bug",
                build_retry_spec=lambda p: spec,
            )

        assert not result.validation_passed
        assert result.run_result.timed_out
        mock_validate.assert_not_called()

    def test_validation_fails_retry_succeeds(
        self,
        controller: ValidationRetryController,
        mock_runner: MagicMock,
        successful_run_result: RunResult,
        tmp_path: Path,
    ) -> None:
        """Test that when validation fails once but succeeds on retry."""
        mock_runner.run.return_value = successful_run_result

        spec = RunSpec(
            command=["agent", "run"],
            working_dir=tmp_path,
            timeout_seconds=300,
            output_dir=tmp_path / "output",
        )

        validation_results = [
            (1, "", "Test failed: assertion error"),  # First fails
            (0, "All tests passed", ""),  # Retry succeeds
        ]

        with patch.object(controller, "run_validation_command") as mock_validate:
            mock_validate.side_effect = validation_results

            result = controller.run_with_validation(
                spec=spec,
                validation_cmd="make validate",
                validation_timeout_seconds=60,
                session_output_dir=tmp_path / "session",
                original_prompt="Fix the bug",
                build_retry_spec=lambda p: spec,
            )

        assert result.validation_passed
        assert result.retry_count == 1
        assert mock_runner.run.call_count == 2

    def test_validation_fails_exhausted(
        self,
        mock_runner: MagicMock,
        successful_run_result: RunResult,
        tmp_path: Path,
    ) -> None:
        """Test that max retries are respected."""
        config = RetryConfig(max_validation_retries=2)
        controller = ValidationRetryController(runner=mock_runner, config=config)

        mock_runner.run.return_value = successful_run_result

        spec = RunSpec(
            command=["agent", "run"],
            working_dir=tmp_path,
            timeout_seconds=300,
            output_dir=tmp_path / "output",
        )

        with patch.object(controller, "run_validation_command") as mock_validate:
            # All attempts fail
            mock_validate.return_value = (1, "", "Test failed")

            result = controller.run_with_validation(
                spec=spec,
                validation_cmd="make validate",
                validation_timeout_seconds=60,
                session_output_dir=tmp_path / "session",
                original_prompt="Fix the bug",
                build_retry_spec=lambda p: spec,
            )

        assert not result.validation_passed
        assert result.retry_count == 2  # Hit the max
        # 1 initial + 2 retries = 3 total runs
        assert mock_runner.run.call_count == 3

    def test_error_file_written(
        self,
        controller: ValidationRetryController,
        mock_runner: MagicMock,
        successful_run_result: RunResult,
        tmp_path: Path,
    ) -> None:
        """Test that validation errors are written to file."""
        mock_runner.run.return_value = successful_run_result

        spec = RunSpec(
            command=["agent", "run"],
            working_dir=tmp_path,
            timeout_seconds=300,
            output_dir=tmp_path / "output",
        )
        session_dir = tmp_path / "session"

        with patch.object(controller, "run_validation_command") as mock_validate:
            mock_validate.return_value = (1, "stdout content", "stderr content")

            result = controller.run_with_validation(
                spec=spec,
                validation_cmd="make validate",
                validation_timeout_seconds=60,
                session_output_dir=session_dir,
                original_prompt="Fix the bug",
                build_retry_spec=lambda p: spec,
            )

        assert result.final_error_file is not None
        assert result.final_error_file.exists()
        content = result.final_error_file.read_text()
        assert "stderr content" in content
        assert "stdout content" in content

    def test_retry_prompt_includes_context(
        self,
        controller: ValidationRetryController,
        tmp_path: Path,
    ) -> None:
        """Test that retry prompt includes all context."""
        error_file = tmp_path / "errors.txt"
        prompt = controller.build_retry_prompt(
            original_prompt="Fix authentication bug",
            validation_cmd="make validate",
            validation_error="AssertionError: expected 200, got 401",
            error_file_path=error_file,
        )

        assert "Fix authentication bug" in prompt
        assert "make validate" in prompt
        assert "AssertionError" in prompt
        assert str(error_file) in prompt
        assert "codebase was working before" in prompt


class TestRunValidationCommand:
    """Tests for run_validation_command()."""

    def test_successful_validation(
        self,
        controller: ValidationRetryController,
        tmp_path: Path,
    ) -> None:
        """Test successful validation command."""
        # Create a simple passing command
        exit_code, stdout, stderr = controller.run_validation_command(
            working_dir=tmp_path,
            validation_cmd=f"{sys.executable} -c \"print('pass')\"",
            timeout_seconds=30,
        )

        assert exit_code == 0
        assert "pass" in stdout

    def test_failed_validation(
        self,
        controller: ValidationRetryController,
        tmp_path: Path,
    ) -> None:
        """Test failed validation command."""
        exit_code, stdout, stderr = controller.run_validation_command(
            working_dir=tmp_path,
            validation_cmd=f"{sys.executable} -c \"import sys; print('fail', file=sys.stderr); sys.exit(1)\"",
            timeout_seconds=30,
        )

        assert exit_code == 1
        assert "fail" in stderr

    def test_validation_timeout(
        self,
        controller: ValidationRetryController,
        tmp_path: Path,
    ) -> None:
        """Test validation timeout."""
        from issue_orchestrator.ports.command_runner import CommandResult

        controller._command_runner = MagicMock()  # noqa: SLF001
        controller._command_runner.run.return_value = CommandResult(  # noqa: SLF001
            returncode=-1,
            stdout="",
            stderr="",
            timed_out=True,
        )

        exit_code, stdout, stderr = controller.run_validation_command(
            working_dir=tmp_path,
            validation_cmd=f"{sys.executable} -c \"import time; time.sleep(10)\"",
            timeout_seconds=1,
        )

        assert exit_code == -1
        assert "timed out" in stderr.lower()


class TestWriteValidationErrors:
    """Tests for write_validation_errors()."""

    def test_creates_directory(
        self,
        controller: ValidationRetryController,
        tmp_path: Path,
    ) -> None:
        """Test that directory is created if missing."""
        session_dir = tmp_path / "new" / "nested" / "dir"

        error_file = controller.write_validation_errors(
            session_output_dir=session_dir,
            validation_error="test error",
            validation_output="test output",
        )

        assert error_file.exists()
        assert session_dir.exists()

    def test_includes_both_streams(
        self,
        controller: ValidationRetryController,
        tmp_path: Path,
    ) -> None:
        """Test that both stdout and stderr are included."""
        error_file = controller.write_validation_errors(
            session_output_dir=tmp_path,
            validation_error="stderr content here",
            validation_output="stdout content here",
        )

        content = error_file.read_text()
        assert "stderr content here" in content
        assert "stdout content here" in content
        assert "STDERR" in content
        assert "STDOUT" in content
