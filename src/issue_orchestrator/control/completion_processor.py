"""Completion processor - handles agent completion records.

This controller reads CompletionRecords written by coding-done/reviewer-done and executes
the appropriate actions via adapters.

Architecture principle: The agent reports intent; the orchestrator decides and executes.

The agent does NOT:
- Push code
- Create PRs
- Post comments
- Mutate labels

All those actions are performed here after validating the completion record
as untrusted input.
"""

import json
import logging
import os
import shutil
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from ..domain.models import (
    CompletionRecord,
    RequestedAction,
    COMPLETION_RECORD_PATH,
)
from ..domain.events import EventBus, SessionEvent
from ..events import EventContext, EventName
from ..ports import EventSink
from ..ports.event_sink import RunScopedEventPayload, make_run_scoped_event, make_trace_event
from ..infra.timeline_trace import is_timeline_trace_enabled
from ..infra.worktree_base import resolve_base_branch
from ..ports.session_output import SessionOutput, ValidationRecord
from .validation import PublishGate, ValidationRecordStore
from .completion_pr_collision import (
    create_pr_with_collision_handling,
    get_open_pr_for_issue,
    maybe_switch_branch_for_pr_collision,
)
from .completion_failure_reporting import (
    build_gate_failure_comment,
    build_processing_failure_comment,
)
from .completion_record_validation import CompletionRecordValidator
from .completion_result_artifacts import (
    build_pr_body,
    build_processing_result,
    cleanup_completion_record,
    preserve_completion_record,
    write_reviewer_feedback_file,
)
from .completion_review_exchange import CompletionReviewExchange
from .completion_ports import GitAdapter, LabelAdapter, PRAdapter
from .completion_types import (
    ERROR_PREFIX_CREATE_PR,
    ERROR_PREFIX_PUBLISH_BLOCKED,
    ERROR_PREFIX_PUSH,
    ProcessingResult,
)
from ..ports.pull_request_tracker import PRInfo
from ..ports.working_copy import PushResult, RebaseResult

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..infra.config import Config


