"""Completion processor - handles agent completion records.

This controller reads CompletionRecords written by agent-done and executes
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
import re
import logging
import os
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING, runtime_checkable

from ..domain.models import (
    CompletionRecord,
    RequestedAction,
    COMPLETION_RECORD_PATH,
    sanitize_agent_label,
)
from ..domain.events import EventBus, SessionEvent
from ..infra.issue_diagnostics import write_issue_diagnostic
from ..ports.session_output import SessionOutput, ValidationRecord
from .validation import PublishGate, ValidationRecordStore

logger = logging.getLogger(__name__)

# Error prefixes for critical failures (used by completion_handler to detect blocking errors)
# Keep in sync with completion_handler.py's critical error detection
ERROR_PREFIX_PUSH = "push_branch"
ERROR_PREFIX_CREATE_PR = "create_pr"

if TYPE_CHECKING:
    from ..infra.config import Config


@runtime_checkable
class LabelAdapter(Protocol):
    """Protocol for label operations."""

    def add_label(self, issue_number: int, label: str) -> None: ...
    def remove_label(self, issue_number: int, label: str) -> None: ...


from ..ports.pull_request_tracker import PRInfo
from ..ports.working_copy import PushResult, RebaseResult


@runtime_checkable
class PRAdapter(Protocol):
    """Protocol for PR operations."""

    def create_pr(
        self, title: str, body: str, head: str, base: str = "main", draft: bool | None = None
    ) -> PRInfo: ...
    def add_comment(self, issue_or_pr_number: int, body: str) -> str: ...
    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]: ...
    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]: ...


@runtime_checkable
class GitAdapter(Protocol):
    """Protocol for git operations."""

    def push(
        self,
        worktree: Path,
        remote: str = "origin",
        set_upstream: bool = True,
        skip_hooks: bool = False,
    ) -> PushResult: ...

    def rebase_on_branch(self, worktree: Path, target: str = "origin/main") -> RebaseResult: ...
    def create_branch_from_current(self, worktree: Path, branch: str) -> None: ...
    def list_branch_names(self, worktree: Path) -> list[str]: ...
    def get_current_branch(self, worktree: Path) -> str | None: ...
    def has_uncommitted_changes(self, worktree: Path) -> bool: ...


@dataclass
class ProcessingResult:
    """Result of processing a completion record."""

    success: bool
    message: str
    pr_url: str | None = None
    actions_taken: list[str] | None = None
    errors: list[str] | None = None
    diagnostic_path: str | None = None  # Path to detailed failure diagnostics
    review_exchange_completed: bool = False


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

    def read_completion_record(
        self, worktree: Path, completion_path: str | None = None
    ) -> CompletionRecord | None:
        """Read and validate a completion record from a worktree.

        Args:
            worktree: Path to the worktree directory.
            completion_path: Relative path to completion file. If None, uses legacy path.

        Returns:
            The validated CompletionRecord, or None if not found/invalid.
        """
        record_path = worktree / (completion_path or COMPLETION_RECORD_PATH)

        if not record_path.exists():
            logger.info("No completion record found at %s", record_path)
            return None

        try:
            with open(record_path) as f:
                data = json.load(f)
            record = CompletionRecord.from_dict(data)
            logger.info(
                "Read completion record: outcome=%s session=%s path=%s",
                record.outcome.value,
                record.session_id,
                record_path,
            )
            return record
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in completion record: {e}")
            return None
        except ValueError as e:
            logger.error(f"Invalid completion record: {e}")
            return None

    def _resolve_agent_label_from_completion_path(
        self, completion_path: str | None
    ) -> tuple[str | None, str | None]:
        if completion_path is None or self._config is None:
            return None, None
        filename = Path(completion_path).name
        if not (filename.startswith("completion-") and filename.endswith(".json")):
            return None, None
        safe_name = filename[len("completion-"):-len(".json")]
        matches = [
            label
            for label in self._config.agents.keys()
            if sanitize_agent_label(label) == safe_name
        ]
        if not matches:
            return None, None
        if len(matches) > 1:
            return (
                None,
                "Multiple agent labels map to completion file "
                f"{filename}: {', '.join(matches)}",
            )
        return matches[0], None

    def validate_worktree_state(
        self, worktree: Path, record: CompletionRecord
    ) -> tuple[bool, str]:
        """Validate worktree state before executing actions.

        This is a policy check - even if the agent requested actions,
        we verify the worktree is in a valid state.

        Args:
            worktree: Path to the worktree.
            record: The completion record to validate against.

        Returns:
            Tuple of (is_valid, reason_if_invalid).
        """
        # Get current branch
        branch = self.git_adapter.get_current_branch(worktree)
        if not branch:
            return False, "Could not determine current branch"

        # For push operations, verify we're not on main
        if RequestedAction.PUSH_BRANCH in record.requested_actions:
            if branch in ("main", "master"):
                return False, f"Cannot push: on protected branch '{branch}'"

        # Check for uncommitted changes if push is requested
        if RequestedAction.PUSH_BRANCH in record.requested_actions:
            if self.git_adapter.has_uncommitted_changes(worktree):
                logger.warning(
                    f"Worktree has uncommitted changes, will push anyway"
                )
                # This is a warning, not a failure - agent may have left uncommitted changes

        return True, ""

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
        if record_path is not None:
            self.session_output.update_manifest(
                run_dir,
                {"validation_record_path": str(record_path)},
            )
            try:
                (run_dir / "validation-record.path").write_text(str(record_path))
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
            return ProcessingResult(
                success=False,
                message=f"Validation failed: {reason}",
                errors=[reason],
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
                self._write_reviewer_feedback_file(run_dir, pr_number, record.review_issues)

        # Build and return result
        total_duration = time.monotonic() - start_time
        return self._build_processing_result(
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
            return ProcessingResult(
                success=False,
                message="Publish gate requires session output but no session name available",
                errors=["session_name is required for publish gate"],
            )
        session_output_dir = self.session_output.find_run_dir(worktree, session_name)
        if session_output_dir is None:
            return ProcessingResult(
                success=False,
                message=f"Publish gate requires session output but run dir not found for {session_name}",
                errors=[f"Session output directory not found for {session_name}"],
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
        halt_actions = False
        review_exchange_completed = False

        for action in record.requested_actions:
            action_start = time.monotonic()
            logger.info("Executing action: %s for issue #%d", action.value, issue_number)
            try:
                result = self._execute_single_action(
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
                )
                if result.halt:
                    halt_actions = True
                if result.branch:
                    branch = result.branch
                if result.pr_url:
                    pr_url = result.pr_url
                if result.review_exchange_completed:
                    review_exchange_completed = True
                if result.skip_remaining:
                    continue

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
            finally:
                action_duration = time.monotonic() - action_start
                logger.info(
                    "Action finished: %s for issue #%d in %.2fs",
                    action.value,
                    issue_number,
                    action_duration,
                )
            if halt_actions:
                logger.warning(
                    "Halting remaining actions for issue #%d due to push failure",
                    issue_number,
                )
                break

        return branch, pr_url, review_exchange_completed

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
            )
        elif action == RequestedAction.POST_COMMENT:
            if record.comment_body:
                self.pr_adapter.add_comment(label_target, record.comment_body)
                actions_taken.append(f"Posted comment to #{label_target}")
        elif action == RequestedAction.ADD_BLOCKED_LABEL:
            label = self._get_label("blocked")
            self.label_adapter.add_label(issue_number, label)
            actions_taken.append(f"Added '{label}' label")
        elif action == RequestedAction.ADD_NEEDS_HUMAN_LABEL:
            label = self._get_label("needs_human")
            self.label_adapter.add_label(issue_number, label)
            actions_taken.append(f"Added '{label}' label")
        elif action == RequestedAction.ADD_CODE_REVIEWED_LABEL:
            label = self._get_label("code_reviewed")
            self.label_adapter.add_label(label_target, label)
            actions_taken.append(f"Added '{label}' label to #{label_target}")
        elif action == RequestedAction.ADD_NEEDS_REWORK_LABEL:
            label = self._get_label("needs_rework")
            self.label_adapter.add_label(label_target, label)
            actions_taken.append(f"Added '{label}' label to #{label_target}")
        elif action == RequestedAction.REMOVE_CODE_REVIEW_LABEL:
            label = self._get_label("code_review")
            self.label_adapter.remove_label(label_target, label)
            actions_taken.append(f"Removed '{label}' label from #{label_target}")

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

        rebase_result = self.git_adapter.rebase_on_branch(worktree, "origin/main")
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
    ) -> "_ActionResult":
        """Execute create PR action."""
        if not branch:
            errors.append(f"{ERROR_PREFIX_CREATE_PR}: Cannot create PR - no branch")
            logger.error("Cannot create PR for #%d: no branch", issue_number)
            return self._ActionResult(skip_remaining=True)

        skip_hooks = os.environ.get("E2E_SKIP_PUSH_HOOKS") == "1"
        pr_title = f"#{issue_number}: {issue_title}"
        pr_body = self._build_pr_body(record, issue_number)

        # Check for existing PR to reuse
        if self._pr_collision_strategy in {"reuse_open", "new_branch"}:
            existing_pr = self._get_open_pr_for_issue(issue_number)
            if existing_pr:
                actions_taken.append(f"Reused PR #{existing_pr.number}")
                logger.info(
                    "Reused existing PR #%d for issue #%d: %s",
                    existing_pr.number,
                    issue_number,
                    existing_pr.url,
                )
                return self._ActionResult(pr_url=existing_pr.url, skip_remaining=True)

        # Maybe switch branch for PR collision
        if self._pr_collision_strategy == "new_branch":
            branch = self._maybe_switch_branch_for_pr_collision(
                worktree=worktree,
                branch=branch,
                issue_number=issue_number,
                actions_taken=actions_taken,
                skip_hooks=skip_hooks,
            )

        exchange_mode, exchange_result, exchange_halt = self._run_review_exchange_if_needed(
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            agent_label=agent_label,
            errors=errors,
            actions_taken=actions_taken,
        )
        if exchange_halt:
            return self._ActionResult(halt=True)

        # Create the PR
        logger.info("Creating PR for #%d: branch=%s", issue_number, branch)
        draft_pr = exchange_mode not in {"via-mcp", "via-local-loop"}
        pr = self._create_pr_with_collision_handling(
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
                label = self._get_label("code_reviewed")
                self.label_adapter.add_label(pr.number, label)
                actions_taken.append(f"Added '{label}' label to PR #{pr.number}")
                review_label = self._get_label("code_review")
                self.label_adapter.remove_label(pr.number, review_label)
                actions_taken.append(f"Removed '{review_label}' label from PR #{pr.number}")
                comment = (
                    f"✅ Review completed via {exchange_mode} loop.\n\n"
                    f"- Rounds: {exchange_result.rounds}\n"
                    f"- Outcome: {exchange_result.reason}\n"
                )
                if self._config and self._config.review_exchange_require_validation:
                    comment += "- Validation: required and passed\n"
                self.pr_adapter.add_comment(pr.number, comment)
                actions_taken.append(f"Posted review completion comment to PR #{pr.number}")
            return self._ActionResult(
                branch=branch,
                pr_url=pr.url,
                review_exchange_completed=review_exchange_completed,
            )

        return self._ActionResult(branch=branch)

    def _run_review_exchange_if_needed(
        self,
        *,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        agent_label: str | None,
        errors: list[str],
        actions_taken: list[str],
    ) -> tuple[str | None, Any | None, bool]:
        try:
            exchange_mode = self._resolve_review_exchange_mode(agent_label)
        except ValueError as exc:
            errors.append(f"review_exchange: {exc}")
            return None, None, True
        if exchange_mode not in {"via-mcp", "via-local-loop"}:
            return exchange_mode, None, False
        logger.info("Review exchange mode selected: %s", exchange_mode)
        exchange_result = self._run_review_exchange_loop(
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            agent_label=agent_label,
        )
        if exchange_result.status != "ok":
            errors.append(
                f"review_exchange: {exchange_result.status} ({exchange_result.reason})"
            )
            return exchange_mode, exchange_result, True
        actions_taken.append("Review exchange passed")
        return exchange_mode, exchange_result, False

    def _resolve_review_exchange_mode(self, agent_label: str | None) -> str | None:
        if not self._config:
            return None
        if not self._config.review_enabled:
            return None
        mode = self._config.review_exchange_mode
        if mode in {"via-mcp", "via-local-loop"}:
            agent_label = self._require_review_exchange_agent_label(agent_label, mode)
            if mode == "via-mcp":
                from ..infra.review_exchange_registry import supports_mcp_pair

                coder_system, reviewer_system = self._resolve_exchange_systems(agent_label)
                if not supports_mcp_pair(coder_system, reviewer_system):
                    raise ValueError(
                        "Review exchange via-mcp requires a supported ai_system pair: "
                        f"{agent_label} ({coder_system}->{reviewer_system})"
                    )
            return mode
        if mode != "auto":
            return None
        if not agent_label:
            logger.warning(
                "Review exchange auto mode requires agent label; falling back to draft PR."
            )
            return None
        agent_label = self._require_review_exchange_agent_label(agent_label, "auto")
        from ..infra.review_exchange_registry import supports_mcp_pair

        coder_system, reviewer_system = self._resolve_exchange_systems(agent_label)
        if supports_mcp_pair(coder_system, reviewer_system):
            return "via-mcp"
        return "via-local-loop"

    def _require_review_exchange_agent_label(
        self, agent_label: str | None, mode: str
    ) -> str:
        if not agent_label:
            raise ValueError(f"Review exchange requires agent_label for {mode} mode")
        return agent_label

    def _resolve_reviewer_label(self, agent_label: str) -> str:
        if not self._config:
            raise ValueError("Review exchange requires config")
        if agent_label not in self._config.agents:
            raise ValueError(f"Review exchange agent '{agent_label}' not found in config.agents")
        reviewer_label = self._config.get_reviewer_for_agent(agent_label)
        if not reviewer_label:
            raise ValueError("Review exchange requires review.default or per-agent reviewer")
        if reviewer_label not in self._config.agents:
            raise ValueError(f"Review exchange reviewer '{reviewer_label}' not found in config.agents")
        return reviewer_label

    def _resolve_exchange_systems(self, agent_label: str) -> tuple[str, str]:
        if not self._config:
            raise ValueError("Review exchange requires config")
        reviewer_label = self._resolve_reviewer_label(agent_label)
        coder_system = self._config.agents[agent_label].ai_system
        reviewer_system = self._config.agents[reviewer_label].ai_system
        if not coder_system or not reviewer_system:
            raise ValueError("Review exchange requires ai_system on coder and reviewer agents")
        return coder_system, reviewer_system

    def _run_review_exchange_loop(
        self,
        *,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        agent_label: str | None,
    ):
        if not self._config:
            raise ValueError("Review exchange requires config")
        if not agent_label:
            raise ValueError("Review exchange requires agent_label")
        coder_label = agent_label
        reviewer_label = self._resolve_reviewer_label(agent_label)
        coder_agent = self._config.agents[coder_label]
        reviewer_agent = self._config.agents[reviewer_label]
        max_rounds = self._config.review_exchange_max_rounds
        max_no_progress = self._config.review_exchange_max_no_progress
        require_validation = self._config.review_exchange_require_validation
        web_port = self._config.web_port

        from .review_exchange_loop import run_review_exchange_loop

        return run_review_exchange_loop(
            session_output=self.session_output,
            worktree_path=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            coder_label=coder_label,
            reviewer_label=reviewer_label,
            coder_agent=coder_agent,
            reviewer_agent=reviewer_agent,
            max_rounds=max_rounds,
            max_no_progress=max_no_progress,
            require_validation=require_validation,
            web_port=web_port,
        )

    def _create_pr_with_collision_handling(
        self,
        *,
        worktree: Path,
        pr_title: str,
        pr_body: str,
        branch: str,
        issue_number: int,
        actions_taken: list[str],
        skip_hooks: bool,
        draft: bool,
    ) -> PRInfo | None:
        """Create PR with collision handling."""
        try:
            return self.pr_adapter.create_pr(
                title=pr_title,
                body=pr_body,
                head=branch,
                base="main",
                draft=draft,
            )
        except Exception as e:
            if self._pr_collision_strategy == "new_branch" and self._is_pr_collision_error(e):
                new_branch = self._switch_to_suffixed_branch(
                    worktree=worktree,
                    branch=branch,
                    issue_number=issue_number,
                    actions_taken=actions_taken,
                    skip_hooks=skip_hooks,
                )
                return self.pr_adapter.create_pr(
                    title=pr_title,
                    body=pr_body,
                    head=new_branch,
                    base="main",
                    draft=draft,
                )
            elif self._is_no_commits_error(e):
                raise RuntimeError(
                    f"Cannot create PR: no commits between main and {branch}. "
                    f"Possible causes: (1) agent didn't make any changes, "
                    f"(2) work already merged via another PR, "
                    f"(3) commits lost during rebase. "
                    f"Human review required."
                )
            else:
                raise

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

    def _build_processing_result(
        self,
        *,
        worktree: Path,
        record: CompletionRecord,
        session_name: str | None,
        issue_number: int,
        issue_title: str,
        branch: str | None,
        pr_url: str | None,
        review_exchange_completed: bool,
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
        total_duration: float,
        completion_path: str | None,
    ) -> ProcessingResult:
        """Build final processing result and handle cleanup."""
        # Determine overall success
        success = len(errors) == 0 or (
            # Partial success if we at least completed the main work
            RequestedAction.PUSH_BRANCH in record.requested_actions
            and "Pushed branch to remote" in actions_taken
        )
        logger.info(
            "Completion result: issue=%s success=%s actions=%s errors=%s pr_url=%s",
            issue_number,
            success,
            actions_taken,
            errors,
            pr_url,
        )
        logger.info(
            "Completion processing duration: issue=%s elapsed=%.2fs",
            issue_number,
            total_duration,
        )

        # Build result message and emit events
        diagnostic_path: str | None = None
        if success:
            message = f"Processed {record.outcome.value}: {', '.join(actions_taken)}"
            self._emit(
                SessionEvent.COMPLETED,
                issue_number,
                {
                    "outcome": record.outcome.value,
                    "actions_taken": actions_taken,
                    "pr_url": pr_url,
                },
            )
        else:
            message = f"Processing failed: {'; '.join(errors)}"
            self._emit(
                SessionEvent.FAILED,
                issue_number,
                {
                    "outcome": record.outcome.value,
                    "actions_taken": actions_taken,
                    "errors": errors,
                },
            )
            # Write detailed failure diagnostics to worktree
            diagnostic_path = self._write_failure_diagnostic(
                worktree=worktree,
                session_name=session_name,
                issue_number=issue_number,
                issue_title=issue_title,
                branch=branch,
                outcome=record.outcome.value,
                requested_actions=[a.value for a in record.requested_actions],
                actions_taken=actions_taken,
                errors=errors,
                error_details=error_details,
                duration_seconds=total_duration,
            )

        # Clean up the completion record after processing to prevent re-processing
        self._cleanup_completion_record(worktree, completion_path, issue_number)

        return ProcessingResult(
            success=success,
            message=message,
            pr_url=pr_url,
            actions_taken=actions_taken if actions_taken else None,
            diagnostic_path=diagnostic_path,
            errors=errors if errors else None,
            review_exchange_completed=review_exchange_completed,
        )

    def _cleanup_completion_record(
        self,
        worktree: Path,
        completion_path: str | None,
        issue_number: int,
    ) -> None:
        """Clean up the completion record after processing."""
        record_path = worktree / (completion_path or COMPLETION_RECORD_PATH)
        existed_before = record_path.exists()
        cleanup_ok = self.cleanup_record(worktree, completion_path)
        exists_after = record_path.exists()
        logger.warning("CLEANUP: issue=%d path=%s existed_before=%s exists_after=%s",
                      issue_number, record_path, existed_before, exists_after)
        if existed_before and exists_after and not cleanup_ok:
            self._report_cleanup_failure(issue_number, worktree, record_path)

    def _build_pr_body(self, record: CompletionRecord, issue_number: int) -> str:
        """Build the PR body from the completion record.

        Args:
            record: The completion record.
            issue_number: The issue number to link.

        Returns:
            The formatted PR body.
        """
        parts = [
            f"Closes #{issue_number}",
            "",
        ]

        if record.implementation:
            parts.extend([
                "## Implementation",
                record.implementation,
                "",
            ])

        if record.problems:
            parts.extend([
                "## Problems Encountered",
                record.problems,
                "",
            ])

        parts.extend([
            "---",
            "*Generated by issue-orchestrator*",
        ])

        return "\n".join(parts)

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

    def _is_pr_collision_error(self, error: Exception) -> bool:
        message = str(error).lower()
        return "pull request" in message and "already exists" in message

    def _is_no_commits_error(self, error: Exception) -> bool:
        """Detect GitHub 422 'No commits between main and branch' error.

        This happens when the branch is identical to main, meaning the work
        was already merged (possibly from a previous session or other PR).
        """
        message = str(error).lower()
        return "no commits between" in message

    def _pr_matches_issue(self, pr: PRInfo, issue_number: int) -> bool:
        if pr.branch and pr.branch.startswith(f"{issue_number}-"):
            return True
        if pr.title and f"#{issue_number}" in pr.title:
            return True
        return False

    def _get_open_pr_for_issue(self, issue_number: int) -> PRInfo | None:
        try:
            prs = self.pr_adapter.get_prs_for_issue(issue_number, state="open")
        except Exception as e:
            logger.warning("Failed to query open PRs for issue %s: %s", issue_number, e)
            return None
        for pr in prs:
            if self._pr_matches_issue(pr, issue_number):
                return pr
        return None

    def _maybe_switch_branch_for_pr_collision(
        self,
        *,
        worktree: Path,
        branch: str,
        issue_number: int,
        actions_taken: list[str],
        skip_hooks: bool,
    ) -> str:
        try:
            prs = self.pr_adapter.get_prs_for_branch(branch, state="all")
        except Exception as e:
            logger.warning("Failed to query PRs for branch %s: %s", branch, e)
            return branch
        if not prs:
            return branch
        for pr in prs:
            if pr.state.lower() == "open" and self._pr_matches_issue(pr, issue_number):
                return branch
        return self._switch_to_suffixed_branch(
            worktree=worktree,
            branch=branch,
            issue_number=issue_number,
            actions_taken=actions_taken,
            skip_hooks=skip_hooks,
        )

    def _next_branch_name(self, worktree: Path, branch: str) -> str:
        base = re.sub(r"-r\d+$", "", branch)
        existing = self.git_adapter.list_branch_names(worktree)
        pattern = re.compile(rf"^{re.escape(base)}-r(\d+)$")
        max_suffix = 0
        for name in existing:
            match = pattern.match(name)
            if match:
                max_suffix = max(max_suffix, int(match.group(1)))
        return f"{base}-r{max_suffix + 1}"

    def _switch_to_suffixed_branch(
        self,
        *,
        worktree: Path,
        branch: str,
        issue_number: int,
        actions_taken: list[str],
        skip_hooks: bool,
    ) -> str:
        new_branch = self._next_branch_name(worktree, branch)
        self.git_adapter.create_branch_from_current(worktree, new_branch)
        push_result = self.git_adapter.push(worktree, skip_hooks=skip_hooks)
        if not push_result.success:
            raise RuntimeError(f"Failed to push new branch {new_branch}: {push_result.message}")
        actions_taken.append(f"Switched to new branch {new_branch}")
        logger.info(
            "PR collision remediation for issue #%d: branch=%s -> %s",
            issue_number,
            branch,
            new_branch,
        )
        return new_branch

    def _write_reviewer_feedback_file(
        self,
        run_dir: Path,
        pr_number: int,
        review_issues: str,
    ) -> Path | None:
        """Write reviewer feedback to the review session's run directory.

        This enables the "local cache" pattern: when a rework session starts shortly
        after a review completes, it can read the feedback from the review session's
        run directory instead of fetching from GitHub (which may have eventual
        consistency delays).

        The file is written to: {run_dir}/reviewer-feedback.json

        Args:
            run_dir: Path to the review session's run directory.
            pr_number: The PR number being reviewed.
            review_issues: The reviewer's feedback text (from agent-done --issues).

        Returns:
            Path to the written file, or None if writing failed.
        """
        feedback_file = run_dir / "reviewer-feedback.json"

        feedback_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pr_number": pr_number,
            "review_issues": review_issues,
        }

        try:
            feedback_file.write_text(json.dumps(feedback_data, indent=2))
            logger.info(
                "[REVIEW_FEEDBACK] Wrote reviewer feedback for PR #%d: %s",
                pr_number,
                feedback_file,
            )
            return feedback_file
        except Exception as e:
            logger.warning(
                "[REVIEW_FEEDBACK] Failed to write feedback file for PR #%d: %s",
                pr_number,
                e,
            )
            return None

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

    def _report_cleanup_failure(
        self,
        issue_number: int,
        worktree: Path,
        record_path: Path,
    ) -> None:
        """Report cleanup failure with a local diagnostic reference."""
        diagnostic = write_issue_diagnostic(
            worktree=worktree,
            issue_number=issue_number,
            kind="completion-cleanup",
            summary="Completion record could not be deleted",
            details={
                "record_path": str(record_path),
                "worktree": str(worktree),
            },
        )

        if diagnostic:
            comment = (
                "WARNING: Cleanup incomplete\n\n"
                "The completion record could not be deleted after processing. "
                "This can happen if the file is still open or locked.\n\n"
                f"- Worktree: `{diagnostic.worktree_name}`\n"
                f"- Diagnostic file: `{diagnostic.relative_path}`\n\n"
                "Close any editors or processes using the file, then delete it manually."
            )
        else:
            comment = (
                "WARNING: Cleanup incomplete\n\n"
                "The completion record could not be deleted after processing. "
                "Close any editors or processes using the file, then delete it manually."
            )

        try:
            self.pr_adapter.add_comment(issue_number, comment)
        except Exception as exc:
            logger.warning("Failed to add cleanup warning comment for #%d: %s", issue_number, exc)

    def _write_failure_diagnostic(
        self,
        worktree: Path,
        session_name: str | None,
        issue_number: int,
        issue_title: str,
        branch: str | None,
        outcome: str,
        requested_actions: list[str],
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
        duration_seconds: float,
    ) -> str | None:
        """Write detailed failure diagnostics to a file in the worktree.

        This captures stack traces and detailed error context that would be
        inappropriate to post publicly on GitHub.

        Args:
            worktree: Path to the worktree.
            issue_number: The issue number.
            issue_title: The issue title.
            branch: Branch name (if known).
            outcome: The agent's reported outcome.
            requested_actions: Actions requested by the agent.
            actions_taken: Actions that succeeded.
            errors: Summary error messages.
            error_details: Full error details including stack traces.
            duration_seconds: How long processing took.

        Returns:
            Relative path to the diagnostic file, or None if writing failed.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"failure-diagnostic-{timestamp}.json"
        if session_name:
            run_dir = self.session_output.find_run_dir(worktree, session_name=session_name)
            if run_dir:
                diagnostic_dir = run_dir
                diagnostic_rel = f".issue-orchestrator/sessions/{run_dir.name}/{filename}"
            else:
                diagnostic_dir = self.session_output.ensure_run_dir(worktree, session_name)
                diagnostic_rel = f".issue-orchestrator/sessions/{diagnostic_dir.name}/{filename}"
        else:
            diagnostic_dir = worktree / ".issue-orchestrator"
            diagnostic_rel = f".issue-orchestrator/{filename}"
        diagnostic_path = diagnostic_dir / filename

        diagnostic = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_name": session_name,
            "issue_number": issue_number,
            "issue_title": issue_title,
            "branch": branch,
            "worktree": str(worktree),
            "outcome_reported": outcome,
            "requested_actions": requested_actions,
            "actions_taken": actions_taken,
            "errors": errors,
            "error_details": error_details,
            "duration_seconds": round(duration_seconds, 2),
        }

        try:
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
            diagnostic_path.write_text(json.dumps(diagnostic, indent=2))
            if session_name:
                run_dir = self.session_output.find_run_dir(worktree, session_name=session_name)
                if run_dir:
                    self.session_output.update_manifest(
                        run_dir,
                        {"diagnostic_path": diagnostic_rel},
                    )
            logger.info(
                "[DIAGNOSTIC] Wrote failure diagnostic: issue=%d path=%s",
                issue_number, diagnostic_path,
            )
            # Return relative path for inclusion in GitHub comment
            return diagnostic_rel
        except Exception as e:
            logger.warning(
                "[DIAGNOSTIC] Failed to write failure diagnostic: issue=%d error=%s",
                issue_number, e,
            )
            return None
