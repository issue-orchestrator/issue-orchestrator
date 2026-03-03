"""Validation retry controller.

This module implements the retry loop for validation failures. When an agent
completes but validation fails, the orchestrator retries with error context
injected into the prompt.

Key design decisions:
- agent-runner invokes the agent ONCE only
- Orchestrator owns all retry policy
- Validation errors are written to session output directory
- Retry prompt includes full context (original task, validation cmd, errors)
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

# Phase 2 migration: validation_retry still uses vendored AgentRunner/RunSpec.
# Import directly from _vendor until migrated to unified AgentRunner.
from issue_orchestrator._vendor.agent_runner import AgentRunner, RunResult, RunSpec

from ..infra.config import RetryConfig

if TYPE_CHECKING:
    from ..ports.command_runner import CommandRunner

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of running an agent with validation.

    Attributes:
        run_result: The final RunResult from agent-runner
        validation_passed: True if validation passed (or was not configured)
        validation_output: Stdout from validation command (if run)
        validation_error: Stderr from validation command (if failed)
        retry_count: Number of retries that were attempted
        final_error_file: Path to the validation errors file (if written)
    """

    run_result: RunResult
    validation_passed: bool
    validation_output: str
    validation_error: str
    retry_count: int
    final_error_file: Optional[Path]


RETRY_PROMPT_TEMPLATE = """The codebase was working before you started. After your changes, validation failed.

Original task:
{original_prompt}

Validation command: {validation_cmd}

Validation errors (also available at {error_file_path}):
```
{validation_error}
```

Fix these errors. The failures may be directly in code you changed, or transitively
related (e.g., you broke an import, renamed something other code depends on, or
violated a project guardrail).

When done, run: coding-done completed --implementation "..." --problems "..."
"""


