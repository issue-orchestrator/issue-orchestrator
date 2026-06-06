"""Action planning policy for session completion outcomes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from ..domain.models import RETROSPECTIVE_REVIEW_TERMINAL_PREFIX, Session, SessionStatus
from ..domain.triage_manifest import TriageManifest
from ..infra.config import Config
from ..ports import RepositoryHost
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .completion_types import (
    ERROR_PREFIX_CREATE_PR,
    ERROR_PREFIX_PUBLISH_BLOCKED,
    ERROR_PREFIX_PUSH,
    REVIEW_EXCHANGE_ERROR_PREFIX,
)
from .invalid_record_actions import invalid_record_actions
from .label_manager import LabelManager
from .reconciliation import ExpectedState, build_expected_for_mutation

logger = logging.getLogger(__name__)


def _read_triage_manifest(session: Session) -> TriageManifest | None:
    """Read triage manifest from session if it exists.

    The triage_manifest path is stored in the session's run manifest.json
    during launch via ctx.update_manifest({"triage_manifest": path}).

    Returns None if:
    - Session has no worktree path
    - No run directory with triage_manifest path exists
    - Manifest file doesn't exist or can't be parsed
    """
    if not session.worktree_path:
        return None

    worktree = Path(session.worktree_path)
    sessions_dir = worktree / ".issue-orchestrator" / "sessions"
    if not sessions_dir.exists():
        return None

    # Find the run directory that has a triage_manifest entry in its manifest.json.
    for run_dir in sessions_dir.iterdir():
        if not run_dir.is_dir():
            continue

        run_manifest_path = run_dir / "manifest.json"
        if not run_manifest_path.exists():
            continue

        try:
            run_manifest = json.loads(run_manifest_path.read_text())
            triage_manifest_path = run_manifest.get("triage_manifest")
            if triage_manifest_path:
                manifest_path = Path(triage_manifest_path)
                if manifest_path.exists():
                    return TriageManifest.read(manifest_path)
                logger.warning(
                    "[triage] Manifest path in run manifest doesn't exist: %s",
                    manifest_path,
                )
        except Exception as exc:
            logger.warning(
                "[triage] Failed to read manifest from %s: %s",
                run_dir,
                exc,
                exc_info=True,
            )

    return None


def critical_processing_errors(
    processing_errors: Optional[list[str]],
    *,
    pr_url: str | None = None,
    issue_number: int | None = None,
    log_downgraded: bool = False,
    context: str = "completion",
) -> tuple[list[str], list[str]]:
    """Return (critical, downgraded) publish/finalize errors.

    A create_pr error is only critical if completion reconciliation cannot find
    a PR. GitHub can still surface a transient 422 even when the PR was
    ultimately created or an equivalent open PR is discoverable.
    """
    if not processing_errors:
        return [], []

    critical: list[str] = []
    downgraded: list[str] = []
    for error in processing_errors:
        if error.startswith(ERROR_PREFIX_PUSH) or error.startswith(
            ERROR_PREFIX_PUBLISH_BLOCKED
        ):
            critical.append(error)
            continue
        if error.startswith(ERROR_PREFIX_CREATE_PR):
            if pr_url:
                downgraded.append(error)
            else:
                critical.append(error)
    if downgraded and log_downgraded and issue_number is not None:
        logger.info(
            "[COMPLETION] Ignoring non-blocking create_pr processing errors: "
            "context=%s issue=%d pr_url=%s errors=%s",
            context,
            issue_number,
            pr_url,
            downgraded,
        )
    return critical, downgraded


def _has_critical_errors(
    processing_errors: Optional[list[str]],
    *,
    pr_url: str | None = None,
) -> bool:
    """Check if processing_errors contains critical publish/finalize failures."""
    critical, _ = critical_processing_errors(processing_errors, pr_url=pr_url)
    return bool(critical)


def has_review_exchange_errors(processing_errors: Optional[list[str]]) -> bool:
    """Check if processing_errors contains review exchange halt/failure markers."""
    if not processing_errors:
        return False
    return any(
        error.startswith(REVIEW_EXCHANGE_ERROR_PREFIX) for error in processing_errors
    )


class CompletionActionPlanner:
    """Plans label/comment actions for completion outcomes."""

    def __init__(
        self,
        config: Config,
        repository_host: RepositoryHost,
        label_manager: LabelManager,
    ) -> None:
        self.config = config
        self.repository_host = repository_host
        self._lm = label_manager

    def _interrupted_retry_mode(self, session: Session) -> str | None:
        """Map session type to interrupted-retry mode."""
        if session.terminal_id.startswith(
            "issue-"
        ) or session.terminal_id.startswith("rework-"):
            return "coding"
        if session.terminal_id.startswith(
            ("review-", RETROSPECTIVE_REVIEW_TERMINAL_PREFIX)
        ):
            return "review"
        return None

    def _interrupted_retry_guard_label(self, mode: str) -> str:
        retry_cfg = self.config.retry.interrupted_sessions
        if mode == "coding":
            return retry_cfg.coding_guard_label
        return retry_cfg.review_guard_label

    def _is_interrupted_retry_enabled(self, mode: str) -> bool:
        retry_cfg = self.config.retry.interrupted_sessions
        if not retry_cfg.enabled:
            return False
        if mode == "coding":
            return retry_cfg.retry_coding
        if mode == "review":
            return retry_cfg.retry_review
        return False

    def _issue_has_label(self, issue_number: int, label: str) -> bool:
        """Best-effort label check from GitHub to guard retry loops."""
        try:
            issue = self.repository_host.get_issue(issue_number)
            if not issue:
                return False
            return label in issue.labels
        except Exception as exc:
            logger.warning(
                "[COMPLETION] Failed to read labels for issue #%d while evaluating interrupted retry: %s",
                issue_number,
                exc,
            )
            return False

    def _generate_interrupted_retry_actions(
        self,
        session: Session,
        expected: ExpectedState,
    ) -> list[Action] | None:
        """Generate auto-retry actions for interrupted sessions when configured."""
        mode = self._interrupted_retry_mode(session)
        if mode is None or not self._is_interrupted_retry_enabled(mode):
            return None

        guard_label = self._interrupted_retry_guard_label(mode)
        if self._issue_has_label(session.issue.number, guard_label):
            logger.info(
                "[COMPLETION] Interrupted auto-retry skipped for issue #%d (%s): guard label already present (%s)",
                session.issue.number,
                mode,
                guard_label,
            )
            return None

        session_kind = session.terminal_id.split("-", 1)[0]
        actions: list[Action] = [
            AddLabelAction(
                issue_number=session.issue.number,
                label=guard_label,
                reason=f"interrupted {mode} session auto-retry guard",
                expected=expected,
            ),
            AddCommentAction(
                number=session.issue.number,
                comment=(
                    f"🔁 **{session_kind.capitalize()} Session Interrupted**\n\n"
                    f"The {session_kind} session exited without a completion record (`completion command`).\n\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                    f"- Session: `{session.terminal_id}`\n\n"
                    "Auto-retry is enabled, so this will be retried on the next scheduler cycle.\n"
                    f"A guard label (`{guard_label}`) was added to prevent retry loops."
                ),
                reason=f"Notify interrupted {mode} session auto-retry",
                expected=expected,
            ),
        ]
        if session.terminal_id.startswith("issue-"):
            actions.append(
                RemoveLabelAction(
                    issue_number=session.issue.number,
                    label=self._lm.in_progress,
                    reason="Interrupted issue session - releasing claim for auto-retry",
                    expected=expected,
                )
            )
        return actions

    def _is_triage_session(self, session: Session) -> bool:
        """Check if this session is a triage review session."""
        if not self.config.triage_review_agent:
            return False
        return session.issue.agent_type == self.config.triage_review_agent

    def _generate_triage_actions(
        self,
        session: Session,
        status: SessionStatus,
        processing_errors: Optional[list[str]],
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate label actions for triage session completion.

        Adds triage-reviewed or triage-failed labels to all PRs in the manifest.
        """
        actions: list[Action] = []
        session_kind = session.terminal_id.split("-", 1)[0]

        if session_kind != "issue" or not self._is_triage_session(session):
            return actions

        triage_manifest = _read_triage_manifest(session)
        if not triage_manifest or not triage_manifest.prs:
            return actions

        # Determine which label to add based on success/failure.
        if status == SessionStatus.COMPLETED and not _has_critical_errors(
            processing_errors
        ):
            triage_label = self.config.triage_reviewed_label or "triage-reviewed"
            reason = "Triage completed successfully"
        else:
            triage_label = self.config.triage_failed_label or "triage-failed"
            reason = "Triage session failed"

        logger.info(
            "[triage] Adding '%s' label to %d PRs",
            triage_label,
            len(triage_manifest.prs),
        )

        for pr in triage_manifest.prs:
            actions.append(
                AddLabelAction(
                    issue_number=pr.number,
                    label=triage_label,
                    reason=reason,
                    expected=expected,
                )
            )

        return actions

    def generate_completion_actions(
        self,
        session: Session,
        status: SessionStatus,
        processing_errors: Optional[list[str]] = None,
        diagnostic_path: Optional[str] = None,
        review_exchange_halted: bool = False,
        blocked_label: Optional[str] = None,
        blocked_reason: Optional[str] = None,
        pr_url: Optional[str] = None,
        completion_detail: Optional[dict[str, Any]] = None,
    ) -> tuple[Action, ...]:
        """Generate label/comment actions for session completion.

        This encapsulates the POLICY logic for what labels to add/remove
        when a session completes with various statuses.
        """
        expected = build_expected_for_mutation()

        # Check for critical processing errors (push/PR creation failures).
        critical_errors, _downgraded_errors = critical_processing_errors(
            processing_errors,
            pr_url=pr_url,
            issue_number=session.issue.number,
            log_downgraded=True,
            context="actions",
        )

        # If agent said "completed" but critical processing failed, treat as blocked-failed.
        if status == SessionStatus.COMPLETED and critical_errors:
            logger.info(
                "[COMPLETION] Agent said completed but processing failed: issue=%d errors=%s",
                session.issue.number,
                critical_errors,
            )
            return tuple(
                self._generate_processing_failure_actions(
                    session, critical_errors, diagnostic_path, expected
                )
            )

        if status == SessionStatus.COMPLETED and review_exchange_halted:
            logger.info(
                "[COMPLETION] Review exchange halted - generating blocked-failed actions: issue=%d",
                session.issue.number,
            )
            return tuple(self._generate_review_exchange_halted_actions(session, expected))

        if status == SessionStatus.TIMED_OUT:
            return tuple(self._generate_timeout_actions(session, expected))

        if status == SessionStatus.FAILED:
            invalid_actions = invalid_record_actions(
                session=session,
                expected=expected,
                labels=self._lm,
                detail=completion_detail,
                diagnostic_path=diagnostic_path,
            )
            if invalid_actions is not None:
                return tuple(invalid_actions)
            return tuple(self._generate_failure_actions(session, expected))

        if status == SessionStatus.BLOCKED:
            return tuple(
                self._generate_blocked_actions(
                    session,
                    expected,
                    blocked_label=blocked_label,
                    blocked_reason=blocked_reason,
                )
            )

        if status == SessionStatus.COMPLETED:
            # POLICY: Completion -> release in-progress (claim maintained via pr-pending).
            actions: list[Action] = [
                RemoveLabelAction(
                    issue_number=session.issue.number,
                    label=self._lm.in_progress,
                    reason="Session completed successfully",
                    expected=expected,
                )
            ]
            actions.extend(
                self._generate_triage_actions(
                    session, status, processing_errors, expected
                )
            )
            return tuple(actions)

        # NEEDS_HUMAN keeps in-progress to maintain the ownership claim.
        return ()

    def _generate_processing_failure_actions(
        self,
        session: Session,
        critical_errors: list[str],
        diagnostic_path: Optional[str],
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate actions when agent said completed but push/PR creation failed.

        Tracks consecutive publish failures via publish-fail-count-N labels.
        After max_consecutive_publish_failures, escalates to needs-human.
        """
        issue_number = session.issue.number
        in_progress_label = self._lm.in_progress

        # Count previous consecutive publish failures from issue labels.
        prev_count = self._lm.extract_publish_fail_count(session.issue.labels)
        new_count = prev_count + 1
        max_failures = self.config.max_consecutive_publish_failures

        first_error = critical_errors[0][:100] if critical_errors else "Unknown error"
        if len(first_error) == 100:
            first_error += "..."

        diagnostic_info = ""
        if diagnostic_path and session.worktree_path:
            worktree_name = Path(session.worktree_path).name
            diagnostic_info = (
                f"\n**Diagnostic file:** `{worktree_name}/{diagnostic_path}`\n"
            )

        if new_count >= max_failures:
            logger.info(
                "[COMPLETION] Publish failure count %d >= max %d, escalating to needs-human: issue=%d",
                new_count,
                max_failures,
                issue_number,
            )
            return [
                AddLabelAction(
                    issue_number=issue_number,
                    label=self._lm.needs_human,
                    reason=f"Publishing failed {new_count} consecutive times — escalating to needs-human",
                    expected=expected,
                ),
                AddCommentAction(
                    number=issue_number,
                    comment=(
                        "❌ **Publishing Failed — Escalated**\n\n"
                        f"Publishing has failed **{new_count} consecutive times** "
                        f"(max: {max_failures}). This issue needs human investigation.\n\n"
                        f"**Latest error:** {first_error}\n"
                        f"{diagnostic_info}\n"
                        f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                        f"- Session: `{session.terminal_id}`\n"
                    ),
                    reason="Escalate repeated publish failure to human",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=in_progress_label,
                    reason="Publishing failed - releasing claim",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=self._lm.needs_rework,
                    reason="Publishing failed - clearing needs-rework to prevent re-queuing loop",
                    expected=expected,
                ),
            ]

        actions: list[Action] = [
            AddLabelAction(
                issue_number=issue_number,
                label=self._lm.publish_failed,
                reason="Publishing failed after agent completion (push/PR creation failed)",
                expected=expected,
            ),
            AddCommentAction(
                number=issue_number,
                comment=(
                    f"❌ **Publishing Failed** (attempt {new_count}/{max_failures})\n\n"
                    "The agent completed its work, but the orchestrator could not push or create a PR.\n\n"
                    f"**Error:** {first_error}\n"
                    f"{diagnostic_info}\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                    f"- Session: `{session.terminal_id}`\n\n"
                    f"This issue has been marked as `{self._lm.publish_failed}` and will not be automatically retried.\n"
                    "Remove the label to retry."
                ),
                reason="Notify about processing failure",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=issue_number,
                label=in_progress_label,
                reason="Processing failed - releasing claim",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=issue_number,
                label=self._lm.needs_rework,
                reason="Publishing failed - clearing needs-rework to prevent re-queuing loop",
                expected=expected,
            ),
        ]

        if prev_count > 0:
            actions.append(
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=self._lm.publish_fail_count_label(prev_count),
                    reason="Updating publish failure count",
                    expected=expected,
                )
            )
        actions.append(
            AddLabelAction(
                issue_number=issue_number,
                label=self._lm.publish_fail_count_label(new_count),
                reason=f"Publish failure #{new_count}",
                expected=expected,
            )
        )

        return actions

    def _generate_timeout_actions(
        self,
        session: Session,
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate actions when session timed out."""
        issue_number = session.issue.number
        in_progress_label = self._lm.in_progress
        is_issue_session = session.terminal_id.startswith("issue-")
        session_kind = session.terminal_id.split("-", 1)[0]

        if is_issue_session:
            timeout_mins = (
                session.agent_config.timeout_minutes
                if session.agent_config
                else "unknown"
            )
            return [
                AddLabelAction(
                    issue_number=issue_number,
                    label=self._lm.blocked_failed,
                    reason=f"Session timed out after {session.runtime_minutes} minutes",
                    expected=expected,
                ),
                AddCommentAction(
                    number=issue_number,
                    comment=(
                        "⏱️ **Session Timed Out**\n\n"
                        f"The agent session exceeded the {timeout_mins} minute timeout limit.\n\n"
                        f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                        f"- Session: `{session.terminal_id}`\n\n"
                        f"This issue has been marked as `{self._lm.blocked_failed}` and will not be automatically retried.\n"
                        "Remove the label to allow reprocessing."
                    ),
                    reason="Notify about session timeout",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=in_progress_label,
                    reason="Session timed out - releasing claim",
                    expected=expected,
                ),
            ]
        return [
            AddCommentAction(
                number=issue_number,
                comment=(
                    f"⏱️ **{session_kind.capitalize()} Session Timed Out**\n\n"
                    f"The {session_kind} session exceeded its timeout and did not produce an outcome.\n\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                    f"- Session: `{session.terminal_id}`\n\n"
                    "The PR remains pending; review will be retried automatically."
                ),
                reason=f"Notify about {session_kind} session timeout",
                expected=expected,
            ),
        ]

    def _generate_failure_actions(
        self,
        session: Session,
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate actions when session failed without completion command."""
        if retry_actions := self._generate_interrupted_retry_actions(session, expected):
            return retry_actions

        issue_number = session.issue.number
        in_progress_label = self._lm.in_progress
        is_issue_session = session.terminal_id.startswith("issue-")
        session_kind = session.terminal_id.split("-", 1)[0]

        if is_issue_session:
            return [
                AddLabelAction(
                    issue_number=issue_number,
                    label=self._lm.needs_human,
                    reason="Session terminated without calling completion command (mandatory)",
                    expected=expected,
                ),
                AddCommentAction(
                    number=issue_number,
                    comment=(
                        "🔍 **Session Needs Investigation**\n\n"
                        "The agent session terminated without calling the completion command "
                        "(`coding-done` or `reviewer-done`).\n\n"
                        "**This is unexpected** - the completion command is mandatory and must be called "
                        "to complete any session (completed, blocked, or needs_human).\n\n"
                        f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                        f"- Session: `{session.terminal_id}`\n\n"
                        "**Possible causes:**\n"
                        "- Agent crashed or was interrupted\n"
                        "- Orchestrator shutdown/restart interrupted the session lifecycle\n"
                        "- Agent ignored the mandatory completion command requirement\n"
                        "- Infrastructure issue prevented completion\n\n"
                        f"This issue has been marked as `{self._lm.needs_human}` for investigation.\n"
                        "Remove the label after investigating to allow reprocessing."
                    ),
                    reason="Notify about session needing human investigation",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=in_progress_label,
                    reason="Session failed - releasing claim",
                    expected=expected,
                ),
            ]
        return [
            AddCommentAction(
                number=issue_number,
                comment=(
                    f"🔍 **{session_kind.capitalize()} Session Needs Investigation**\n\n"
                    f"The {session_kind} session terminated without calling the completion command.\n\n"
                    "**This is unexpected** - the completion command is mandatory.\n\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                    f"- Session: `{session.terminal_id}`\n\n"
                    "Possible causes include orchestrator shutdown/restart, agent crash, or workflow interruption.\n\n"
                    "The PR remains pending; please investigate what happened."
                ),
                reason=f"Notify about {session_kind} session needing investigation",
                expected=expected,
            ),
        ]

    def _generate_blocked_actions(
        self,
        session: Session,
        expected: ExpectedState,
        blocked_label: Optional[str] = None,
        blocked_reason: Optional[str] = None,
    ) -> list[Action]:
        """Generate actions when agent explicitly reported blocked."""
        is_issue_session = session.terminal_id.startswith("issue-")
        label = blocked_label or self._lm.blocked

        if is_issue_session:
            reason_text = (
                blocked_reason.strip() if blocked_reason else "No reason provided."
            )
            return [
                AddLabelAction(
                    issue_number=session.issue.number,
                    label=label,
                    reason="Agent reported issue as blocked",
                    expected=expected,
                ),
                AddCommentAction(
                    number=session.issue.number,
                    comment=(
                        "🚧 **Session Blocked**\n\n"
                        "The agent reported this issue as blocked.\n\n"
                        f"**Reason:** {reason_text}\n"
                        f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                        f"- Session: `{session.terminal_id}`\n\n"
                        f"This issue has been marked as `{label}` and will not be automatically retried.\n"
                        "Remove the label to allow reprocessing."
                    ),
                    reason="Notify about blocked session and reason",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=session.issue.number,
                    label=self._lm.in_progress,
                    reason="Session blocked - releasing claim",
                    expected=expected,
                ),
            ]
        # Review/rework BLOCKED completions do not map to issue-blocking labels;
        # their parent workflows own any PR/review state transitions.
        return []

    def _generate_review_exchange_halted_actions(
        self,
        session: Session,
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate hold actions when a review exchange halts without progress."""
        issue_number = session.issue.number
        return [
            AddLabelAction(
                issue_number=issue_number,
                label=self._lm.blocked_failed,
                reason="Review exchange halted with no progress",
                expected=expected,
            ),
            AddCommentAction(
                number=issue_number,
                comment=(
                    "⚠️ **Review Exchange Halted**\n\n"
                    "The automated review exchange stopped because it could not make further progress.\n\n"
                    f"- Session: `{session.terminal_id}`\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n\n"
                    f"This issue has been marked as `{self._lm.blocked_failed}` and will not be retried automatically.\n"
                    "Use Retry/Unblock when you want to run it again."
                ),
                reason="Notify that review exchange halted and issue is on hold",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=issue_number,
                label=self._lm.in_progress,
                reason="Review exchange halted - releasing claim",
                expected=expected,
            ),
        ]