class CompletionProcessor:
    """Process agent completion records and execute requested actions.

    This is a control-plane component that:
    1. Reads completion records (untrusted input from agents)
    2. Validates the record and current worktree state
    3. Decides which actions to actually execute (may differ from requested)
    4. Executes actions via adapters (execution plane)

    The processor has AUTHORITY to reject or modify requested actions based on policy.
    """

    def __init__(
        self,
        label_adapter: LabelAdapter,
        pr_adapter: PRAdapter,
        git_adapter: GitAdapter,
        session_output: SessionOutput,
        event_bus: EventBus | None = None,
        label_config: dict[str, str] | None = None,
        publish_gate: PublishGate | None = None,
        config: "Config | None" = None,
    ):
        """Initialize the processor with required adapters.

        Args:
            label_adapter: Adapter for label operations (add/remove labels).
            pr_adapter: Adapter for PR operations (create PR, add comment).
            git_adapter: Adapter for git operations (push).
            session_output: Session output storage for artifacts.
            event_bus: Optional EventBus for emitting processing events.
            label_config: Optional mapping of label names (e.g., {"blocked": "blocked"}).
            publish_gate: Optional PublishGate for validating before publish actions.
        """
        self.label_adapter = label_adapter
        self.pr_adapter = pr_adapter
        self.git_adapter = git_adapter
        self.session_output = session_output
        self.event_bus = event_bus
        self._trace_events: EventSink | None = None
        self._event_context: EventContext | None = None
        self.label_config = label_config or {}
        self.publish_gate = publish_gate
        self._config = config
        self._pr_collision_strategy = (
            config.worktree_remediation_pr_collision
            if config is not None
            else "new_branch"
        )
        self._push_rebase_retry = (
            config.worktree_remediation_push_rebase_retry
            if config is not None
            else True
        )
        self._review_exchange = CompletionReviewExchange(
            config=config,
            session_output=session_output,
            emit_review_started=self._emit_review_started,
            emit_review_outcome=self._emit_review_outcome,
        )
        self._record_validator = CompletionRecordValidator(
            config=config,
            git_adapter=git_adapter,
        )

    def _emit(
        self,
        event_type: SessionEvent,
        issue_number: int,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Emit an event if event_bus is configured."""
        if self.event_bus:
            self.event_bus.publish(
                event_type,
                entity_id=issue_number,
                data=data or {},
                source="completion_processor",
            )

    def _add_issue_comment(self, issue_number: int, comment: str, *, context: str) -> None:
        try:
            self.pr_adapter.add_comment(issue_number, comment)
        except Exception as exc:
            logger.warning(
                "Failed to add %s comment for #%d: %s",
                context,
                issue_number,
                exc,
            )

    def set_event_emitter(self, events: EventSink, event_context: EventContext) -> None:
        """Attach TraceEvent emitter for review exchange events."""
        self._trace_events = events
        self._event_context = event_context

    def _get_label(self, key: str) -> str:
        """Get label name from config, or use default."""
        defaults = {
            "blocked": "blocked",
            "needs_human": "needs-human",
            "code_reviewed": "code-reviewed",
            "needs_rework": "needs-rework",
            "code_review": "code-review",
            "in_progress": "in-progress",
            "validation_failed": "validation-failed",
        }
        return self.label_config.get(key, defaults.get(key, key))

    def _base_branch(self) -> str:
        if self._config is None:
            return "main"
        resolved = resolve_base_branch(
            self._config.repo_root,
            config_override=self._config.worktree_base_branch_override,
            default_branch_resolver=self.git_adapter.default_branch,
            log=logger,
        )
        return resolved.branch

    def read_completion_record(
        self, worktree: Path, completion_path: str | None = None
    ) -> CompletionRecord | None:
        return self._record_validator.read_completion_record(worktree, completion_path)

    def _resolve_agent_label_from_completion_path(
        self, completion_path: str | None
    ) -> tuple[str | None, str | None]:
        return self._record_validator.resolve_agent_label_from_completion_path(
            completion_path
        )

    def validate_worktree_state(
        self, worktree: Path, record: CompletionRecord
    ) -> tuple[bool, str]:
        return self._record_validator.validate_worktree_state(worktree, record)

    def _check_dirty_policy(self, worktree: Path) -> tuple[bool, str]:
        return self._record_validator.check_dirty_policy(worktree)

    @staticmethod
    def _is_ignored_dirty_path(path: str) -> bool:
        return CompletionRecordValidator.is_ignored_dirty_path(path)

    def _emit_review_comment_added(
        self,
        *,
        issue_number: int,
        pr_number: int,
        comment_url: str | None,
        comment_body: str,
        run_dir: Path | None = None,
    ) -> None:
        """Emit trace event for a posted review comment (if trace events are configured)."""
        if self._trace_events is None or self._event_context is None:
            return
        excerpt = comment_body.strip().replace("\n", " ")
        payload = {
            "issue_number": issue_number,
            "pr_number": pr_number,
            "comment_url": comment_url or "",
            "comment_excerpt": excerpt[:180] if excerpt else "",
            "summary": "Posted review comment",
        }
        if run_dir is not None:
            payload["run_dir"] = str(run_dir)
        self._trace_events.publish(
            make_trace_event(
                EventName.REVIEW_COMMENT_ADDED,
                self._event_context.enrich(payload),
            )
        )

    def _emit_review_started(
        self,
        *,
        issue_number: int,
        reviewer_label: str | None,
        exchange_mode: str,
        run_dir: Path,
    ) -> None:
        """Emit trace event when local review exchange starts."""
        if self._trace_events is None or self._event_context is None:
            return
        payload: RunScopedEventPayload = {
            "issue_number": issue_number,
            "task": "review",
            "agent": reviewer_label or "",
            "review_exchange_mode": exchange_mode,
            "run_id": str(self._event_context.run_id),
            "run_dir": str(run_dir),
        }
        self._trace_events.publish(make_run_scoped_event(EventName.REVIEW_STARTED, payload))

    def _emit_review_outcome(
        self,
        *,
        issue_number: int,
        reviewer_label: str | None,
        exchange_mode: str,
        approved: bool,
        rounds: int | None,
        summary: str,
        run_dir: Path | None = None,
    ) -> None:
        """Emit review terminal event from local exchange outcome."""
        if self._trace_events is None or self._event_context is None:
            return
        payload = {
            "issue_number": issue_number,
            "task": "review",
            "agent": reviewer_label or "",
            "review_exchange_mode": exchange_mode,
            "rounds": rounds,
            "summary": summary,
        }
        if run_dir is not None:
            payload["run_dir"] = str(run_dir)
        event_name = EventName.REVIEW_APPROVED if approved else EventName.REVIEW_CHANGES_REQUESTED
        self._trace_events.publish(
            make_trace_event(
                event_name,
                self._event_context.enrich(payload),
            )
        )

    def _requires_publish_gate(self, record: CompletionRecord) -> bool:
        """Check if the completion record requests actions that require publish gate.

        Args:
            record: The completion record to check.

        Returns:
            True if any requested action requires publish gate validation.
        """
        publish_actions = {RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR}
        return bool(set(record.requested_actions) & publish_actions)

    def _check_publish_gate(
        self,
        worktree: Path,
        session_output_dir: Path | None = None,
    ) -> tuple[bool, str, ValidationRecord | None]:
        """Check if publishing is allowed by the publish gate.

        Args:
            worktree: Path to the worktree.
            session_output_dir: If provided, validation output is written directly here.

        Returns:
            Tuple of (allowed, reason, record).
        """
        if self.publish_gate is None:
            # No gate configured = allowed
            return True, "", None

        result = self.publish_gate.check(session_output_dir=session_output_dir)
        if result.allowed:
            cache_note = " (cached)" if result.cache_hit else ""
            logger.info("Publish gate passed%s: %s", cache_note, result.reason)
            return True, result.reason, result.record
        else:
            logger.warning("Publish gate failed: %s", result.reason)
            return False, result.reason, result.record

    @staticmethod
    def _load_validation_record(record_path: Path) -> ValidationRecord | None:
        try:
            data = json.loads(record_path.read_text())
        except OSError:
            return None
        except json.JSONDecodeError:
            return None
        try:
            return ValidationRecord.from_dict(data)
        except TypeError:
            return None

    def _attach_validation_artifacts(
        self,
        worktree: Path,
        session_name: str,
        record: ValidationRecord | None = None,
        record_path: Path | None = None,
    ) -> None:
        """Attach validation artifacts to session output.

        Updates manifest with paths to validation files that should already exist
        in the session output directory (written directly by validation).
        """
        run_dir = self.session_output.ensure_run_dir(worktree, session_name)
        if record_path is None and record is not None:
            record_path = ValidationRecordStore(worktree).get_record_path(record.head_sha)
        run_dir_record_path = run_dir / "validation-record.json"
        if not run_dir_record_path.exists() and record_path is not None and record_path.exists():
            try:
                shutil.copy2(record_path, run_dir_record_path)
            except OSError:
                logger.debug(
                    "Failed to copy validation record from %s to %s",
                    record_path,
                    run_dir_record_path,
                )
        effective_record_path = run_dir_record_path if run_dir_record_path.exists() else record_path
        if effective_record_path is not None:
            self.session_output.update_manifest(
                run_dir,
                {"validation_record_path": str(effective_record_path)},
            )
            try:
                (run_dir / "validation-record.path").write_text(str(effective_record_path))
            except OSError:
                logger.debug("Failed to write validation pointer for %s", run_dir)

        # Update manifest with validation output paths (files written by validation)
        updates: dict[str, str] = {}
        stdout_path = run_dir / "validation-stdout.log"
        stderr_path = run_dir / "validation-stderr.log"

        if stdout_path.exists():
            updates["validation_stdout"] = str(stdout_path)
        if stderr_path.exists():
            updates["validation_stderr"] = str(stderr_path)

        if updates:
            self.session_output.update_manifest(run_dir, updates)

    def process(
        self,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        pr_number: int | None = None,
        completion_path: str | None = None,
        agent_label: str | None = None,
    ) -> ProcessingResult:
        """Process a completion record and execute actions.

        Args:
            worktree: Path to the worktree containing the completion record.
            issue_number: The GitHub issue number this work is for.
            issue_title: The issue title (for PR creation).
            pr_number: Optional PR number for review sessions. When provided,
                label operations will target the PR instead of the issue.
            completion_path: Relative path to completion file. If None, uses legacy path.

        Returns:
            ProcessingResult with success status and details.
        """
        start_time = time.monotonic()
        # For review sessions, label operations target the PR
        label_target = pr_number if pr_number else issue_number
        actions_taken: list[str] = []
        errors: list[str] = []
        error_details: list[dict[str, Any]] = []  # Full diagnostic info per error
        pr_url: str | None = None

        # Read and validate completion record
        record, session_name, error_result = self._read_and_validate_record(
            worktree, completion_path
        )
        if error_result:
            return error_result
        assert record is not None  # Guaranteed if error_result is None

        # Validate worktree state
        valid, reason = self.validate_worktree_state(worktree, record)
        if not valid:
            tagged_reason = f"{ERROR_PREFIX_PUBLISH_BLOCKED}: {reason}"
            comment = build_processing_failure_comment(
                errors=[tagged_reason],
                actions_taken=[],
                diagnostic_path=None,
            )
            self._add_issue_comment(issue_number, comment, context="processing failure")
            return ProcessingResult(
                success=False,
                message=f"Validation failed: {reason}",
                errors=[tagged_reason],
            )

        # Check publish gate if actions require it
        gate_error = self._check_publish_gate_if_required(
            worktree, record, session_name, issue_number
        )
        if gate_error:
            return gate_error

        # Get branch name for PR operations
        branch = self.git_adapter.get_current_branch(worktree)
        logger.info(
            "Completion worktree state: issue=%s branch=%s worktree=%s",
            issue_number,
            branch,
            worktree,
        )

        # Log what actions were requested
        logger.info(
            "Processing completion for #%d: outcome=%s, requested_actions=%s",
            issue_number,
            record.outcome.value,
            [a.value for a in record.requested_actions],
        )

        if agent_label is None:
            agent_label, agent_error = self._resolve_agent_label_from_completion_path(
                completion_path
            )
            if agent_error:
                return ProcessingResult(
                    success=False,
                    message=agent_error,
                    errors=[agent_error],
                )

        preserved_completion_path = preserve_completion_record(
            session_output=self.session_output,
            worktree=worktree,
            completion_path=completion_path,
            session_name=session_name,
        )

        # Execute requested actions in order
        branch, pr_url, review_exchange_completed = self._execute_actions(
            worktree=worktree,
            record=record,
            issue_number=issue_number,
            issue_title=issue_title,
            label_target=label_target,
            branch=branch,
            session_name=session_name,
            agent_label=agent_label,
            actions_taken=actions_taken,
            errors=errors,
            error_details=error_details,
        )

        # Write reviewer feedback to session run directory for rework sessions to use
        # This is only relevant for review sessions (pr_number provided) with feedback
        if pr_number and record.review_issues and session_name:
            run_dir = self.session_output.find_run_dir(worktree, session_name)
            if run_dir:
                write_reviewer_feedback_file(run_dir, pr_number, record.review_issues)

        # Build and return result
        total_duration = time.monotonic() - start_time
        return build_processing_result(
            session_output=self.session_output,
            worktree=worktree,
            record=record,
            session_name=session_name,
            issue_number=issue_number,
            issue_title=issue_title,
            branch=branch,
            pr_url=pr_url,
            review_exchange_completed=review_exchange_completed,
            actions_taken=actions_taken,
            errors=errors,
            error_details=error_details,
            total_duration=total_duration,
            completion_path=completion_path,
            preserved_completion_path=preserved_completion_path,
            emit_completion_event=self._emit,
            post_issue_comment=self._add_issue_comment,
            cleanup_completion_record_fn=self._cleanup_completion_record,
        )

    def _read_and_validate_record(
        self,
        worktree: Path,
        completion_path: str | None,
    ) -> tuple[CompletionRecord | None, str | None, ProcessingResult | None]:
        """Read completion record and attach validation artifacts.

        Returns:
            Tuple of (record, session_name, error_result).
            If error_result is not None, caller should return it immediately.
        """
        record = self.read_completion_record(worktree, completion_path)
        if not record:
            return None, None, ProcessingResult(
                success=False,
                message="No completion record found",
                errors=["Completion record not found or invalid"],
            )

        session_name = self.session_output.session_name_from_path(completion_path) or record.session_id
        if record.validation_record_path and session_name:
            self._attach_validation_artifacts(
                worktree,
                session_name,
                record_path=Path(record.validation_record_path),
            )
        if session_name:
            run_dir = self.session_output.find_run_dir(worktree, session_name)
            if run_dir:
                self.session_output.attach_claude_log(run_dir)

        return record, session_name, None

    def _check_publish_gate_if_required(
        self,
        worktree: Path,
        record: CompletionRecord,
        session_name: str | None,
        issue_number: int,
    ) -> ProcessingResult | None:
        """Check publish gate if actions require it.

        Returns:
            ProcessingResult if gate check failed, None if passed or not required.
        """
        if not self._requires_publish_gate(record):
            return None
        # Get session output dir for validation to write directly there
        if not session_name:
            comment = build_processing_failure_comment(
                errors=["session_name is required for publish gate"],
                actions_taken=[],
                diagnostic_path=None,
            )
            self._add_issue_comment(issue_number, comment, context="processing failure")
            return ProcessingResult(
                success=False,
                message="Publish gate requires session output but no session name available",
                errors=["session_name is required for publish gate"],
            )
        session_output_dir = self.session_output.find_run_dir(worktree, session_name)
        if session_output_dir is None:
            message = f"Session output directory not found for {session_name}"
            comment = build_processing_failure_comment(
                errors=[message],
                actions_taken=[],
                diagnostic_path=None,
            )
            self._add_issue_comment(issue_number, comment, context="processing failure")
            return ProcessingResult(
                success=False,
                message=f"Publish gate requires session output but run dir not found for {session_name}",
                errors=[message],
            )

        gate_passed, gate_reason, gate_record = self._check_publish_gate(
            worktree, session_output_dir=session_output_dir
        )
        if not gate_passed:
            return self._handle_gate_failure(
                worktree, record, session_name, issue_number, gate_reason, gate_record
            )
        else:
            # Attach validation artifacts even on success
            if gate_record and session_name:
                record_path = ValidationRecordStore(worktree).get_record_path(gate_record.head_sha)
                self._attach_validation_artifacts(
                    worktree,
                    session_name,
                    record=gate_record,
                    record_path=record_path,
                )
        return None

    def _handle_gate_failure(
        self,
        worktree: Path,
        record: CompletionRecord,
        session_name: str | None,
        issue_number: int,
        gate_reason: str,
        gate_record: ValidationRecord | None,
    ) -> ProcessingResult:
        """Handle publish gate failure."""
        if gate_record and session_name:
            record_path = ValidationRecordStore(worktree).get_record_path(gate_record.head_sha)
            self._attach_validation_artifacts(
                worktree,
                session_name,
                record=gate_record,
                record_path=record_path,
            )
        if session_name:
            run_dir = self.session_output.ensure_run_dir(worktree, session_name)
            self.session_output.update_manifest(
                run_dir,
                {
                    "validation_passed": False,
                    "validation_failure_reason": gate_reason,
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        # Add validation-failed label so user knows why issue is stuck
        validation_failed_label = self._get_label("validation_failed")
        try:
            self.label_adapter.add_label(issue_number, validation_failed_label)
            logger.info(
                "Added '%s' label to issue #%d due to validation failure",
                validation_failed_label,
                issue_number,
            )
        except Exception as e:
            logger.warning(
                "Failed to add validation-failed label to issue #%d: %s",
                issue_number,
                e,
            )
        comment = build_gate_failure_comment(
            gate_reason=gate_reason,
            validation_failed_label=validation_failed_label,
        )
        self._add_issue_comment(issue_number, comment, context="validation failure")

        self._emit(
            SessionEvent.FAILED,
            issue_number,
            {
                "outcome": record.outcome.value,
                "gate_failure": gate_reason,
            },
        )
        return ProcessingResult(
            success=False,
            message=f"Validation failed: {gate_reason}",
            errors=[f"Validation: {gate_reason}"],
        )

    def _execute_actions(
        self,
        *,
        worktree: Path,
        record: CompletionRecord,
        issue_number: int,
        issue_title: str,
        label_target: int,
        branch: str | None,
        session_name: str | None,
        agent_label: str | None,
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
    ) -> tuple[str | None, str | None, bool]:
        """Execute all requested actions from completion record.

        Returns:
            Tuple of (final_branch, pr_url, review_exchange_completed).
        """
        pr_url: str | None = None
        requested_actions = tuple(record.requested_actions)
        (
            plan,
            exchange_mode,
            exchange_result,
            review_exchange_completed,
            should_halt,
        ) = self._review_exchange.prepare_review_exchange(
            requested_actions=requested_actions,
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            agent_label=agent_label,
            record=record,
            errors=errors,
            actions_taken=actions_taken,
            run_review_exchange_loop=self._run_review_exchange_loop,
        )
        if should_halt:
            return branch, pr_url, review_exchange_completed

        return self._execute_planned_actions(
            plan=plan,
            worktree=worktree,
            record=record,
            issue_number=issue_number,
            issue_title=issue_title,
            label_target=label_target,
            branch=branch,
            session_name=session_name,
            agent_label=agent_label,
            actions_taken=actions_taken,
            errors=errors,
            error_details=error_details,
            exchange_mode=exchange_mode,
            exchange_result=exchange_result,
            review_exchange_completed=review_exchange_completed,
        )

    def _execute_planned_actions(
        self,
        *,
        plan: Any,
        worktree: Path,
        record: CompletionRecord,
        issue_number: int,
        issue_title: str,
        label_target: int,
        branch: str | None,
        session_name: str | None,
        agent_label: str | None,
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
        exchange_mode: str | None,
        exchange_result: Any | None,
        review_exchange_completed: bool,
    ) -> tuple[str | None, str | None, bool]:
        pr_url: str | None = None

        for action in plan.ordered_actions:
            result = self._execute_action_with_observability(
                action=action,
                worktree=worktree,
                record=record,
                issue_number=issue_number,
                issue_title=issue_title,
                label_target=label_target,
                branch=branch,
                session_name=session_name,
                agent_label=agent_label,
                actions_taken=actions_taken,
                errors=errors,
                error_details=error_details,
                exchange_mode=exchange_mode,
                exchange_result=exchange_result,
            )
            if result is None:
                continue
            if result.branch:
                branch = result.branch
            if result.pr_url:
                pr_url = result.pr_url
            if result.review_exchange_completed:
                review_exchange_completed = True
            if result.skip_remaining:
                continue
            if result.halt:
                logger.warning(
                    "Halting remaining actions for issue #%d due to push failure",
                    issue_number,
                )
                break

        return branch, pr_url, review_exchange_completed

    def _execute_action_with_observability(
        self,
        *,
        action: RequestedAction,
        worktree: Path,
        record: CompletionRecord,
        issue_number: int,
        issue_title: str,
        label_target: int,
        branch: str | None,
        session_name: str | None,
        agent_label: str | None,
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
        exchange_mode: str | None,
        exchange_result: Any | None,
    ) -> "_ActionResult | None":
        action_start = time.monotonic()
        logger.info("Executing action: %s for issue #%d", action.value, issue_number)
        if is_timeline_trace_enabled():
            logger.info(
                "[TIMELINE] completion.action_start issue=%s action=%s requested_actions=%s label_target=%s",
                issue_number,
                action.value,
                ",".join(a.value for a in record.requested_actions),
                label_target,
            )
        try:
            return self._execute_single_action(
                action=action,
                worktree=worktree,
                record=record,
                issue_number=issue_number,
                issue_title=issue_title,
                label_target=label_target,
                branch=branch,
                session_name=session_name,
                agent_label=agent_label,
                actions_taken=actions_taken,
                errors=errors,
                error_details=error_details,
                exchange_mode=exchange_mode,
                exchange_result=exchange_result,
            )
        except Exception as e:
            logger.exception(
                "Exception executing action %s for #%d: %s",
                action.value,
                issue_number,
                e,
            )
            errors.append(f"{action.value}: {e}")
            error_details.append({
                "action": action.value,
                "error": str(e),
                "exception_type": type(e).__name__,
                "traceback": traceback.format_exc(),
            })
            return None
        finally:
            action_duration = time.monotonic() - action_start
            logger.info(
                "Action finished: %s for issue #%d in %.2fs",
                action.value,
                issue_number,
                action_duration,
            )
            if is_timeline_trace_enabled():
                logger.info(
                    "[TIMELINE] completion.action_end issue=%s action=%s elapsed=%.3f actions_taken=%s errors=%s",
                    issue_number,
                    action.value,
                    action_duration,
                    len(actions_taken),
                    len(errors),
                )

    @dataclass
    class _ActionResult:
        """Result of executing a single action."""

        halt: bool = False  # Stop processing remaining actions
        skip_remaining: bool = False  # Skip to next action (used by continue)
        branch: str | None = None  # Updated branch name
        pr_url: str | None = None  # PR URL if created
        review_exchange_completed: bool = False

    def _execute_single_action(
        self,
        *,
        action: RequestedAction,
        worktree: Path,
        record: CompletionRecord,
        issue_number: int,
        issue_title: str,
        label_target: int,
        branch: str | None,
        session_name: str | None,
        agent_label: str | None,
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
        exchange_mode: str | None,
        exchange_result: Any | None,
    ) -> "_ActionResult":
        """Execute a single action and return the result."""
        if action == RequestedAction.PUSH_BRANCH:
            return self._execute_push_action(
                worktree, issue_number, action, actions_taken, errors, error_details
            )
        elif action == RequestedAction.CREATE_PR:
            return self._execute_create_pr_action(
                worktree=worktree,
                record=record,
                issue_number=issue_number,
                issue_title=issue_title,
                branch=branch,
                session_name=session_name,
                agent_label=agent_label,
                actions_taken=actions_taken,
                errors=errors,
                exchange_mode=exchange_mode,
                exchange_result=exchange_result,
            )
        elif action == RequestedAction.POST_COMMENT:
            return self._execute_post_comment_action(
                record=record,
                issue_number=issue_number,
                label_target=label_target,
                actions_taken=actions_taken,
            )
        else:
            label_result = self._execute_label_mutation_action(
                action=action,
                issue_number=issue_number,
                label_target=label_target,
                actions_taken=actions_taken,
            )
            if label_result is not None:
                return label_result

        return self._ActionResult()

    def _execute_post_comment_action(
        self,
        *,
        record: CompletionRecord,
        issue_number: int,
        label_target: int,
        actions_taken: list[str],
    ) -> "_ActionResult":
        """Execute post-comment action with optional review comment event."""
        if not record.comment_body:
            return self._ActionResult()

        comment_url = self.pr_adapter.add_comment(label_target, record.comment_body)
        actions_taken.append(f"Posted comment to #{label_target}")
        # If comment target differs from issue number, this is a PR-scoped review comment.
        if label_target != issue_number:
            self._emit_review_comment_added(
                issue_number=issue_number,
                pr_number=label_target,
                comment_url=comment_url,
                comment_body=record.comment_body,
            )
        return self._ActionResult()

    def _execute_label_mutation_action(
        self,
        *,
        action: RequestedAction,
        issue_number: int,
        label_target: int,
        actions_taken: list[str],
    ) -> "_ActionResult | None":
        """Execute label add/remove action variants."""
        label_actions: dict[RequestedAction, tuple[str, int, str]] = {
            RequestedAction.ADD_BLOCKED_LABEL: ("blocked", issue_number, "add"),
            RequestedAction.ADD_NEEDS_HUMAN_LABEL: ("needs_human", issue_number, "add"),
            RequestedAction.ADD_CODE_REVIEWED_LABEL: ("code_reviewed", label_target, "add"),
            RequestedAction.ADD_NEEDS_REWORK_LABEL: ("needs_rework", label_target, "add"),
            RequestedAction.REMOVE_NEEDS_REWORK_LABEL: ("needs_rework", label_target, "remove"),
            RequestedAction.REMOVE_CODE_REVIEW_LABEL: ("code_review", label_target, "remove"),
        }
        config = label_actions.get(action)
        if config is None:
            return None

        label_key, target_number, operation = config
        label = self._get_label(label_key)
        if is_timeline_trace_enabled():
            logger.info(
                "[TIMELINE] completion.label_mutation issue=%s action=%s operation=%s label_key=%s label=%s target=%s",
                issue_number,
                action.value,
                operation,
                label_key,
                label,
                target_number,
            )
        if operation == "add":
            self.label_adapter.add_label(target_number, label)
            if target_number == issue_number:
                actions_taken.append(f"Added '{label}' label")
            else:
                actions_taken.append(f"Added '{label}' label to #{target_number}")
        else:
            self.label_adapter.remove_label(target_number, label)
            actions_taken.append(f"Removed '{label}' label from #{target_number}")

        return self._ActionResult()

    def _execute_push_action(
        self,
        worktree: Path,
        issue_number: int,
        action: RequestedAction,
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
    ) -> "_ActionResult":
        """Execute push branch action."""
        skip_hooks = os.environ.get("E2E_SKIP_PUSH_HOOKS") == "1"
        result = self.git_adapter.push(worktree, skip_hooks=skip_hooks)
        if result.success:
            actions_taken.append("Pushed branch to remote")
            logger.info("Push succeeded for #%d", issue_number)
            return self._ActionResult()

        # Handle push failure with potential rebase retry
        retry_result: PushResult | None = None
        if self._push_rebase_retry and self._is_non_fast_forward(result.message):
            retry_result = self._attempt_rebase_and_retry_push(
                worktree, issue_number, action, actions_taken, errors, error_details, skip_hooks
            )

        if retry_result and retry_result.success:
            actions_taken.append("Pushed branch to remote after rebase")
            logger.info("Push succeeded after rebase for #%d", issue_number)
            return self._ActionResult()

        # Push failed
        errors.append(f"{ERROR_PREFIX_PUSH}: Push failed: {result.message}")
        error_details.append({
            "action": action.value,
            "error": result.message,
            "retryable": result.retryable,
            "branch": result.branch,
            "remote": result.remote,
        })
        logger.error("Push failed for #%d: %s", issue_number, result.message)
        return self._ActionResult(halt=True)

    def _attempt_rebase_and_retry_push(
        self,
        worktree: Path,
        issue_number: int,
        action: RequestedAction,
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
        skip_hooks: bool,
    ) -> PushResult | None:
        """Attempt to rebase and retry push after non-fast-forward failure."""
        if self.git_adapter.has_uncommitted_changes(worktree):
            logger.warning(
                "Push retry skipped due to uncommitted changes: issue=%s",
                issue_number,
            )
            return None

        rebase_result = self.git_adapter.rebase_on_branch(
            worktree,
            f"origin/{self._base_branch()}",
        )
        if rebase_result.success:
            actions_taken.append("Rebased onto origin/main")
            return self.git_adapter.push(worktree, skip_hooks=skip_hooks)

        errors.append(f"{ERROR_PREFIX_PUSH}: Rebase failed: {rebase_result.message}")
        error_details.append({
            "action": action.value,
            "error": rebase_result.message,
            "stage": "rebase",
            "conflicts": rebase_result.conflicts,
            "aborted": rebase_result.aborted,
        })
        return None

    def _execute_create_pr_action(
        self,
        *,
        worktree: Path,
        record: CompletionRecord,
        issue_number: int,
        issue_title: str,
        branch: str | None,
        session_name: str | None,
        agent_label: str | None,
        actions_taken: list[str],
        errors: list[str],
        exchange_mode: str | None,
        exchange_result: Any | None,
    ) -> "_ActionResult":
        """Execute create PR action."""
        if not branch:
            errors.append(f"{ERROR_PREFIX_CREATE_PR}: Cannot create PR - no branch")
            logger.error("Cannot create PR for #%d: no branch", issue_number)
            return self._ActionResult(skip_remaining=True)

        skip_hooks = os.environ.get("E2E_SKIP_PUSH_HOOKS") == "1"
        pr_title = f"#{issue_number}: {issue_title}"
        pr_body = build_pr_body(record, issue_number)
        exchange_mode, exchange_resolution_failed = self._review_exchange.resolve_create_pr_exchange_mode(
            exchange_mode=exchange_mode,
            agent_label=agent_label,
            errors=errors,
        )
        if exchange_resolution_failed:
            return self._ActionResult(halt=True)
        if self._review_exchange.missing_review_exchange_outcome(exchange_mode, exchange_result):
            errors.append("review_exchange: missing exchange outcome before PR creation")
            return self._ActionResult(halt=True)
        review_run_dir = self._review_exchange.resolve_review_exchange_run_dir(
            exchange_outcome=exchange_result,
            worktree=worktree,
            session_name=session_name,
        )

        # Check for existing PR to reuse after review exchange succeeds.
        reused = self._reuse_existing_pr_if_available(
            issue_number=issue_number,
            branch=branch,
            exchange_mode=exchange_mode,
            exchange_result=exchange_result,
            actions_taken=actions_taken,
            run_dir=review_run_dir,
        )
        if reused is not None:
            return reused

        # Maybe switch branch for PR collision
        if self._pr_collision_strategy == "new_branch":
            branch = maybe_switch_branch_for_pr_collision(
                pr_adapter=self.pr_adapter,
                git_adapter=self.git_adapter,
                worktree=worktree,
                branch=branch,
                issue_number=issue_number,
                actions_taken=actions_taken,
                skip_hooks=skip_hooks,
            )

        # Create the PR
        logger.info("Creating PR for #%d: branch=%s", issue_number, branch)
        draft_pr = exchange_mode not in {"via-mcp", "via-local-loop"}
        pr = create_pr_with_collision_handling(
            pr_adapter=self.pr_adapter,
            git_adapter=self.git_adapter,
            base_branch=self._base_branch,
            pr_collision_strategy=self._pr_collision_strategy,
            worktree=worktree,
            pr_title=pr_title,
            pr_body=pr_body,
            branch=branch,
            issue_number=issue_number,
            actions_taken=actions_taken,
            skip_hooks=skip_hooks,
            draft=draft_pr,
        )

        if pr:
            self._apply_pr_labels(pr, record, actions_taken)
            review_exchange_completed = False
            if exchange_mode in {"via-mcp", "via-local-loop"} and exchange_result:
                review_exchange_completed = True
                self._finalize_review_exchange_pr(
                    issue_number=issue_number,
                    pr_number=pr.number,
                    exchange_mode=exchange_mode,
                    exchange_result=exchange_result,
                    actions_taken=actions_taken,
                    run_dir=review_run_dir,
                )
            return self._ActionResult(
                branch=branch,
                pr_url=pr.url,
                review_exchange_completed=review_exchange_completed,
            )

        return self._ActionResult(branch=branch)

    def _reuse_existing_pr_if_available(
        self,
        *,
        issue_number: int,
        branch: str,
        exchange_mode: str | None,
        exchange_result: Any | None,
        actions_taken: list[str],
        run_dir: Path | None,
    ) -> "_ActionResult | None":
        if self._pr_collision_strategy not in {"reuse_open", "new_branch"}:
            return None
        existing_pr = get_open_pr_for_issue(
            self.pr_adapter,
            issue_number,
            expected_branch=branch,
        )
        if not existing_pr:
            return None
        actions_taken.append(f"Reused PR #{existing_pr.number}")
        logger.info(
            "Reused existing PR #%d for issue #%d: %s",
            existing_pr.number,
            issue_number,
            existing_pr.url,
        )
        review_exchange_completed = False
        if exchange_mode in {"via-mcp", "via-local-loop"} and exchange_result:
            review_exchange_completed = True
            self._finalize_review_exchange_pr(
                issue_number=issue_number,
                pr_number=existing_pr.number,
                exchange_mode=exchange_mode,
                exchange_result=exchange_result,
                actions_taken=actions_taken,
                run_dir=run_dir,
            )
        return self._ActionResult(
            pr_url=existing_pr.url,
            skip_remaining=True,
            review_exchange_completed=review_exchange_completed,
        )

    def _finalize_review_exchange_pr(
        self,
        *,
        issue_number: int,
        pr_number: int,
        exchange_mode: str,
        exchange_result: Any,
        actions_taken: list[str],
        run_dir: Path | None = None,
    ) -> None:
        """Apply review-exchange completion labels/comment to a PR."""
        label = self._get_label("code_reviewed")
        self.label_adapter.add_label(pr_number, label)
        actions_taken.append(f"Added '{label}' label to PR #{pr_number}")
        review_label = self._get_label("code_review")
        self.label_adapter.remove_label(pr_number, review_label)
        actions_taken.append(f"Removed '{review_label}' label from PR #{pr_number}")
        comment = (
            f"✅ Review completed via {exchange_mode} loop.\n\n"
            f"- Rounds: {exchange_result.rounds}\n"
            f"- Outcome: {exchange_result.reason}\n"
        )
        if self._config and self._config.review_exchange_require_validation:
            comment += "- Validation: required and passed\n"
        comment_url = self.pr_adapter.add_comment(pr_number, comment)
        actions_taken.append(f"Posted review completion comment to PR #{pr_number}")
        self._emit_review_comment_added(
            issue_number=issue_number,
            pr_number=pr_number,
            comment_url=comment_url,
            comment_body=comment,
            run_dir=run_dir,
        )

    def _resolve_review_exchange_mode(self, agent_label: str | None) -> str | None:
        return self._review_exchange.resolve_review_exchange_mode(agent_label)

    def _run_review_exchange_loop(
        self,
        *,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        agent_label: str | None,
        initial_validation_record_path: Path | None = None,
        on_started: Callable[[Path], None] | None = None,
    ) -> Any:
        return self._review_exchange.run_review_exchange_loop(
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            agent_label=agent_label,
            initial_validation_record_path=initial_validation_record_path,
            on_started=on_started,
            events=self._trace_events,
            event_context=self._event_context,
        )

    def _apply_pr_labels(
        self,
        pr: PRInfo,
        record: CompletionRecord,
        actions_taken: list[str],
    ) -> None:
        """Apply extra labels to PR if specified."""
        actions_taken.append(f"Created PR #{pr.number}")
        logger.info("Created PR #%d: %s", pr.number, pr.url)

        # Skip for fake/dry-run PRs (numbers 90000-99999)
        is_dry_run_pr = 90000 <= pr.number <= 99999
        if record.pr_labels and not is_dry_run_pr:
            for label in record.pr_labels:
                self.label_adapter.add_label(pr.number, label)
                logger.info("Added label '%s' to PR #%d", label, pr.number)
            actions_taken.append(f"Added labels to PR: {record.pr_labels}")
        elif record.pr_labels and is_dry_run_pr:
            logger.info("[E2E_DRY_RUN] Skipping PR label addition for fake PR #%d", pr.number)

    def _cleanup_completion_record(
        self,
        worktree: Path,
        completion_path: str | None,
        issue_number: int,
    ) -> None:
        cleanup_completion_record(
            worktree=worktree,
            completion_path=completion_path,
            issue_number=issue_number,
            cleanup_record=self.cleanup_record,
            post_issue_comment=self._add_issue_comment,
        )

    def _is_non_fast_forward(self, message: str) -> bool:
        lower = message.lower()
        return any(
            marker in lower
            for marker in (
                "non-fast-forward",
                "fetch first",
                "rejected",
                "stale info",
            )
        )

    def cleanup_record(self, worktree: Path, completion_path: str | None = None) -> bool:
        """Remove the completion record after processing.

        Args:
            worktree: Path to the worktree.
            completion_path: Agent-specific path to completion.json (optional).

        Returns:
            True if successfully removed, False otherwise.
        """
        record_path = worktree / (completion_path or COMPLETION_RECORD_PATH)
        try:
            if record_path.exists():
                record_path.unlink()
                logger.debug(f"Removed completion record: {record_path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to remove completion record: {e}")
            return False
