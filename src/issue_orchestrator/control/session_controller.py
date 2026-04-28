"""Session lifecycle controller.

This controller makes decisions about session outcomes based on:
1. Observations (session running, terminated, timed out)
2. Completion records (completion.json written by coding-done/reviewer-done)
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

import json
import logging
import os
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
from ..domain.models import SessionStatus, CompletionOutcome, RequestedAction
from ..ports.provider_resilience import ProviderErrorType
from ..infra.provider_resilience import ProviderStatus, read_provider_status
from ..infra.logging_config import issue_log
from ..infra.validation_state import (
    DEFAULT_RETRY_TEMPLATE,
    _truncate_with_tail,
)
from ..observation.observation import SessionObservation, SessionObservationResult
from ..ports import EventSink, make_trace_event
from ..ports.session_output import SessionOutput, ValidationState
from .validation import PublishGate

logger = logging.getLogger(__name__)
_AGENT_DONE_MARKER = ".agent-done-marker"


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
    blocked_reason: Optional[str] = None

    # Curated detail from CompletionRecord for trace event enrichment.
    # Keys: implementation, problems, attempted, blocked_reason, blocked_by,
    #        question, review_summary, review_issues, risk_level
    completion_detail: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class ValidationFailureContext:
    worktree_path: Path
    run_dir: Path
    session_name: str
    issue_number: int
    issue_title: str
    retry_count: int
    error: str | None
    error_file: Path | None
    original_prompt: str | None
    retry_prompt_template: str | None
    repo_root: Path | None


@dataclass(frozen=True)
class ValidationGateDecision:
    status: SessionStatus
    passed: bool
    error: str | None
    error_file: Path | None


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
            logger.debug(
                issue_log(issue_number, "Session still running: session=%s"),
                session_name,
            )
            return SessionDecision(
                status=SessionStatus.RUNNING, reason="Session still running"
            )

        completion_session_name = self.session_output.session_name_from_path(
            completion_path
        )
        validation_session_name = completion_session_name or session_name
        run_dir = self._resolve_run_dir(
            worktree_path, session_name, completion_session_name
        )
        provider_status = self._read_provider_status(run_dir)
        if provider_status and provider_status.succeeded and self._provider_resilience:
            self._provider_resilience.record_success(provider_status.provider)

        # Log and look up completion record
        self._log_completion_lookup(
            worktree_path, issue_number, session_name, completion_path
        )
        record = self.completion_processor.read_completion_record(
            worktree_path, completion_path
        )

        if record is None:
            return self._handle_no_completion_record(
                observation,
                worktree_path,
                issue_number,
                session_name,
                run_dir,
                completion_path,
            )

        # Recover completed work from timed-out sessions when possible.
        recovered = observation.observation == SessionObservation.TIMED_OUT
        if recovered:
            self._log_timeout_recovery(issue_number, session_name, record)

        # Phase: dirty preflight. Short-circuit before validation/publish work.
        dirty_preflight_decision = self._run_dirty_preflight_before_validation(
            record=record,
            worktree_path=worktree_path,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=validation_session_name,
            run_dir=run_dir,
            validation_retry_count=validation_retry_count,
            original_prompt=original_prompt,
            retry_prompt_template=retry_prompt_template,
            repo_root=repo_root,
            recovered=recovered,
        )
        if dirty_preflight_decision is not None:
            return dirty_preflight_decision

        # Process completion record
        pr_number = self._extract_pr_number_from_session_name(session_name)
        result = self.completion_processor.process(
            worktree_path,
            issue_number,
            issue_title,
            pr_number=pr_number,
            completion_path=completion_path,
        )
        deferred_decision = self._deferred_review_exchange_decision(
            result=result,
            run_dir=run_dir,
            session_name=validation_session_name,
            issue_number=issue_number,
            recovered=recovered,
        )
        if deferred_decision is not None:
            return deferred_decision
        self._emit_processing_completed_event(
            worktree_path, issue_number, session_name, run_dir, result
        )

        # Map outcome to status
        status = self._map_outcome_to_status(record)
        if result.failure_kind == "validation_failed":
            status = self._handle_pre_publish_validation_failure(
                run_dir=run_dir,
                session_name=validation_session_name,
                issue_number=issue_number,
                validation_reason=result.message,
            )
        blocked_reason = (
            record.blocked_reason if status == SessionStatus.BLOCKED else None
        )

        # Run validation if configured
        validation_passed, validation_error, validation_error_file = None, None, None
        validation_decision = self._run_validation_phase_if_needed(
            status=status,
            worktree_path=worktree_path,
            session_name=validation_session_name,
            run_dir=run_dir,
            issue_number=issue_number,
            issue_title=issue_title,
            outcome=record.outcome,
            validation_retry_count=validation_retry_count,
            original_prompt=original_prompt,
            retry_prompt_template=retry_prompt_template,
            repo_root=repo_root,
        )
        if validation_decision is not None:
            status = validation_decision.status
            validation_passed = validation_decision.passed
            validation_error = validation_decision.error
            validation_error_file = validation_decision.error_file

        # Enrich manifest with CompletionRecord detail
        self._enrich_manifest_from_completion(run_dir, record)

        # Build completion detail for trace event enrichment
        completion_detail = self._build_completion_detail(record)
        if result.completion_record_path:
            completion_detail["completion_path_absolute"] = (
                result.completion_record_path
            )

        # Log completion summary
        self._log_session_completion(
            issue_number, session_name, status, record, result, recovered
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
            blocked_reason=blocked_reason,
            completion_detail=completion_detail,
        )

    def _run_dirty_preflight_before_validation(
        self,
        *,
        record: "CompletionRecord",
        worktree_path: Path,
        issue_number: int,
        issue_title: str,
        session_name: str,
        run_dir: Path | None,
        validation_retry_count: int,
        original_prompt: str | None,
        retry_prompt_template: str | None,
        repo_root: Path | None,
        recovered: bool,
    ) -> SessionDecision | None:
        """Return a validation retry decision if push preconditions are dirty.

        This is intentionally before the validation command. A dirty worktree is
        cheap to detect and actionable for the coder; running a full validation
        suite first just delays the feedback and can hide the true blocker.
        """
        if not (
            record.outcome == CompletionOutcome.COMPLETED
            and RequestedAction.PUSH_BRANCH in record.requested_actions
            and self._validation_cmd
            and self._command_runner
        ):
            return None

        dirty_policy = self.completion_processor.check_dirty_policy(worktree_path)
        if dirty_policy.ok:
            logger.info(
                issue_log(
                    issue_number,
                    "Dirty preflight passed before validation: session=%s worktree=%s",
                ),
                session_name,
                worktree_path,
            )
            return None

        validation_error = (
            "Validation blocked before running command because the worktree "
            f"is not publishable: {dirty_policy.reason}"
        )
        logger.warning(
            issue_log(
                issue_number,
                "Dirty preflight failed before validation: session=%s error=%s",
            ),
            session_name,
            validation_error,
        )

        failure_run_dir = run_dir or self.session_output.ensure_run_dir(
            worktree_path, session_name
        )
        self._record_validation_failure_manifest(
            run_dir=failure_run_dir,
            outcome=record.outcome,
            retry_count=validation_retry_count,
            validation_error=validation_error,
        )
        status = self._route_validation_failure(
            ValidationFailureContext(
                worktree_path=worktree_path,
                run_dir=failure_run_dir,
                session_name=session_name,
                issue_number=issue_number,
                issue_title=issue_title,
                retry_count=validation_retry_count,
                error=validation_error,
                error_file=None,
                original_prompt=original_prompt,
                retry_prompt_template=retry_prompt_template,
                repo_root=repo_root,
            )
        )

        return SessionDecision(
            status=status,
            completion_processed=False,
            recovered_from_timeout=recovered,
            reason=validation_error,
            validation_passed=False,
            validation_error=validation_error,
            validation_error_file=None,
        )

    def _deferred_review_exchange_decision(
        self,
        *,
        result: "ProcessingResult",
        run_dir: Path,
        session_name: str,
        issue_number: int,
        recovered: bool,
    ) -> SessionDecision | None:
        if not result.review_exchange_deferred:
            return None

        # Exchange is running in a background thread. Keep the session active
        # so the next tick re-observes, re-enters the pipeline, and resumes
        # publish once the summary is visible. Do not emit processing_completed:
        # the record is still pending.
        if result.validation_failed_rerouted:
            self._emit_pre_publish_validation_failure(
                run_dir=run_dir,
                session_name=session_name,
                issue_number=issue_number,
                validation_reason=result.message,
            )
        return SessionDecision(
            status=SessionStatus.RUNNING,
            processing_result=result,
            completion_processed=False,
            recovered_from_timeout=recovered,
            reason="Review exchange running in background; awaiting completion",
        )

    def _handle_pre_publish_validation_failure(
        self,
        *,
        run_dir: Path | None,
        session_name: str,
        issue_number: int,
        validation_reason: str,
    ) -> SessionStatus:
        if run_dir is None:
            return SessionStatus.VALIDATION_FAILED
        self._emit_pre_publish_validation_failure(
            run_dir=run_dir,
            session_name=session_name,
            issue_number=issue_number,
            validation_reason=validation_reason,
        )
        return SessionStatus.VALIDATION_FAILED

    def _run_validation_phase_if_needed(
        self,
        *,
        status: SessionStatus,
        worktree_path: Path,
        session_name: str,
        run_dir: Path,
        issue_number: int,
        issue_title: str,
        outcome: CompletionOutcome,
        validation_retry_count: int,
        original_prompt: str | None,
        retry_prompt_template: str | None,
        repo_root: Path | None,
    ) -> ValidationGateDecision | None:
        if not (
            status == SessionStatus.COMPLETED
            and self._validation_cmd
            and self._command_runner
        ):
            return None
        return self._run_validation_gate(
            worktree_path,
            session_name,
            run_dir,
            issue_number,
            issue_title,
            outcome,
            validation_retry_count,
            original_prompt,
            retry_prompt_template,
            repo_root,
        )

    def _emit_pre_publish_validation_failure(
        self,
        *,
        run_dir: Path,
        session_name: str,
        issue_number: int,
        validation_reason: str,
    ) -> None:
        self._emit_event(
            EventName.SESSION_VALIDATION_FAILED,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "validation_cmd": "pre_publish_hook",
                "run_dir": str(run_dir),
                "validation_reason": validation_reason,
                "artifacts": self._validation_record_artifacts(run_dir),
            },
        )

    def _log_completion_lookup(
        self,
        worktree_path: Path,
        issue_number: int,
        session_name: str,
        completion_path: str | None,
    ) -> None:
        """Log completion record lookup details."""
        full_path = (
            (worktree_path / completion_path).resolve()
            if completion_path
            else (worktree_path / ".issue-orchestrator/completion.json").resolve()
        )
        logger.info(
            issue_log(
                issue_number, "Session not running: session=%s checking_completion=%s"
            ),
            session_name,
            completion_path or ".issue-orchestrator/completion.json",
        )
        self._emit_event(
            EventName.COMPLETION_LOOKUP,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "worktree_path": str(worktree_path.resolve()),
                "completion_path": completion_path,
                "full_path": str(full_path),
                "file_exists": full_path.exists(),
            },
        )
        exists = full_path.exists()
        size = full_path.stat().st_size if exists else None
        logger.info(
            issue_log(issue_number, "Completion lookup: exists=%s size=%s path=%s"),
            exists,
            size,
            full_path,
        )

    def _handle_no_completion_record(
        self,
        observation: SessionObservationResult,
        worktree_path: Path,
        issue_number: int,
        session_name: str,
        run_dir: Path | None,
        completion_path: str | None,
    ) -> SessionDecision:
        """Handle case where no completion record exists."""
        debug_context = self._collect_completion_debug_context(
            worktree_path=worktree_path,
            run_dir=run_dir,
            completion_path=completion_path,
        )
        self._write_no_completion_diagnostic(
            observation=observation,
            worktree_path=worktree_path,
            issue_number=issue_number,
            session_name=session_name,
            run_dir=run_dir,
            completion_path=completion_path,
            debug_context=debug_context,
        )
        session_log = self._get_session_log_tail(run_dir, session_name)
        provider_status = self._read_provider_status(run_dir)

        payload = {
            "issue_number": issue_number,
            "session_name": session_name,
            "observation": observation.observation.value,
            "last_output": session_log[-500:] if session_log else "",
        }
        if run_dir:
            payload["run_dir"] = str(run_dir)
        self._emit_event(EventName.SESSION_NO_COMPLETION_RECORD, payload)

        if (
            provider_status
            and provider_status.error_type == ProviderErrorType.TRANSIENT
            and not provider_status.succeeded
        ):
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
                blocked_reason=provider_status.last_error_summary
                or "Provider unavailable",
            )

        if observation.observation == SessionObservation.TIMED_OUT:
            logger.warning(
                issue_log(
                    issue_number,
                    "SESSION COMPLETE: status=TIMED_OUT outcome=none "
                    "reason=no_completion_record session=%s expected_completion=%s "
                    "marker_exists=%s nearby_completion_files=%d",
                ),
                session_name,
                debug_context["requested_completion_abs_path"],
                debug_context["agent_done_marker_exists"],
                len(debug_context["nearby_completion_candidates"]),
            )
            self._log_completion_debug_context(
                issue_number, session_name, debug_context
            )
            if session_log:
                logger.warning(issue_log(issue_number, "LAST OUTPUT:\n%s"), session_log)
            return SessionDecision(
                status=SessionStatus.TIMED_OUT,
                reason="Timed out without completion record",
            )

        logger.error(
            issue_log(
                issue_number,
                "SESSION COMPLETE: status=FAILED outcome=none "
                "reason=no_completion_record session=%s expected_completion=%s "
                "marker_exists=%s nearby_completion_files=%d",
            ),
            session_name,
            debug_context["requested_completion_abs_path"],
            debug_context["agent_done_marker_exists"],
            len(debug_context["nearby_completion_candidates"]),
        )
        self._log_completion_debug_context(issue_number, session_name, debug_context)
        if session_log:
            logger.error(issue_log(issue_number, "LAST OUTPUT:\n%s"), session_log)
        return SessionDecision(
            status=SessionStatus.FAILED, reason="Terminated without completion record"
        )

    def _write_no_completion_diagnostic(
        self,
        observation: SessionObservationResult,
        worktree_path: Path,
        issue_number: int,
        session_name: str,
        run_dir: Path | None,
        completion_path: str | None,
        debug_context: dict[str, Any] | None = None,
    ) -> None:
        """Persist a durable diagnostic snapshot when completion is missing."""
        try:
            requested_rel_path = (
                completion_path or ".issue-orchestrator/completion.json"
            )
            requested_path = (worktree_path / requested_rel_path).resolve()
            if not run_dir:
                run_dir = self.session_output.ensure_run_dir(
                    worktree_path, session_name
                )

            run_dir_completion_path: str | None = None
            run_dir_completion_exists: bool | None = None
            run_dir_completion_size: int | None = None
            if completion_path:
                completion_name = Path(completion_path).name
                run_dir_candidate = run_dir / completion_name
                run_dir_completion_path = str(run_dir_candidate)
                run_dir_completion_exists = run_dir_candidate.exists()
                if run_dir_completion_exists:
                    run_dir_completion_size = run_dir_candidate.stat().st_size

            requested_exists = requested_path.exists()
            requested_size = requested_path.stat().st_size if requested_exists else None

            diagnostic = {
                "kind": "no-completion-record",
                "schema_version": 1,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "issue_number": issue_number,
                "session_name": session_name,
                "observation": observation.observation.value,
                "runtime_minutes": observation.runtime_minutes,
                "requested_completion_path": requested_rel_path,
                "requested_completion_abs_path": str(requested_path),
                "requested_completion_exists": requested_exists,
                "requested_completion_size": requested_size,
                "run_dir": str(run_dir),
                "run_dir_completion_abs_path": run_dir_completion_path,
                "run_dir_completion_exists": run_dir_completion_exists,
                "run_dir_completion_size": run_dir_completion_size,
                "pid": os.getpid(),
            }
            if debug_context:
                diagnostic.update(debug_context)
            diagnostic_path = self.session_output.write_diagnostic(
                run_dir,
                diagnostic,
                prefix="no-completion",
            )
            logger.info(
                issue_log(
                    issue_number,
                    "Saved no-completion diagnostic: session=%s path=%s",
                ),
                session_name,
                diagnostic_path,
            )
        except Exception as exc:
            logger.warning(
                issue_log(
                    issue_number,
                    "Failed to write no-completion diagnostic for session=%s: %s",
                ),
                session_name,
                exc,
            )

    def _collect_completion_debug_context(
        self,
        *,
        worktree_path: Path,
        run_dir: Path | None,
        completion_path: str | None,
    ) -> dict[str, Any]:
        requested_rel_path = completion_path or ".issue-orchestrator/completion.json"
        requested_path = (worktree_path / requested_rel_path).resolve()
        marker_path = worktree_path / _AGENT_DONE_MARKER
        marker_exists = marker_path.exists()
        marker_preview: str | None = None
        if marker_exists:
            try:
                marker_preview = _truncate_with_tail(
                    marker_path.read_text(encoding="utf-8"), 200
                )
            except OSError:
                marker_preview = "<unreadable>"
        return {
            "requested_completion_path": requested_rel_path,
            "requested_completion_abs_path": str(requested_path),
            "agent_done_marker_path": str(marker_path.resolve()),
            "agent_done_marker_exists": marker_exists,
            "agent_done_marker_preview": marker_preview,
            "nearby_completion_candidates": self._find_nearby_completion_candidates(
                worktree_path=worktree_path,
                run_dir=run_dir,
                requested_path=requested_path,
            ),
        }

    def _find_nearby_completion_candidates(
        self,
        *,
        worktree_path: Path,
        run_dir: Path | None,
        requested_path: Path,
    ) -> list[dict[str, Any]]:
        candidates: list[Path] = []
        root_candidates = worktree_path / ".issue-orchestrator"
        if root_candidates.exists():
            candidates.extend(root_candidates.glob("completion*.json"))
            sessions_dir = root_candidates / "sessions"
            if sessions_dir.exists():
                candidates.extend(sessions_dir.glob("**/completion*.json"))

        unique_paths: dict[Path, None] = {}
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved == requested_path:
                continue
            unique_paths[resolved] = None

        sorted_candidates = sorted(
            unique_paths.keys(),
            key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
            reverse=True,
        )[:10]
        records: list[dict[str, Any]] = []
        for candidate in sorted_candidates:
            try:
                stat = candidate.stat()
                relative_to_run_dir = None
                if run_dir is not None:
                    try:
                        relative_to_run_dir = str(candidate.relative_to(run_dir))
                    except ValueError:
                        relative_to_run_dir = None
                records.append(
                    {
                        "path": str(candidate),
                        "size": stat.st_size,
                        "mtime": datetime.fromtimestamp(
                            stat.st_mtime, timezone.utc
                        ).isoformat(),
                        "under_run_dir": relative_to_run_dir is not None,
                        "run_dir_relative_path": relative_to_run_dir,
                    }
                )
            except OSError:
                continue
        return records

    def _log_completion_debug_context(
        self,
        issue_number: int,
        session_name: str,
        debug_context: dict[str, Any],
    ) -> None:
        logger.warning(
            issue_log(
                issue_number,
                "Completion debug: session=%s marker_path=%s marker_exists=%s marker_preview=%s",
            ),
            session_name,
            debug_context["agent_done_marker_path"],
            debug_context["agent_done_marker_exists"],
            debug_context["agent_done_marker_preview"] or "",
        )
        nearby_candidates = debug_context["nearby_completion_candidates"]
        if nearby_candidates:
            logger.warning(
                issue_log(
                    issue_number, "Completion debug: session=%s nearby_candidates=%s"
                ),
                session_name,
                nearby_candidates,
            )
        else:
            logger.warning(
                issue_log(
                    issue_number, "Completion debug: session=%s nearby_candidates=[]"
                ),
                session_name,
            )

    def _read_provider_status(self, run_dir: Path | None) -> ProviderStatus | None:
        if not run_dir:
            return None
        return read_provider_status(run_dir)

    def _get_session_log_tail(self, run_dir: Path | None, session_name: str) -> str:
        """Get last 50 lines of session log for diagnostics."""
        if not run_dir:
            return ""
        log_path = self.session_output.get_log_path_for_run_dir(run_dir)
        if not (log_path and log_path.exists()):
            return ""
        try:
            content = log_path.read_text()
            lines = content.strip().split("\n")
            return "\n".join(lines[-50:])
        except Exception as e:
            logger.debug("Could not read session log: %s", e)
            return ""

    def _log_timeout_recovery(
        self, issue_number: int, session_name: str, record: "CompletionRecord"
    ) -> None:
        """Log and emit event for timeout recovery."""
        logger.info(
            issue_log(
                issue_number,
                "Session timed out but has completion.json - recovering work: outcome=%s",
            ),
            record.outcome.value,
        )
        self._emit_event(
            EventName.SESSION_TIMEOUT_RECOVERED,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "outcome": record.outcome.value,
            },
        )

    def _extract_pr_number_from_session_name(self, session_name: str) -> int | None:
        """Extract PR number from review session name."""
        if not session_name.startswith("review-"):
            return None
        try:
            pr_number = int(session_name.replace("review-", ""))
            logger.debug(f"Review session detected, PR number: {pr_number}")
            return pr_number
        except ValueError:
            logger.warning(
                f"Could not parse PR number from session name: {session_name}"
            )
            return None

    def _emit_processing_completed_event(
        self,
        worktree_path: Path,
        issue_number: int,
        session_name: str,
        run_dir: Path | None,
        result: "ProcessingResult",
    ) -> None:
        """Emit session processing completed event."""
        payload = {
            "issue_number": issue_number,
            "session_name": session_name,
            "success": result.success,
            "message": result.message,
            "actions_taken": result.actions_taken,
            "errors": result.errors,
            "pr_url": result.pr_url,
        }
        if run_dir:
            payload["run_dir"] = str(run_dir)
        self._emit_event(EventName.SESSION_PROCESSING_COMPLETED, payload)

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
        run_dir: Path,
        issue_number: int,
        issue_title: str,
        outcome: CompletionOutcome,
        validation_retry_count: int,
        original_prompt: str | None,
        retry_prompt_template: str | None,
        repo_root: Path | None,
    ) -> ValidationGateDecision:
        """Run validation gate and return updated status."""
        logger.info(
            issue_log(issue_number, "Running validation gate: cmd=%s timeout=%ds"),
            self._validation_cmd,
            self._validation_timeout,
        )
        validation_passed, validation_error, validation_error_file = (
            self._run_validation(worktree_path, session_name, issue_number, run_dir)
        )

        if validation_passed:
            dirty_after_validation = self._handle_dirty_after_validation_if_needed(
                worktree_path=worktree_path,
                run_dir=run_dir,
                session_name=session_name,
                issue_number=issue_number,
                issue_title=issue_title,
                outcome=outcome,
                validation_retry_count=validation_retry_count,
                original_prompt=original_prompt,
                retry_prompt_template=retry_prompt_template,
                repo_root=repo_root,
            )
            if dirty_after_validation is not None:
                return dirty_after_validation

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
            self._emit_event(
                EventName.SESSION_VALIDATION_PASSED,
                {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "validation_cmd": self._validation_cmd,
                    "run_dir": str(run_dir),
                    "artifacts": self._validation_record_artifacts(
                        run_dir,
                        require_record=True,
                    ),
                },
            )
            return ValidationGateDecision(
                status=SessionStatus.COMPLETED,
                passed=validation_passed,
                error=validation_error,
                error_file=validation_error_file,
            )

        self._record_validation_failure_manifest(
            run_dir=run_dir,
            outcome=outcome,
            retry_count=validation_retry_count,
            validation_error=validation_error,
        )
        return ValidationGateDecision(
            status=self._route_validation_failure(
                ValidationFailureContext(
                    worktree_path=worktree_path,
                    run_dir=run_dir,
                    session_name=session_name,
                    issue_number=issue_number,
                    issue_title=issue_title,
                    retry_count=validation_retry_count,
                    error=validation_error,
                    error_file=validation_error_file,
                    original_prompt=original_prompt,
                    retry_prompt_template=retry_prompt_template,
                    repo_root=repo_root,
                )
            ),
            passed=False,
            error=validation_error,
            error_file=validation_error_file,
        )

    def _handle_dirty_after_validation_if_needed(
        self,
        *,
        worktree_path: Path,
        run_dir: Path,
        session_name: str,
        issue_number: int,
        issue_title: str,
        outcome: CompletionOutcome,
        validation_retry_count: int,
        original_prompt: str | None,
        retry_prompt_template: str | None,
        repo_root: Path | None,
    ) -> ValidationGateDecision | None:
        dirty_policy = self.completion_processor.check_dirty_policy(worktree_path)
        if dirty_policy.ok:
            logger.info(
                issue_log(
                    issue_number,
                    "Dirty postflight passed after validation: session=%s worktree=%s",
                ),
                session_name,
                worktree_path,
            )
            return None

        validation_error = (
            "Validation command passed, but the worktree became dirty before "
            f"publish: {dirty_policy.reason}"
        )
        logger.warning(
            issue_log(
                issue_number,
                "Dirty postflight failed after validation: session=%s error=%s",
            ),
            session_name,
            validation_error,
        )
        self._record_validation_failure_manifest(
            run_dir=run_dir,
            outcome=outcome,
            retry_count=validation_retry_count,
            validation_error=validation_error,
        )
        status = self._route_validation_failure(
            ValidationFailureContext(
                worktree_path=worktree_path,
                run_dir=run_dir,
                session_name=session_name,
                issue_number=issue_number,
                issue_title=issue_title,
                retry_count=validation_retry_count,
                error=validation_error,
                error_file=None,
                original_prompt=original_prompt,
                retry_prompt_template=retry_prompt_template,
                repo_root=repo_root,
            )
        )
        return ValidationGateDecision(
            status=status,
            passed=False,
            error=validation_error,
            error_file=None,
        )

    def _record_validation_failure_manifest(
        self,
        *,
        run_dir: Path,
        outcome: CompletionOutcome,
        retry_count: int,
        validation_error: str | None,
    ) -> None:
        self.session_output.update_manifest(
            run_dir,
            {
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "outcome": outcome.value,
                "validation_passed": False,
                "validation_status": "retry"
                if retry_count < self._max_validation_retries
                else "failed",
                "validation_reason": validation_error,
            },
        )

    def _route_validation_failure(
        self, failure: ValidationFailureContext
    ) -> SessionStatus:
        if failure.retry_count < self._max_validation_retries:
            return self._handle_validation_retry(failure)
        return self._handle_validation_exhausted(failure)

    def _handle_validation_retry(
        self,
        failure: ValidationFailureContext,
    ) -> SessionStatus:
        """Handle validation failure with retries remaining."""
        validation_summary = self._validation_error_summary(failure.error_file)
        logger.warning(
            issue_log(
                failure.issue_number,
                "Validation gate FAILED (retry %d/%d): cmd=%s error=%s summary=%s error_file=%s run_dir=%s",
            ),
            failure.retry_count + 1,
            self._max_validation_retries,
            self._validation_cmd,
            failure.error[:200] if failure.error else "none",
            validation_summary or "none",
            failure.error_file,
            failure.run_dir,
        )
        state = ValidationState(
            retry_count=failure.retry_count + 1,
            max_retries=self._max_validation_retries,
            validation_cmd=self._validation_cmd,
            last_error=failure.error[:2000] if failure.error else None,
            last_error_file=str(failure.error_file)
            if failure.error_file
            else None,
        )
        self.session_output.write_validation_state(failure.run_dir, state)

        task_prompt = failure.original_prompt or failure.issue_title
        retry_prompt_content = self._render_retry_prompt(
            task_prompt=task_prompt,
            validation_error=failure.error or "Unknown error",
            validation_error_file=failure.error_file,
            retry_count=failure.retry_count,
            max_retries=self._max_validation_retries,
            template_path=failure.retry_prompt_template,
            repo_root=failure.repo_root,
        )
        self.session_output.write_retry_prompt(failure.run_dir, retry_prompt_content)

        self._emit_event(
            EventName.SESSION_VALIDATION_RETRY_NEEDED,
            {
                "issue_number": failure.issue_number,
                "session_name": failure.session_name,
                "validation_cmd": self._validation_cmd,
                "error_file": str(failure.error_file)
                if failure.error_file
                else None,
                "validation_reason": failure.error,
                "validation_error_summary": validation_summary,
                "retry_count": failure.retry_count,
                "max_retries": self._max_validation_retries,
                "run_dir": str(failure.run_dir),
                "artifacts": self._validation_record_artifacts(failure.run_dir),
            },
        )
        return SessionStatus.NEEDS_VALIDATION_RETRY

    def _handle_validation_exhausted(
        self,
        failure: ValidationFailureContext,
    ) -> SessionStatus:
        """Handle validation failure with max retries exhausted."""
        logger.warning(
            issue_log(
                failure.issue_number,
                "Validation gate FAILED (max retries %d exhausted): error=%s error_file=%s",
            ),
            self._max_validation_retries,
            failure.error[:200] if failure.error else "none",
            failure.error_file,
        )
        self.session_output.clear_retry_state(failure.run_dir)
        self._emit_event(
            EventName.SESSION_VALIDATION_FAILED,
            {
                "issue_number": failure.issue_number,
                "session_name": failure.session_name,
                "validation_cmd": self._validation_cmd,
                "error_file": str(failure.error_file)
                if failure.error_file
                else None,
                "retry_count": failure.retry_count,
                "run_dir": str(failure.run_dir),
                "artifacts": self._validation_record_artifacts(failure.run_dir),
            },
        )
        return SessionStatus.VALIDATION_FAILED

    def _resolve_run_dir(
        self,
        worktree_path: Path,
        session_name: str,
        completion_session_name: str | None,
    ) -> Path:
        """Pick the most relevant run directory for the session being processed."""
        if completion_session_name:
            run_dir = self.session_output.find_run_dir(
                worktree_path, completion_session_name
            )
            if run_dir:
                return run_dir

        run_dir = self.session_output.find_run_dir(worktree_path, session_name)
        if run_dir:
            return run_dir

        fallback_name = completion_session_name or session_name
        run_dir = self.session_output.ensure_run_dir(worktree_path, fallback_name)
        logger.warning(
            "Run dir missing for session=%s; created fallback run dir at %s",
            fallback_name,
            run_dir,
        )
        return run_dir

    def _enrich_manifest_from_completion(
        self,
        run_dir: Path | None,
        record: "CompletionRecord",
    ) -> None:
        """Write CompletionRecord detail into the run manifest."""
        from ..domain.run_manifest import RunManifest

        if not run_dir:
            logger.debug(
                "[MANIFEST] No run dir — skipping enrichment",
            )
            return

        try:
            manifest = RunManifest.load(run_dir)
            manifest.enrich_from_completion_record(record)
            manifest.save()
        except Exception as exc:
            logger.warning(
                "[MANIFEST] Failed to enrich manifest for %s: %s",
                run_dir.name.split("__", 1)[-1]
                if "__" in run_dir.name
                else run_dir.name,
                exc,
            )

    @staticmethod
    def _build_completion_detail(record: "CompletionRecord") -> dict[str, Any]:
        """Extract curated fields from CompletionRecord for trace events."""
        detail: dict[str, Any] = {}
        for key in (
            "implementation",
            "problems",
            "attempted",
            "blocked_reason",
            "blocked_by",
            "question",
            "review_summary",
            "review_issues",
            "risk_level",
            "follow_up_issues",
        ):
            value = getattr(record, key, None)
            if value is not None:
                if key == "follow_up_issues":
                    detail[key] = [issue.to_dict() for issue in value]
                    continue
                detail[key] = value
        return detail

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
                issue_log(
                    issue_number,
                    "SESSION COMPLETE: status=%s outcome=%s pr=%s recovered=%s session=%s",
                ),
                status.value,
                record.outcome.value,
                pr_url,
                recovered,
                session_name,
            )
        else:
            logger.error(
                issue_log(
                    issue_number,
                    "SESSION COMPLETE: status=%s outcome=%s pr=%s recovered=%s errors=%s session=%s",
                ),
                status.value,
                record.outcome.value,
                pr_url,
                recovered,
                result.errors,
                session_name,
            )

    def _run_validation(
        self,
        worktree_path: Path,
        session_name: str,
        issue_number: int,
        run_dir: Path | None = None,
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
        target_run_dir = run_dir or self.session_output.ensure_run_dir(
            worktree_path, session_name
        )

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

        result = gate.check(session_output_dir=target_run_dir)
        if result.cache_hit:
            if result.record is None:
                raise RuntimeError("validation cache hit did not include a record")
            self._materialize_cached_validation_record(target_run_dir, result.record)

        if result.allowed:
            if result.cache_hit:
                logger.info(
                    issue_log(
                        issue_number, "Validation cache hit: SHA=%s (skipped re-run)"
                    ),
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

    @staticmethod
    def _materialize_cached_validation_record(run_dir: Path, record: Any) -> None:
        record_path = run_dir / "validation-record.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record.to_dict(), indent=2) + "\n")

    @staticmethod
    def _validation_error_summary(validation_error_file: Optional[Path]) -> str | None:
        """Return a short human-meaningful excerpt from validation stderr."""
        if validation_error_file is None or not validation_error_file.exists():
            return None
        try:
            for raw_line in validation_error_file.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                line = raw_line.strip()
                if line:
                    return line[:300]
        except OSError:
            return None
        return None

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
                    logger.warning(
                        "Failed to load retry template from %s: %s",
                        template_full_path,
                        e,
                    )
            else:
                logger.warning(
                    "Retry template not found at %s, using default", template_full_path
                )

        # Render template with variables
        # Note: retry_count is 0-based internally, display as 1-based
        display_count = retry_count + 1
        display_max = max_retries + 1
        return template.format(
            original_task=task_prompt,
            validation_cmd=self._validation_cmd or "",
            error_file=str(validation_error_file)
            if validation_error_file
            else "unknown",
            error_summary=_truncate_with_tail(validation_error),
            retry_count=display_count,
            max_retries=display_max,
            retries_remaining=display_max - display_count,
        )

    def _emit_event(self, event_type: EventName, data: dict[str, Any]) -> None:
        """Emit a trace event."""
        self.events.publish(make_trace_event(event_type, data))

    @staticmethod
    def _validation_record_artifacts(
        run_dir: Path, *, require_record: bool = False
    ) -> list[dict[str, str]]:
        record_path = run_dir / "validation-record.json"
        if not record_path.exists():
            if require_record:
                raise FileNotFoundError(
                    f"validation-record.json missing for passed validation event: {record_path}"
                )
            return []
        return [
            {
                "type": "validation",
                "label": "Validation Record",
                "value": str(record_path),
            }
        ]
