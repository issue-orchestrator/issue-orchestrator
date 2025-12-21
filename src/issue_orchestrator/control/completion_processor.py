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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import (
    CompletionRecord,
    CompletionOutcome,
    RequestedAction,
    COMPLETION_RECORD_PATH,
)
from ..domain.events import EventBus, SessionEvent

logger = logging.getLogger(__name__)


@runtime_checkable
class LabelAdapter(Protocol):
    """Protocol for label operations."""

    def add_label(self, issue_number: int, label: str) -> None: ...
    def remove_label(self, issue_number: int, label: str) -> None: ...


@runtime_checkable
class PRAdapter(Protocol):
    """Protocol for PR operations."""

    def create_pr(
        self, title: str, body: str, head: str, base: str = "main"
    ) -> "PRInfo": ...
    def add_comment(self, issue_or_pr_number: int, body: str) -> str: ...


@runtime_checkable
class GitAdapter(Protocol):
    """Protocol for git operations."""

    def push(
        self,
        worktree: Path,
        remote: str = "origin",
        force_with_lease: bool = True,
        set_upstream: bool = True,
    ) -> "PushResult": ...

    def get_current_branch(self, worktree: Path) -> str | None: ...
    def has_uncommitted_changes(self, worktree: Path) -> bool: ...


@dataclass
class PRInfo:
    """Minimal PR info for return type."""

    number: int
    url: str


@dataclass
class PushResult:
    """Minimal push result for return type."""

    success: bool
    message: str


@dataclass
class ProcessingResult:
    """Result of processing a completion record."""

    success: bool
    message: str
    pr_url: str | None = None
    actions_taken: list[str] | None = None
    errors: list[str] | None = None


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
    ):
        """Initialize the processor with required adapters.

        Args:
            label_adapter: Adapter for label operations (add/remove labels).
            pr_adapter: Adapter for PR operations (create PR, add comment).
            git_adapter: Adapter for git operations (push).
            event_bus: Optional EventBus for emitting processing events.
            label_config: Optional mapping of label names (e.g., {"blocked": "blocked"}).
        """
        self.label_adapter = label_adapter
        self.pr_adapter = pr_adapter
        self.git_adapter = git_adapter
        self.event_bus = event_bus
        self.label_config = label_config or {}

    def _emit(
        self,
        event_type: SessionEvent,
        issue_number: int,
        data: dict | None = None,
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
        }
        return self.label_config.get(key, defaults.get(key, key))

    def read_completion_record(self, worktree: Path) -> CompletionRecord | None:
        """Read and validate a completion record from a worktree.

        Args:
            worktree: Path to the worktree directory.

        Returns:
            The validated CompletionRecord, or None if not found/invalid.
        """
        record_path = worktree / COMPLETION_RECORD_PATH

        if not record_path.exists():
            logger.debug(f"No completion record found at {record_path}")
            return None

        try:
            with open(record_path) as f:
                data = json.load(f)
            record = CompletionRecord.from_dict(data)
            logger.info(
                f"Read completion record: {record.outcome.value} "
                f"(session: {record.session_id})"
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

    def process(
        self, worktree: Path, issue_number: int, issue_title: str
    ) -> ProcessingResult:
        """Process a completion record and execute actions.

        Args:
            worktree: Path to the worktree containing the completion record.
            issue_number: The GitHub issue number this work is for.
            issue_title: The issue title (for PR creation).

        Returns:
            ProcessingResult with success status and details.
        """
        actions_taken: list[str] = []
        errors: list[str] = []
        pr_url: str | None = None

        # Read the completion record
        record = self.read_completion_record(worktree)
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

        # Get branch name for PR operations
        branch = self.git_adapter.get_current_branch(worktree)

        # Log what actions were requested
        logger.info(
            "Processing completion for #%d: outcome=%s, requested_actions=%s",
            issue_number,
            record.outcome.value,
            [a.value for a in record.requested_actions],
        )

        # Execute requested actions in order
        for action in record.requested_actions:
            logger.info("Executing action: %s for issue #%d", action.value, issue_number)
            try:
                if action == RequestedAction.PUSH_BRANCH:
                    result = self.git_adapter.push(worktree)
                    if result.success:
                        actions_taken.append(f"Pushed branch to remote")
                        logger.info("Push succeeded for #%d", issue_number)
                    else:
                        errors.append(f"Push failed: {result.message}")
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

                elif action == RequestedAction.POST_COMMENT:
                    if record.comment_body:
                        self.pr_adapter.add_comment(issue_number, record.comment_body)
                        actions_taken.append("Posted comment to issue")

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
                    self.label_adapter.add_label(issue_number, label)
                    actions_taken.append(f"Added '{label}' label")

                elif action == RequestedAction.ADD_NEEDS_REWORK_LABEL:
                    label = self._get_label("needs_rework")
                    self.label_adapter.add_label(issue_number, label)
                    actions_taken.append(f"Added '{label}' label")

                elif action == RequestedAction.REMOVE_CODE_REVIEW_LABEL:
                    label = self._get_label("code_review")
                    self.label_adapter.remove_label(issue_number, label)
                    actions_taken.append(f"Removed '{label}' label")

            except Exception as e:
                logger.exception(
                    "Exception executing action %s for #%d: %s",
                    action.value,
                    issue_number,
                    e,
                )
                errors.append(f"{action.value}: {e}")

        # Determine overall success
        success = len(errors) == 0 or (
            # Partial success if we at least completed the main work
            RequestedAction.PUSH_BRANCH in record.requested_actions
            and "Pushed branch to remote" in actions_taken
        )

        # Build result message and emit events
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

        return ProcessingResult(
            success=success,
            message=message,
            pr_url=pr_url,
            actions_taken=actions_taken if actions_taken else None,
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

    def cleanup_record(self, worktree: Path) -> bool:
        """Remove the completion record after processing.

        Args:
            worktree: Path to the worktree.

        Returns:
            True if successfully removed, False otherwise.
        """
        record_path = worktree / COMPLETION_RECORD_PATH
        try:
            if record_path.exists():
                record_path.unlink()
                logger.debug(f"Removed completion record: {record_path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to remove completion record: {e}")
            return False
