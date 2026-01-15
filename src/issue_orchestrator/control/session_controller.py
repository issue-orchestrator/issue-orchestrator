"""Session lifecycle controller.

This controller makes decisions about session outcomes based on:
1. Observations (session running, terminated, timed out)
2. Completion records (completion.json written by agent-done)
3. Validation gate (optional post-completion validation)

Key principle: completion.json is the source of truth for agent intent.
The observer reports facts; this controller decides outcomes.

Example flows:
- Session terminated + completion.json exists -> process completion record
- Session terminated + no completion.json -> FAILED
- Session timed out + completion.json exists -> recover work, process completion
- Session timed out + no completion.json -> TIMED_OUT
- Completion processed + validation configured -> run validation gate
- Validation failed -> VALIDATION_FAILED
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .completion_processor import CompletionProcessor, ProcessingResult
    from ..ports.command_runner import CommandRunner

from ..events import EventName
from ..domain.models import SessionStatus
from ..infra.logging_config import issue_log
from ..infra.session_output import find_session_log_path, ensure_session_output_dir
from ..infra.validation_state import (
    ValidationState,
    write_validation_state,
    write_retry_prompt,
    clear_validation_state,
)
from ..observation.observation import SessionObservation, SessionObservationResult
from ..ports import EventSink, TraceEvent

logger = logging.getLogger(__name__)


@dataclass
class SessionDecision:
    """Decision about a session's outcome.

    This is the result of processing an observation + completion record.
    Contains the final status and any results from completion processing.
    """

    # The decided status
    status: SessionStatus

    # Processing result if completion.json was processed
    processing_result: Optional["ProcessingResult"] = None

    # Whether completion.json was found and processed
    completion_processed: bool = False

    # Whether this was a recovered timeout (timeout but completion.json existed)
    recovered_from_timeout: bool = False

    # Reason for the decision
    reason: str = ""

    # Validation gate results (if validation was run)
    validation_passed: Optional[bool] = None
    validation_error: Optional[str] = None
    validation_error_file: Optional[Path] = None


class SessionController:
    """Controller that decides session outcomes.

    Uses observations from SessionObserver and completion records from
    CompletionProcessor to determine the true outcome of a session.

    The key insight: a session may time out but still have completed work.
    If completion.json exists with outcome=completed, we should process it
    regardless of whether the session timed out or exited cleanly.

    Optionally runs a validation gate after completion processing.
    """

    def __init__(
        self,
        completion_processor: "CompletionProcessor",
        events: EventSink,
        command_runner: Optional["CommandRunner"] = None,
        validation_cmd: Optional[str] = None,
        validation_timeout_seconds: int = 300,
        max_validation_retries: int = 0,
    ):
        """Initialize the controller.

        Args:
            completion_processor: For reading/processing completion records
            events: For emitting trace events
            command_runner: For running validation commands (optional)
            validation_cmd: Validation command to run after completion (optional)
            validation_timeout_seconds: Timeout for validation command
            max_validation_retries: Maximum number of validation retries (0 = no retries)
        """
        self.completion_processor = completion_processor
        self.events = events
        self._command_runner = command_runner
        self._max_validation_retries = max_validation_retries
        self._validation_cmd = validation_cmd
        self._validation_timeout = validation_timeout_seconds

    def decide_outcome(
        self,
        observation: SessionObservationResult,
        worktree_path: Path,
        issue_number: int,
        issue_title: str,
        session_name: str,
        completion_path: str | None = None,
        validation_retry_count: int = 0,
        original_prompt: str | None = None,
    ) -> SessionDecision:
        """Decide the outcome of a session based on observation + completion.json.

        This is the core decision logic. For ANY non-running session, we check
        completion.json to determine the true outcome. This handles cases where:
        - Agent completed but didn't exit (timeout with completion.json)
        - Agent crashed after writing completion.json
        - Agent exited cleanly with completion.json

        Args:
            observation: What we observed about the session
            worktree_path: Path to the worktree (for reading completion.json)
            issue_number: The issue number for logging/events
            issue_title: The issue title for PR creation
            session_name: Session name for logging
            completion_path: Optional path to completion.json (default: .issue-orchestrator/completion.json)
            validation_retry_count: Current validation retry count (for determining if more retries allowed)

        Returns:
            SessionDecision with the determined status and any processing results
        """
        # If still running, nothing to decide
        if observation.observation == SessionObservation.RUNNING:
            logger.debug(
                issue_log(issue_number, "Session still running: session=%s"),
                session_name,
            )
            return SessionDecision(
                status=SessionStatus.RUNNING,
                reason="Session still running",
            )

        # Session is not running - check completion.json
        # This is the source of truth for agent intent
        # Each agent writes to its own file (based on completion_path)
        full_path = (worktree_path / completion_path).resolve() if completion_path else (worktree_path / ".issue-orchestrator/completion.json").resolve()
        logger.info(
            issue_log(issue_number, "Session not running: session=%s observation=%s checking_completion=%s"),
            session_name,
            observation.observation.value,
            completion_path or ".issue-orchestrator/completion.json",
        )
        self._emit_event(EventName.COMPLETION_LOOKUP, {
            "issue_number": issue_number,
            "session_name": session_name,
            "worktree_path": str(worktree_path.resolve()),
            "completion_path": completion_path,
            "full_path": str(full_path),
            "file_exists": full_path.exists(),
        })
        exists = full_path.exists()
        size = None
        if exists:
            try:
                size = full_path.stat().st_size
            except OSError:
                size = None
        logger.info(
            issue_log(issue_number, "Completion lookup: exists=%s size=%s path=%s"),
            exists,
            size,
            full_path,
        )
        record = self.completion_processor.read_completion_record(worktree_path, completion_path)

        if record is None:
            # No completion record - agent died without calling agent-done
            # Try to capture session log for diagnostics
            session_log = ""
            log_path = find_session_log_path(worktree_path, session_name)
            if log_path and log_path.exists():
                try:
                    # Get last 50 lines of session log
                    content = log_path.read_text()
                    lines = content.strip().split("\n")
                    session_log = "\n".join(lines[-50:])
                except Exception as e:
                    logger.debug("Could not read session log: %s", e)

            self._emit_event(EventName.SESSION_NO_COMPLETION_RECORD, {
                "issue_number": issue_number,
                "session_name": session_name,
                "observation": observation.observation.value,
                "last_output": session_log[-500:] if session_log else "",
            })

            if observation.observation == SessionObservation.TIMED_OUT:
                logger.warning(
                    issue_log(issue_number, "SESSION COMPLETE: status=TIMED_OUT outcome=none reason=no_completion_record session=%s"),
                    session_name,
                )
                if session_log:
                    logger.warning(
                        issue_log(issue_number, "LAST OUTPUT:\n%s"),
                        session_log,
                    )
                return SessionDecision(
                    status=SessionStatus.TIMED_OUT,
                    reason="Timed out without completion record",
                )
            else:
                logger.error(
                    issue_log(issue_number, "SESSION COMPLETE: status=FAILED outcome=none reason=no_completion_record session=%s"),
                    session_name,
                )
                if session_log:
                    logger.error(
                        issue_log(issue_number, "LAST OUTPUT:\n%s"),
                        session_log,
                    )
                return SessionDecision(
                    status=SessionStatus.FAILED,
                    reason="Terminated without completion record",
                )

        # Completion record exists - process it
        # This is true regardless of whether session timed out or exited
        recovered = observation.observation == SessionObservation.TIMED_OUT

        if recovered:
            logger.info(
                issue_log(issue_number, "Session timed out but has completion.json - recovering work: outcome=%s"),
                record.outcome.value,
            )
            self._emit_event(EventName.SESSION_TIMEOUT_RECOVERED, {
                "issue_number": issue_number,
                "session_name": session_name,
                "outcome": record.outcome.value,
            })

        # For review sessions, extract PR number from session name (review-{pr_number})
        # Label operations need to target the PR, not the original issue
        pr_number = None
        if session_name.startswith("review-"):
            try:
                pr_number = int(session_name.replace("review-", ""))
                logger.debug(f"Review session detected, PR number: {pr_number}")
            except ValueError:
                logger.warning(f"Could not parse PR number from session name: {session_name}")

        # Process the completion record (push, create PR, etc.)
        result = self.completion_processor.process(
            worktree_path,
            issue_number,
            issue_title,
            pr_number=pr_number,
            completion_path=completion_path,
        )

        # Emit event for observability (tests can subscribe to see what happened)
        self._emit_event(EventName.SESSION_PROCESSING_COMPLETED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "success": result.success,
            "message": result.message,
            "actions_taken": result.actions_taken,
            "errors": result.errors,
            "pr_url": result.pr_url,
        })

        # Map completion outcome to session status
        from ..domain.models import CompletionOutcome
        outcome_to_status = {
            CompletionOutcome.COMPLETED: SessionStatus.COMPLETED,
            CompletionOutcome.BLOCKED: SessionStatus.BLOCKED,
            CompletionOutcome.NEEDS_HUMAN: SessionStatus.NEEDS_HUMAN,
            CompletionOutcome.REVIEW_APPROVED: SessionStatus.COMPLETED,
            CompletionOutcome.REVIEW_CHANGES_REQUESTED: SessionStatus.COMPLETED,
        }
        status = outcome_to_status.get(record.outcome, SessionStatus.FAILED)

        # Run validation gate if configured and outcome is COMPLETED
        validation_passed: Optional[bool] = None
        validation_error: Optional[str] = None
        validation_error_file: Optional[Path] = None

        if (
            status == SessionStatus.COMPLETED
            and self._validation_cmd
            and self._command_runner
        ):
            logger.info(
                issue_log(issue_number, "Running validation gate: cmd=%s timeout=%ds"),
                self._validation_cmd,
                self._validation_timeout,
            )
            validation_passed, validation_error, validation_error_file = self._run_validation(
                worktree_path, session_name, issue_number
            )
            if not validation_passed:
                # Check if retries are remaining
                retries_remaining = validation_retry_count < self._max_validation_retries
                if retries_remaining:
                    status = SessionStatus.NEEDS_VALIDATION_RETRY
                    logger.warning(
                        issue_log(issue_number, "Validation gate FAILED (retry %d/%d): error=%s error_file=%s"),
                        validation_retry_count + 1,
                        self._max_validation_retries,
                        validation_error[:200] if validation_error else "none",
                        validation_error_file,
                    )
                    # Write validation state for crash recovery
                    state = ValidationState(
                        retry_count=validation_retry_count + 1,
                        max_retries=self._max_validation_retries,
                        validation_cmd=self._validation_cmd,
                        last_error=validation_error[:2000] if validation_error else None,
                        last_error_file=str(validation_error_file) if validation_error_file else None,
                    )
                    write_validation_state(worktree_path, state)
                    # Write retry prompt for the agent
                    task_prompt = original_prompt or issue_title
                    write_retry_prompt(
                        worktree_path,
                        original_prompt=task_prompt,
                        validation_cmd=self._validation_cmd,
                        validation_error=validation_error or "Unknown error",
                        retry_count=validation_retry_count,
                        max_retries=self._max_validation_retries,
                    )
                    self._emit_event(EventName.SESSION_VALIDATION_RETRY_NEEDED, {
                        "issue_number": issue_number,
                        "session_name": session_name,
                        "validation_cmd": self._validation_cmd,
                        "error_file": str(validation_error_file) if validation_error_file else None,
                        "retry_count": validation_retry_count,
                        "max_retries": self._max_validation_retries,
                    })
                else:
                    status = SessionStatus.VALIDATION_FAILED
                    logger.warning(
                        issue_log(issue_number, "Validation gate FAILED (max retries %d exhausted): error=%s error_file=%s"),
                        self._max_validation_retries,
                        validation_error[:200] if validation_error else "none",
                        validation_error_file,
                    )
                    # Clear validation state since we're done retrying
                    clear_validation_state(worktree_path)
                    self._emit_event(EventName.SESSION_VALIDATION_FAILED, {
                        "issue_number": issue_number,
                        "session_name": session_name,
                        "validation_cmd": self._validation_cmd,
                        "error_file": str(validation_error_file) if validation_error_file else None,
                        "retry_count": validation_retry_count,
                    })
            else:
                logger.info(
                    issue_log(issue_number, "Validation gate PASSED"),
                )
                # Clear validation state on success
                clear_validation_state(worktree_path)
                self._emit_event(EventName.SESSION_VALIDATION_PASSED, {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "validation_cmd": self._validation_cmd,
                })

        # Log the session completion summary
        pr_url = result.pr_url or "none"
        if result.success:
            logger.info(
                issue_log(issue_number, "SESSION COMPLETE: status=%s outcome=%s pr=%s recovered=%s session=%s"),
                status.value,
                record.outcome.value,
                pr_url,
                recovered,
                session_name,
            )
        else:
            logger.error(
                issue_log(issue_number, "SESSION COMPLETE: status=%s outcome=%s pr=%s recovered=%s errors=%s session=%s"),
                status.value,
                record.outcome.value,
                pr_url,
                recovered,
                result.errors,
                session_name,
            )

        return SessionDecision(
            status=status,
            processing_result=result,
            completion_processed=True,
            recovered_from_timeout=recovered,
            reason=f"Processed completion record with outcome: {record.outcome.value}",
            validation_passed=validation_passed,
            validation_error=validation_error,
            validation_error_file=validation_error_file,
        )

    def _run_validation(
        self,
        worktree_path: Path,
        session_name: str,
        issue_number: int,
    ) -> tuple[bool, Optional[str], Optional[Path]]:
        """Run validation command and return result.

        Args:
            worktree_path: Path to the worktree
            session_name: Session name for output directory
            issue_number: Issue number for logging

        Returns:
            Tuple of (passed, error_message, error_file_path)
        """
        if not self._command_runner or not self._validation_cmd:
            return True, None, None

        logger.info(
            issue_log(issue_number, "Running validation: %s in %s"),
            self._validation_cmd,
            worktree_path,
        )

        result = self._command_runner.run(
            self._validation_cmd,
            cwd=worktree_path,
            timeout_seconds=self._validation_timeout,
            shell=True,
        )

        if result.timed_out:
            error_msg = f"Validation timed out after {self._validation_timeout} seconds"
            error_file = self._write_validation_errors(
                worktree_path, session_name, error_msg, result.stdout
            )
            return False, error_msg, error_file

        if result.returncode != 0:
            error_msg = result.stderr or f"Validation failed with exit code {result.returncode}"
            error_file = self._write_validation_errors(
                worktree_path, session_name, error_msg, result.stdout
            )
            return False, error_msg, error_file

        return True, None, None

    def _write_validation_errors(
        self,
        worktree_path: Path,
        session_name: str,
        error: str,
        output: str,
    ) -> Path:
        """Write validation errors to session output directory.

        Args:
            worktree_path: Path to the worktree
            session_name: Session name for output directory
            error: Error message (stderr)
            output: Command output (stdout)

        Returns:
            Path to the error file
        """
        output_dir = ensure_session_output_dir(worktree_path, session_name)
        error_file = output_dir / "validation-errors.txt"

        content = f"""=== VALIDATION ERRORS ===
Command: {self._validation_cmd}
Timeout: {self._validation_timeout}s

=== STDERR ===
{error}

=== STDOUT ===
{output}
"""
        error_file.write_text(content)
        logger.info(
            "Wrote validation errors to %s",
            error_file,
        )
        return error_file

    def _emit_event(self, event_type: EventName, data: dict[str, Any]) -> None:
        """Emit a trace event."""
        self.events.publish(TraceEvent(event_type, data))
