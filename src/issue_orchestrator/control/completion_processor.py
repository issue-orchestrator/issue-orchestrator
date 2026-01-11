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
import logging
import os
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..domain.models import (
    CompletionRecord,
    RequestedAction,
    COMPLETION_RECORD_PATH,
)
from ..domain.events import EventBus, SessionEvent
from ..infra.issue_diagnostics import write_issue_diagnostic
from .validation import PublishGate

logger = logging.getLogger(__name__)


@runtime_checkable
class LabelAdapter(Protocol):
    """Protocol for label operations."""

    def add_label(self, issue_number: int, label: str) -> None: ...
    def remove_label(self, issue_number: int, label: str) -> None: ...


from ..ports.pull_request_tracker import PRInfo
from ..ports.working_copy import PushResult


@runtime_checkable
class PRAdapter(Protocol):
    """Protocol for PR operations."""

    def create_pr(
        self, title: str, body: str, head: str, base: str = "main"
    ) -> PRInfo: ...
    def add_comment(self, issue_or_pr_number: int, body: str) -> str: ...


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
        event_bus: EventBus | None = None,
        label_config: dict[str, str] | None = None,
        publish_gate: PublishGate | None = None,
    ):
        """Initialize the processor with required adapters.

        Args:
            label_adapter: Adapter for label operations (add/remove labels).
            pr_adapter: Adapter for PR operations (create PR, add comment).
            git_adapter: Adapter for git operations (push).
            event_bus: Optional EventBus for emitting processing events.
            label_config: Optional mapping of label names (e.g., {"blocked": "blocked"}).
            publish_gate: Optional PublishGate for validating before publish actions.
        """
        self.label_adapter = label_adapter
        self.pr_adapter = pr_adapter
        self.git_adapter = git_adapter
        self.event_bus = event_bus
        self.label_config = label_config or {}
        self.publish_gate = publish_gate

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

    def _check_publish_gate(self, worktree: Path) -> tuple[bool, str]:
        """Check if publishing is allowed by the publish gate.

        Args:
            worktree: Path to the worktree.

        Returns:
            Tuple of (allowed, reason).
        """
        if self.publish_gate is None:
            # No gate configured = allowed
            return True, ""

        result = self.publish_gate.check()
        if result.allowed:
            cache_note = " (cached)" if result.cache_hit else ""
            logger.info("Publish gate passed%s: %s", cache_note, result.reason)
            return True, result.reason
        else:
            logger.warning("Publish gate failed: %s", result.reason)
            return False, result.reason

    def process(
        self, worktree: Path, issue_number: int, issue_title: str,
        pr_number: int | None = None,
        completion_path: str | None = None,
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
        branch: str | None = None

        # Read the completion record
        record = self.read_completion_record(worktree, completion_path)
        if not record:
            return ProcessingResult(
                success=False,
                message="No completion record found",
                errors=["Completion record not found or invalid"],
            )

        # Validate worktree state
        valid, reason = self.validate_worktree_state(worktree, record)
        if not valid:
            return ProcessingResult(
                success=False,
                message=f"Validation failed: {reason}",
                errors=[reason],
            )

        # Check publish gate if actions require it
        if self._requires_publish_gate(record):
            gate_passed, gate_reason = self._check_publish_gate(worktree)
            if not gate_passed:
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

        # Execute requested actions in order
        for action in record.requested_actions:
            action_start = time.monotonic()
            logger.info("Executing action: %s for issue #%d", action.value, issue_number)
            try:
                if action == RequestedAction.PUSH_BRANCH:
                    # E2E tests can skip pre-push hooks since test scripts create trivial changes
                    skip_hooks = os.environ.get("E2E_SKIP_PUSH_HOOKS") == "1"
                    result = self.git_adapter.push(worktree, skip_hooks=skip_hooks)
                    if result.success:
                        actions_taken.append(f"Pushed branch to remote")
                        logger.info("Push succeeded for #%d", issue_number)
                    else:
                        errors.append(f"Push failed: {result.message}")
                        error_details.append({
                            "action": action.value,
                            "error": result.message,
                            "retryable": result.retryable,
                            "branch": result.branch,
                            "remote": result.remote,
                        })
                        logger.error("Push failed for #%d: %s", issue_number, result.message)

                elif action == RequestedAction.CREATE_PR:
                    if not branch:
                        errors.append("Cannot create PR: no branch")
                        logger.error("Cannot create PR for #%d: no branch", issue_number)
                        continue

                    # Build PR title and body
                    pr_title = f"#{issue_number}: {issue_title}"
                    pr_body = self._build_pr_body(record, issue_number)

                    logger.info("Creating PR for #%d: branch=%s", issue_number, branch)
                    pr = self.pr_adapter.create_pr(
                        title=pr_title,
                        body=pr_body,
                        head=branch,
                        base="main",
                    )
                    pr_url = pr.url
                    actions_taken.append(f"Created PR #{pr.number}")
                    logger.info("Created PR #%d for issue #%d: %s", pr.number, issue_number, pr_url)

                    # Apply extra labels to the PR if specified
                    # Skip for fake/dry-run PRs (numbers 90000-99999)
                    is_dry_run_pr = 90000 <= pr.number <= 99999
                    if record.pr_labels and not is_dry_run_pr:
                        for label in record.pr_labels:
                            self.label_adapter.add_label(pr.number, label)
                            logger.info("Added label '%s' to PR #%d", label, pr.number)
                        actions_taken.append(f"Added labels to PR: {record.pr_labels}")
                    elif record.pr_labels and is_dry_run_pr:
                        logger.info("[E2E_DRY_RUN] Skipping PR label addition for fake PR #%d", pr.number)

                elif action == RequestedAction.POST_COMMENT:
                    if record.comment_body:
                        # Use label_target (PR for reviews, issue otherwise)
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
                    # Use label_target (PR number for reviews, issue number otherwise)
                    self.label_adapter.add_label(label_target, label)
                    actions_taken.append(f"Added '{label}' label to #{label_target}")

                elif action == RequestedAction.ADD_NEEDS_REWORK_LABEL:
                    label = self._get_label("needs_rework")
                    # Use label_target (PR number for reviews, issue number otherwise)
                    self.label_adapter.add_label(label_target, label)
                    actions_taken.append(f"Added '{label}' label to #{label_target}")

                elif action == RequestedAction.REMOVE_CODE_REVIEW_LABEL:
                    label = self._get_label("code_review")
                    # Use label_target (PR number for reviews, issue number otherwise)
                    self.label_adapter.remove_label(label_target, label)
                    actions_taken.append(f"Removed '{label}' label from #{label_target}")

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
        total_duration = time.monotonic() - start_time
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
        # This is critical because review sessions reuse the same worktree
        record_path = worktree / (completion_path or COMPLETION_RECORD_PATH)
        existed_before = record_path.exists()
        cleanup_ok = self.cleanup_record(worktree, completion_path)
        exists_after = record_path.exists()
        logger.warning("CLEANUP: issue=%d path=%s existed_before=%s exists_after=%s",
                      issue_number, record_path, existed_before, exists_after)
        if existed_before and exists_after and not cleanup_ok:
            self._report_cleanup_failure(issue_number, worktree, record_path)

        return ProcessingResult(
            success=success,
            message=message,
            pr_url=pr_url,
            actions_taken=actions_taken if actions_taken else None,
            diagnostic_path=diagnostic_path,
            errors=errors if errors else None,
        )

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
        diagnostic_dir = worktree / ".issue-orchestrator"
        diagnostic_path = diagnostic_dir / filename

        diagnostic = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
            logger.info(
                "[DIAGNOSTIC] Wrote failure diagnostic: issue=%d path=%s",
                issue_number, diagnostic_path,
            )
            # Return relative path for inclusion in GitHub comment
            return f".issue-orchestrator/{filename}"
        except Exception as e:
            logger.warning(
                "[DIAGNOSTIC] Failed to write failure diagnostic: issue=%d error=%s",
                issue_number, e,
            )
            return None
