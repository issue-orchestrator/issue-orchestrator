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
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .completion_processor import CompletionProcessor, ProcessingResult
    from ..ports.command_runner import CommandRunner
    from ..ports.working_copy import WorkingCopy
    from ..domain.models import CompletionRecord
    from .provider_resilience import ProviderResilienceManager

from ..events import EventName
from ..domain.models import SessionStatus, CompletionOutcome
from ..ports.provider_resilience import ProviderErrorType
from ..infra.provider_resilience import ProviderStatus, read_provider_status
from ..infra.logging_config import issue_log
from ..infra.validation_state import (
    DEFAULT_RETRY_TEMPLATE,
    _truncate_with_tail,
)
from ..observation.observation import SessionObservation, SessionObservationResult
from ..ports import EventSink, TraceEvent
from ..ports.session_output import SessionOutput, ValidationState
from .validation import PublishGate

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

    # Optional blocked label override (e.g., provider unavailable)
    blocked_label: Optional[str] = None


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
        session_output: SessionOutput,
        working_copy: "WorkingCopy",
        command_runner: Optional["CommandRunner"] = None,
        validation_cmd: Optional[str] = None,
        validation_timeout_seconds: int = 300,
        max_validation_retries: int = 0,
        provider_resilience: Optional["ProviderResilienceManager"] = None,
        provider_blocked_label: Optional[str] = None,
    ):
        """Initialize the controller.

        Args:
            completion_processor: For reading/processing completion records
            events: For emitting trace events
            session_output: For session artifact storage
            working_copy: For git operations (required for validation cache)
            command_runner: For running validation commands (optional)
            validation_cmd: Validation command to run after completion (optional)
            validation_timeout_seconds: Timeout for validation command
            max_validation_retries: Maximum number of validation retries (0 = no retries)
        """
        self.completion_processor = completion_processor
        self.events = events
        self.session_output = session_output
        self._working_copy = working_copy
        self._command_runner = command_runner
        self._max_validation_retries = max_validation_retries
        self._validation_cmd = validation_cmd
        self._validation_timeout = validation_timeout_seconds
        self._provider_resilience = provider_resilience
        self._provider_blocked_label = provider_blocked_label

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
        retry_prompt_template: str | None = None,
        repo_root: Path | None = None,
    ) -> SessionDecision:
        """Decide the outcome of a session based on observation + completion.json.

        This is the core decision logic. For ANY non-running session, we check
        completion.json to determine the true outcome.
        """
        # If still running, nothing to decide
        if observation.observation == SessionObservation.RUNNING:
            logger.debug(issue_log(issue_number, "Session still running: session=%s"), session_name)
            return SessionDecision(status=SessionStatus.RUNNING, reason="Session still running")

        provider_status = self._read_provider_status(worktree_path, session_name)
        if provider_status and provider_status.succeeded and self._provider_resilience:
            self._provider_resilience.record_success(provider_status.provider)

        # Log and look up completion record
        self._log_completion_lookup(worktree_path, issue_number, session_name, completion_path)
        record = self.completion_processor.read_completion_record(worktree_path, completion_path)

        if record is None:
            return self._handle_no_completion_record(observation, worktree_path, issue_number, session_name)

        # Process completion record
        recovered = observation.observation == SessionObservation.TIMED_OUT
        if recovered:
            self._log_timeout_recovery(issue_number, session_name, record)

        pr_number = self._extract_pr_number_from_session_name(session_name)
        result = self.completion_processor.process(
            worktree_path,
            issue_number,
            issue_title,
            pr_number=pr_number,
            completion_path=completion_path,
        )
        self._emit_processing_completed_event(issue_number, session_name, result)

        # Map outcome to status
        status = self._map_outcome_to_status(record)

        # Run validation if configured
        validation_passed, validation_error, validation_error_file = None, None, None
        if status == SessionStatus.COMPLETED and self._validation_cmd and self._command_runner:
            status, validation_passed, validation_error, validation_error_file = self._run_validation_gate(
                worktree_path, session_name, issue_number, issue_title, record.outcome, validation_retry_count,
                original_prompt, retry_prompt_template, repo_root,
            )

        # Log completion summary
        self._log_session_completion(issue_number, session_name, status, record, result, recovered)

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

    def _log_completion_lookup(
        self,
        worktree_path: Path,
        issue_number: int,
        session_name: str,
        completion_path: str | None,
    ) -> None:
        """Log completion record lookup details."""
        full_path = (worktree_path / completion_path).resolve() if completion_path else (worktree_path / ".issue-orchestrator/completion.json").resolve()
        logger.info(
            issue_log(issue_number, "Session not running: session=%s checking_completion=%s"),
            session_name, completion_path or ".issue-orchestrator/completion.json",
        )
        self._emit_event(EventName.COMPLETION_LOOKUP, {
            "issue_number": issue_number, "session_name": session_name,
            "worktree_path": str(worktree_path.resolve()), "completion_path": completion_path,
            "full_path": str(full_path), "file_exists": full_path.exists(),
        })
        exists = full_path.exists()
        size = full_path.stat().st_size if exists else None
        logger.info(issue_log(issue_number, "Completion lookup: exists=%s size=%s path=%s"), exists, size, full_path)

    def _handle_no_completion_record(
        self,
        observation: SessionObservationResult,
        worktree_path: Path,
        issue_number: int,
        session_name: str,
    ) -> SessionDecision:
        """Handle case where no completion record exists."""
        session_log = self._get_session_log_tail(worktree_path, session_name)
        provider_status = self._read_provider_status(worktree_path, session_name)

        self._emit_event(EventName.SESSION_NO_COMPLETION_RECORD, {
            "issue_number": issue_number, "session_name": session_name,
            "observation": observation.observation.value,
            "last_output": session_log[-500:] if session_log else "",
        })

        if provider_status and provider_status.error_type == ProviderErrorType.TRANSIENT and not provider_status.succeeded:
            if self._provider_resilience:
                self._provider_resilience.record_transient_failure(
                    provider_status.provider,
                    error_summary=provider_status.last_error_summary,
                    attempts=provider_status.attempts,
                )
            return SessionDecision(
                status=SessionStatus.BLOCKED,
                reason="Provider unavailable",
                blocked_label=self._provider_blocked_label,
            )

        if observation.observation == SessionObservation.TIMED_OUT:
            logger.warning(issue_log(issue_number, "SESSION COMPLETE: status=TIMED_OUT outcome=none reason=no_completion_record session=%s"), session_name)
            if session_log:
                logger.warning(issue_log(issue_number, "LAST OUTPUT:\n%s"), session_log)
            return SessionDecision(status=SessionStatus.TIMED_OUT, reason="Timed out without completion record")

        logger.error(issue_log(issue_number, "SESSION COMPLETE: status=FAILED outcome=none reason=no_completion_record session=%s"), session_name)
        if session_log:
            logger.error(issue_log(issue_number, "LAST OUTPUT:\n%s"), session_log)
        return SessionDecision(status=SessionStatus.FAILED, reason="Terminated without completion record")

    def _read_provider_status(self, worktree_path: Path, session_name: str) -> ProviderStatus | None:
        run_dir = self.session_output.find_run_dir(worktree_path, session_name)
        if not run_dir:
            return None
        return read_provider_status(run_dir)

    def _get_session_log_tail(self, worktree_path: Path, session_name: str) -> str:
        """Get last 50 lines of session log for diagnostics."""
        log_path = self.session_output.get_log_path(worktree_path, session_name)
        if not (log_path and log_path.exists()):
            return ""
        try:
            content = log_path.read_text()
            lines = content.strip().split("\n")
            return "\n".join(lines[-50:])
        except Exception as e:
            logger.debug("Could not read session log: %s", e)
            return ""

    def _log_timeout_recovery(self, issue_number: int, session_name: str, record: "CompletionRecord") -> None:
        """Log and emit event for timeout recovery."""
        logger.info(issue_log(issue_number, "Session timed out but has completion.json - recovering work: outcome=%s"), record.outcome.value)
        self._emit_event(EventName.SESSION_TIMEOUT_RECOVERED, {
            "issue_number": issue_number, "session_name": session_name, "outcome": record.outcome.value,
        })

    def _extract_pr_number_from_session_name(self, session_name: str) -> int | None:
        """Extract PR number from review session name."""
        if not session_name.startswith("review-"):
            return None
        try:
            pr_number = int(session_name.replace("review-", ""))
            logger.debug(f"Review session detected, PR number: {pr_number}")
            return pr_number
        except ValueError:
            logger.warning(f"Could not parse PR number from session name: {session_name}")
            return None

    def _emit_processing_completed_event(self, issue_number: int, session_name: str, result: "ProcessingResult") -> None:
        """Emit session processing completed event."""
        self._emit_event(EventName.SESSION_PROCESSING_COMPLETED, {
            "issue_number": issue_number, "session_name": session_name,
            "success": result.success, "message": result.message,
            "actions_taken": result.actions_taken, "errors": result.errors, "pr_url": result.pr_url,
        })

    def _map_outcome_to_status(self, record: "CompletionRecord") -> SessionStatus:
        """Map completion outcome to session status."""
        from ..domain.models import CompletionOutcome
        outcome_to_status = {
            CompletionOutcome.COMPLETED: SessionStatus.COMPLETED,
            CompletionOutcome.BLOCKED: SessionStatus.BLOCKED,
            CompletionOutcome.NEEDS_HUMAN: SessionStatus.NEEDS_HUMAN,
            CompletionOutcome.REVIEW_APPROVED: SessionStatus.COMPLETED,
            CompletionOutcome.REVIEW_CHANGES_REQUESTED: SessionStatus.COMPLETED,
        }
        return outcome_to_status.get(record.outcome, SessionStatus.FAILED)

    def _run_validation_gate(
        self,
        worktree_path: Path,
        session_name: str,
        issue_number: int,
        issue_title: str,
        outcome: CompletionOutcome,
        validation_retry_count: int,
        original_prompt: str | None,
        retry_prompt_template: str | None,
        repo_root: Path | None,
    ) -> tuple[SessionStatus, Optional[bool], Optional[str], Optional[Path]]:
        """Run validation gate and return updated status."""
        logger.info(issue_log(issue_number, "Running validation gate: cmd=%s timeout=%ds"), self._validation_cmd, self._validation_timeout)
        validation_passed, validation_error, validation_error_file = self._run_validation(worktree_path, session_name, issue_number)
        run_dir = self.session_output.ensure_run_dir(worktree_path, session_name)

        if validation_passed:
            logger.info(issue_log(issue_number, "Validation gate PASSED"))
            self.session_output.clear_retry_state(run_dir)
            self.session_output.update_manifest(
                run_dir,
                {
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                    "outcome": outcome.value,
                    "validation_passed": True,
                    "validation_status": "passed",
                },
            )
            self._emit_event(EventName.SESSION_VALIDATION_PASSED, {"issue_number": issue_number, "session_name": session_name, "validation_cmd": self._validation_cmd})
            return SessionStatus.COMPLETED, validation_passed, validation_error, validation_error_file

        # Validation failed - validation_passed is False in both retry and exhausted cases
        retries_remaining = validation_retry_count < self._max_validation_retries
        if retries_remaining:
            self.session_output.update_manifest(
                run_dir,
                {
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                    "outcome": outcome.value,
                    "validation_passed": False,
                    "validation_status": "retry",
                    "validation_reason": validation_error,
                },
            )
            return self._handle_validation_retry(
                worktree_path, run_dir, session_name, issue_number, issue_title,
                validation_retry_count, validation_error, validation_error_file,
                original_prompt, retry_prompt_template, repo_root,
            ), False, validation_error, validation_error_file

        # Max retries exhausted
        self.session_output.update_manifest(
            run_dir,
            {
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "outcome": outcome.value,
                "validation_passed": False,
                "validation_status": "failed",
                "validation_reason": validation_error,
            },
        )
        return self._handle_validation_exhausted(
            run_dir, session_name, issue_number, validation_retry_count, validation_error, validation_error_file,
        ), False, validation_error, validation_error_file

    def _handle_validation_retry(
        self,
        worktree_path: Path,
        run_dir: Path,
        session_name: str,
        issue_number: int,
        issue_title: str,
        validation_retry_count: int,
        validation_error: Optional[str],
        validation_error_file: Optional[Path],
        original_prompt: str | None,
        retry_prompt_template: str | None,
        repo_root: Path | None,
    ) -> SessionStatus:
        """Handle validation failure with retries remaining."""
        logger.warning(
            issue_log(issue_number, "Validation gate FAILED (retry %d/%d): error=%s error_file=%s"),
            validation_retry_count + 1, self._max_validation_retries,
            validation_error[:200] if validation_error else "none", validation_error_file,
        )
        state = ValidationState(
            retry_count=validation_retry_count + 1, max_retries=self._max_validation_retries,
            validation_cmd=self._validation_cmd,
            last_error=validation_error[:2000] if validation_error else None,
            last_error_file=str(validation_error_file) if validation_error_file else None,
        )
        self.session_output.write_validation_state(run_dir, state)

        task_prompt = original_prompt or issue_title
        retry_prompt_content = self._render_retry_prompt(
            task_prompt=task_prompt, validation_error=validation_error or "Unknown error",
            validation_error_file=validation_error_file, retry_count=validation_retry_count,
            max_retries=self._max_validation_retries, template_path=retry_prompt_template, repo_root=repo_root,
        )
        self.session_output.write_retry_prompt(run_dir, retry_prompt_content)

        self._emit_event(EventName.SESSION_VALIDATION_RETRY_NEEDED, {
            "issue_number": issue_number, "session_name": session_name, "validation_cmd": self._validation_cmd,
            "error_file": str(validation_error_file) if validation_error_file else None,
            "retry_count": validation_retry_count, "max_retries": self._max_validation_retries,
        })
        return SessionStatus.NEEDS_VALIDATION_RETRY

    def _handle_validation_exhausted(
        self,
        run_dir: Path,
        session_name: str,
        issue_number: int,
        validation_retry_count: int,
        validation_error: Optional[str],
        validation_error_file: Optional[Path],
    ) -> SessionStatus:
        """Handle validation failure with max retries exhausted."""
        logger.warning(
            issue_log(issue_number, "Validation gate FAILED (max retries %d exhausted): error=%s error_file=%s"),
            self._max_validation_retries, validation_error[:200] if validation_error else "none", validation_error_file,
        )
        self.session_output.clear_retry_state(run_dir)
        self._emit_event(EventName.SESSION_VALIDATION_FAILED, {
            "issue_number": issue_number, "session_name": session_name, "validation_cmd": self._validation_cmd,
            "error_file": str(validation_error_file) if validation_error_file else None,
            "retry_count": validation_retry_count,
        })
        return SessionStatus.VALIDATION_FAILED

    def _log_session_completion(
        self,
        issue_number: int,
        session_name: str,
        status: SessionStatus,
        record: "CompletionRecord",
        result: "ProcessingResult",
        recovered: bool,
    ) -> None:
        """Log session completion summary."""
        pr_url = result.pr_url or "none"
        if result.success:
            logger.info(
                issue_log(issue_number, "SESSION COMPLETE: status=%s outcome=%s pr=%s recovered=%s session=%s"),
                status.value, record.outcome.value, pr_url, recovered, session_name,
            )
        else:
            logger.error(
                issue_log(issue_number, "SESSION COMPLETE: status=%s outcome=%s pr=%s recovered=%s errors=%s session=%s"),
                status.value, record.outcome.value, pr_url, recovered, result.errors, session_name,
            )

    def _run_validation(
        self,
        worktree_path: Path,
        session_name: str,
        issue_number: int,
    ) -> tuple[bool, Optional[str], Optional[Path]]:
        """Run validation command (with SHA-based caching) and return result.

        Uses PublishGate for caching. If a previous validation passed for
        the same SHA and command, the cached result is used. This prevents
        running validation twice for the same commit (e.g., coding session
        passes validation, then review session on same SHA).

        Args:
            worktree_path: Path to the worktree
            session_name: Session name for output directory
            issue_number: Issue number for logging

        Returns:
            Tuple of (passed, error_message, error_file_path)
        """
        if not self._command_runner or not self._validation_cmd:
            return True, None, None

        # Get session output directory for validation artifacts
        run_dir = self.session_output.ensure_run_dir(worktree_path, session_name)

        # Use PublishGate for SHA-based caching
        gate = PublishGate(
            worktree=worktree_path,
            command_runner=self._command_runner,
            working_copy=self._working_copy,
            command=self._validation_cmd,
            timeout_seconds=self._validation_timeout,
        )

        # Get HEAD SHA for logging
        head_sha = self._working_copy.get_head_sha(worktree_path)
        sha_display = head_sha[:8] if head_sha else "unknown"

        logger.info(
            issue_log(issue_number, "Running validation: cmd=%s worktree=%s sha=%s"),
            self._validation_cmd,
            worktree_path,
            sha_display,
        )

        result = gate.check(session_output_dir=run_dir)

        if result.allowed:
            if result.cache_hit:
                logger.info(
                    issue_log(issue_number, "Validation cache hit: SHA=%s (skipped re-run)"),
                    sha_display,
                )
            else:
                logger.info(
                    issue_log(issue_number, "Validation passed: SHA=%s"),
                    sha_display,
                )
            return True, None, None

        # Validation failed - get error file path from record
        error_msg = result.reason
        error_file = self._resolve_error_file_path(worktree_path, result.record)

        return False, error_msg, error_file

    def _resolve_error_file_path(
        self,
        worktree_path: Path,
        record: Optional[Any],
    ) -> Optional[Path]:
        """Resolve the error file path from a validation record.

        Args:
            worktree_path: Path to the worktree
            record: ValidationRecord (or None)

        Returns:
            Absolute path to the error file, or None
        """
        if not record or not record.stderr_path:
            return None

        stderr_path = record.stderr_path
        if Path(stderr_path).is_absolute():
            return Path(stderr_path)
        return worktree_path / stderr_path

    def _render_retry_prompt(
        self,
        task_prompt: str,
        validation_error: str,
        validation_error_file: Optional[Path],
        retry_count: int,
        max_retries: int,
        template_path: Optional[str] = None,
        repo_root: Optional[Path] = None,
    ) -> str:
        """Render the retry prompt content.

        Args:
            task_prompt: The original task prompt
            validation_error: Error output (will be truncated, preserving tail)
            validation_error_file: Path to the full error file
            retry_count: Current retry attempt (0-based, displayed as 1-based)
            max_retries: Maximum allowed retries
            template_path: Optional path to custom template (relative to repo_root)
            repo_root: Repo root for resolving template_path

        Returns:
            Rendered retry prompt content.
        """
        # Load template - custom or default
        template = DEFAULT_RETRY_TEMPLATE
        if template_path and repo_root:
            template_full_path = repo_root / template_path
            if template_full_path.exists():
                try:
                    template = template_full_path.read_text()
                    logger.debug("Loaded retry template from %s", template_full_path)
                except OSError as e:
                    logger.warning("Failed to load retry template from %s: %s", template_full_path, e)
            else:
                logger.warning("Retry template not found at %s, using default", template_full_path)

        # Render template with variables
        # Note: retry_count is 0-based internally, display as 1-based
        return template.format(
            original_task=task_prompt,
            validation_cmd=self._validation_cmd or "",
            error_file=str(validation_error_file) if validation_error_file else "unknown",
            error_summary=_truncate_with_tail(validation_error),
            retry_count=retry_count + 1,
            max_retries=max_retries + 1,
        )

    def _emit_event(self, event_type: EventName, data: dict[str, Any]) -> None:
        """Emit a trace event."""
        self.events.publish(TraceEvent(event_type, data))