class ValidationRetryController:
    """Controls the validation retry loop.

    This controller is responsible for:
    1. Running the agent via AgentRunner
    2. Running validation after completion
    3. Retrying with error context if validation fails
    4. Respecting max retry limits

    It does NOT:
    - Parse completion.json (orchestrator does that)
    - Manage GitHub labels or state (orchestrator does that)
    - Make decisions about blocked/needs_human (orchestrator does that)
    """

    def __init__(
        self,
        runner: AgentRunner,
        config: RetryConfig,
        command_runner: "CommandRunner | None" = None,
    ) -> None:
        """Initialize the controller.

        Args:
            runner: AgentRunner instance for executing agents
            config: Retry configuration
            command_runner: CommandRunner for executing validation commands
        """
        self._runner = runner
        self._config = config
        self._command_runner = command_runner

    def run_validation_command(
        self,
        working_dir: Path,
        validation_cmd: str,
        timeout_seconds: int,
    ) -> tuple[int, str, str]:
        """Run the validation command.

        Args:
            working_dir: Directory to run validation in
            validation_cmd: Command to run (e.g., "make validate")
            timeout_seconds: Timeout for validation

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        logger.info("Running validation: %s in %s", validation_cmd, working_dir)

        if self._command_runner is None:
            logger.error("No command runner configured - cannot run validation")
            return -1, "", "No command runner configured"

        result = self._command_runner.run(
            validation_cmd,
            cwd=working_dir,
            timeout_seconds=timeout_seconds,
            shell=True,
        )

        if result.timed_out:
            logger.warning("Validation timed out after %ds", timeout_seconds)
            return -1, result.stdout, f"Validation timed out after {timeout_seconds} seconds"

        return result.returncode, result.stdout, result.stderr

    def write_validation_errors(
        self,
        session_output_dir: Path,
        validation_error: str,
        validation_output: str,
    ) -> Path:
        """Write validation errors to the session output directory.

        Args:
            session_output_dir: Session output directory
            validation_error: Stderr from validation
            validation_output: Stdout from validation

        Returns:
            Path to the error file
        """
        session_output_dir.mkdir(parents=True, exist_ok=True)

        error_file = session_output_dir / self._config.validation_error_file
        content = f"=== Validation Failed ===\n\n"
        if validation_error:
            content += f"=== STDERR ===\n{validation_error}\n\n"
        if validation_output:
            content += f"=== STDOUT ===\n{validation_output}\n"

        error_file.write_text(content)
        logger.info("Wrote validation errors to %s", error_file)
        return error_file

    def build_retry_prompt(
        self,
        original_prompt: str,
        validation_cmd: str,
        validation_error: str,
        error_file_path: Path,
    ) -> str:
        """Build the retry prompt with error context.

        Args:
            original_prompt: The original task prompt
            validation_cmd: The validation command that failed
            validation_error: The error output from validation
            error_file_path: Path to the error file

        Returns:
            Formatted retry prompt
        """
        return RETRY_PROMPT_TEMPLATE.format(
            original_prompt=original_prompt,
            validation_cmd=validation_cmd,
            validation_error=validation_error,
            error_file_path=error_file_path,
        )

    def run_with_validation(
        self,
        spec: RunSpec,
        validation_cmd: Optional[str],
        validation_timeout_seconds: int,
        session_output_dir: Path,
        original_prompt: str,
        build_retry_spec: Callable[[str], RunSpec],
    ) -> ValidationResult:
        """Run an agent with validation and retry on failure.

        This is the main entry point for the retry loop.

        Args:
            spec: Initial RunSpec for the agent
            validation_cmd: Validation command (None to skip validation)
            validation_timeout_seconds: Timeout for validation command
            session_output_dir: Directory for session output
            original_prompt: Original task prompt (for retry context)
            build_retry_spec: Callable that builds a new RunSpec given a retry prompt

        Returns:
            ValidationResult with final status
        """
        retry_count = 0
        current_spec = spec
        validation_output = ""
        validation_error = ""
        error_file: Optional[Path] = None

        while True:
            # Run the agent
            logger.info(
                "Running agent (attempt %d/%d)",
                retry_count + 1,
                self._config.max_validation_retries + 1,
            )
            run_result = self._runner.run(current_spec)

            # If agent failed or timed out, don't run validation
            if run_result.timed_out or (run_result.exit_code is not None and run_result.exit_code != 0):
                logger.warning(
                    "Agent failed (exit_code=%s, timed_out=%s), skipping validation",
                    run_result.exit_code,
                    run_result.timed_out,
                )
                return ValidationResult(
                    run_result=run_result,
                    validation_passed=False,
                    validation_output="",
                    validation_error="Agent failed before validation",
                    retry_count=retry_count,
                    final_error_file=None,
                )

            # Skip validation if not configured
            if not validation_cmd:
                logger.info("Validation not configured, skipping")
                return ValidationResult(
                    run_result=run_result,
                    validation_passed=True,
                    validation_output="",
                    validation_error="",
                    retry_count=retry_count,
                    final_error_file=None,
                )

            # Run validation
            exit_code, validation_output, validation_error = self.run_validation_command(
                working_dir=current_spec.working_dir,
                validation_cmd=validation_cmd,
                timeout_seconds=validation_timeout_seconds,
            )

            if exit_code == 0:
                logger.info("Validation passed")
                return ValidationResult(
                    run_result=run_result,
                    validation_passed=True,
                    validation_output=validation_output,
                    validation_error=validation_error,
                    retry_count=retry_count,
                    final_error_file=error_file,
                )

            # Validation failed - check if we can retry
            if retry_count >= self._config.max_validation_retries:
                logger.warning(
                    "Validation failed and max retries (%d) exhausted",
                    self._config.max_validation_retries,
                )
                error_file = self.write_validation_errors(
                    session_output_dir,
                    validation_error,
                    validation_output,
                )
                return ValidationResult(
                    run_result=run_result,
                    validation_passed=False,
                    validation_output=validation_output,
                    validation_error=validation_error,
                    retry_count=retry_count,
                    final_error_file=error_file,
                )

            # Write errors and build retry prompt
            logger.info(
                "Validation failed (attempt %d/%d), retrying",
                retry_count + 1,
                self._config.max_validation_retries + 1,
            )
            error_file = self.write_validation_errors(
                session_output_dir,
                validation_error,
                validation_output,
            )
            retry_prompt = self.build_retry_prompt(
                original_prompt=original_prompt,
                validation_cmd=validation_cmd,
                validation_error=validation_error,
                error_file_path=error_file,
            )

            # Build new spec for retry
            current_spec = build_retry_spec(retry_prompt)
            retry_count += 1
