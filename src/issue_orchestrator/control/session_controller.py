"""Session lifecycle controller.

This controller makes decisions about session outcomes based on:
1. Observations (session running, terminated, timed out)
2. Completion records (completion.json written by agent-done)

Key principle: completion.json is the source of truth for agent intent.
The observer reports facts; this controller decides outcomes.

Example flows:
- Session terminated + completion.json exists -> process completion record
- Session terminated + no completion.json -> FAILED
- Session timed out + completion.json exists -> recover work, process completion
- Session timed out + no completion.json -> TIMED_OUT
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .completion_processor import CompletionProcessor, ProcessingResult

from ..events import EventName
from ..models import SessionStatus
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


class SessionController:
    """Controller that decides session outcomes.

    Uses observations from SessionObserver and completion records from
    CompletionProcessor to determine the true outcome of a session.

    The key insight: a session may time out but still have completed work.
    If completion.json exists with outcome=completed, we should process it
    regardless of whether the session timed out or exited cleanly.
    """

    def __init__(
        self,
        completion_processor: "CompletionProcessor",
        events: EventSink,
    ):
        """Initialize the controller.

        Args:
            completion_processor: For reading/processing completion records
            events: For emitting trace events
        """
        self.completion_processor = completion_processor
        self.events = events

    def decide_outcome(
        self,
        observation: SessionObservationResult,
        worktree_path: Path,
        issue_number: int,
        issue_title: str,
        session_name: str,
        completion_path: str | None = None,
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

        Returns:
            SessionDecision with the determined status and any processing results
        """
        # If still running, nothing to decide
        if observation.observation == SessionObservation.RUNNING:
            return SessionDecision(
                status=SessionStatus.RUNNING,
                reason="Session still running",
            )

        # Session is not running - check completion.json
        # This is the source of truth for agent intent
        # Each agent writes to its own file (based on completion_path)
        full_path = (worktree_path / completion_path).resolve() if completion_path else (worktree_path / ".issue-orchestrator/completion.json").resolve()
        self._emit_event(EventName.COMPLETION_LOOKUP, {
            "issue_number": issue_number,
            "session_name": session_name,
            "worktree_path": str(worktree_path.resolve()),
            "completion_path": completion_path,
            "full_path": str(full_path),
            "file_exists": full_path.exists(),
        })
        record = self.completion_processor.read_completion_record(worktree_path, completion_path)

        if record is None:
            # No completion record - agent died without calling agent-done
            # Try to capture session log for diagnostics
            session_log = ""
            log_path = worktree_path / ".issue-orchestrator" / "session.log"
            if log_path.exists():
                try:
                    # Get last 50 lines of session log
                    content = log_path.read_text()
                    lines = content.strip().split("\n")
                    session_log = "\n".join(lines[-50:])
                    logger.warning(
                        "Session %s failed. Last output:\n%s",
                        session_name, session_log
                    )
                except Exception as e:
                    logger.debug("Could not read session log: %s", e)

            self._emit_event(EventName.SESSION_NO_COMPLETION_RECORD, {
                "issue_number": issue_number,
                "session_name": session_name,
                "observation": observation.observation.value,
                "last_output": session_log[-500:] if session_log else "",
            })

            if observation.observation == SessionObservation.TIMED_OUT:
                return SessionDecision(
                    status=SessionStatus.TIMED_OUT,
                    reason="Timed out without completion record",
                )
            else:
                return SessionDecision(
                    status=SessionStatus.FAILED,
                    reason="Terminated without completion record",
                )

        # Completion record exists - process it
        # This is true regardless of whether session timed out or exited
        recovered = observation.observation == SessionObservation.TIMED_OUT

        if recovered:
            logger.info(
                "Session %s timed out but has completion.json - recovering work",
                session_name,
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
        from ..models import CompletionOutcome
        outcome_to_status = {
            CompletionOutcome.COMPLETED: SessionStatus.COMPLETED,
            CompletionOutcome.BLOCKED: SessionStatus.BLOCKED,
            CompletionOutcome.NEEDS_HUMAN: SessionStatus.NEEDS_HUMAN,
            CompletionOutcome.REVIEW_APPROVED: SessionStatus.COMPLETED,
            CompletionOutcome.REVIEW_CHANGES_REQUESTED: SessionStatus.COMPLETED,
        }
        status = outcome_to_status.get(record.outcome, SessionStatus.FAILED)

        return SessionDecision(
            status=status,
            processing_result=result,
            completion_processed=True,
            recovered_from_timeout=recovered,
            reason=f"Processed completion record with outcome: {record.outcome.value}",
        )

    def _emit_event(self, event_type: EventName, data: dict) -> None:
        """Emit a trace event."""
        self.events.publish(TraceEvent(event_type, data))
