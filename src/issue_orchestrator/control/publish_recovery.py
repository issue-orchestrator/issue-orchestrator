"""Owner for manual publish recovery of publish-failed issues.

The dashboard "Retry publish" button lands here. A publish failure means a
completed coding session pushed/created-PR unsuccessfully; the work itself is
intact in the worktree (branch + completion record on disk). This service:

1. Records durable retry *locators* when a publish fails (so the issue stays
   retryable across restarts) via :class:`PublishRetryLocatorStore`.
2. On retry, either recovers an already-created PR, or re-runs the publish
   **off the request thread** on the shared :class:`BackgroundJobRunner` — the
   same live-completion runner introduced by #6573 — reconstructing the inputs
   from the stored locators plus the on-disk completion record.
3. Reconciles success on the next ``tick`` drain: clears publish-failed state
   and the stored locators.

The heavy work (``CompletionProcessor.process``: validate — cache-aware on the
same commit — + push + PR) must stay off the request thread, which is why the
republish is dispatched to the runner rather than run inline.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Protocol

from ..control.actions import (
    AddLabelAction,
    RemoveLabelAction,
    SupersedePullRequestAction,
)
from ..domain.models import OrchestratorState, SessionHistoryEntry
from ..domain.pr_attempt_scope import scope_prs_to_active_issue_branch
from ..domain.publish_retry import PublishRetryLocators
from ..ports.background_job import BackgroundJobRunner, CompletedJob
from ..ports.fresh_issue_reader import FreshIssueReader
from ..ports.publish_retry_locator_store import PublishRetryLocatorStore
from ..ports.pull_request_tracker import PRInfo
from .completion_types import ERROR_PREFIX_CREATE_PR, ERROR_PREFIX_PUSH

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..domain.session_run import SessionRunAssets
    from .completion_types import ProcessingResult
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

_JOB_PREFIX = "republish:"
_PUBLISH_FAILURE_PREFIXES = (ERROR_PREFIX_PUSH, ERROR_PREFIX_CREATE_PR)


class _RepositoryHost(Protocol):
    def get_issue(self, issue_number: int) -> Any: ...
    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]: ...


class _CompletionProcessor(Protocol):
    def process(
        self,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        *,
        run_assets: "SessionRunAssets",
        pr_number: int | None = ...,
        completion_path: str | None = ...,
        agent_label: str | None = ...,
    ) -> "ProcessingResult": ...


class _ActionApplier(Protocol):
    def apply(
        self,
        action: AddLabelAction | RemoveLabelAction | SupersedePullRequestAction,
    ) -> Any: ...


@dataclass(frozen=True)
class RetryPublishResult:
    """Result of a retry-publish request."""

    status: str
    message: str
    job_id: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None


@dataclass(frozen=True)
class _RetryDecision:
    allowed: bool
    reason: str
    locators: PublishRetryLocators | None = None
    issue_title: str = ""
    agent_label: str | None = None


@dataclass(frozen=True)
class _RepublishContext:
    """Reconcile context for an in-flight republish job, kept until it drains."""

    issue_number: int
    issue_title: str
    agent_label: str | None
    worktree_path: str


def is_publish_failure(processing_errors: Sequence[str] | None) -> bool:
    """True iff the completion errors indicate a push / PR-creation failure."""
    if not processing_errors:
        return False
    return any(
        str(error).split(":", 1)[0].strip() in _PUBLISH_FAILURE_PREFIXES
        for error in processing_errors
    )


class PublishRecoveryService:
    """Backend owner for retrying or recovering publish-failed issues."""

    def __init__(
        self,
        repository_host: _RepositoryHost,
        completion_processor: _CompletionProcessor,
        locator_store: PublishRetryLocatorStore,
        runner: BackgroundJobRunner,
        label_manager: "LabelManager",
        fresh_issue_reader: FreshIssueReader,
        action_applier: _ActionApplier,
    ) -> None:
        self._repository_host = repository_host
        self._completion_processor = completion_processor
        self._locator_store = locator_store
        self._runner = runner
        self._lm = label_manager
        self._fresh_issue_reader = fresh_issue_reader
        self._action_applier = action_applier
        self._lock = Lock()
        self._pending: dict[int, _RepublishContext] = {}
        self._results: dict[int, "ProcessingResult"] = {}
        # Issues whose in-flight republish was abandoned (reset/termination)
        # while the worker thread could not be force-killed. A late drain of
        # such a job must supersede any PR it created rather than reconcile it
        # as success. Consumed on the next drain of that issue's job.
        self._tombstoned: set[int] = set()

    # ------------------------------------------------------------------
    # Recording (called on the live completion path when a publish fails)
    # ------------------------------------------------------------------

    def record_publish_failure(
        self,
        session: "Session",
        processing_errors: Sequence[str] | None,
    ) -> None:
        """Persist durable retry locators when a session's publish fails.

        Idempotent per issue: overwrites any prior locators. No-op when the
        completion errors are not a publish (push/PR) failure.
        """
        if not is_publish_failure(processing_errors):
            return
        locators = PublishRetryLocators(
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            session_key=session.key.stable_id(),
            worktree_path=str(session.worktree_path),
            branch_name=session.branch_name,
            completion_path=session.completion_path,
            run_assets=session.run_assets,
            agent_label=session.agent_label,
            pr_number=session.pr_number,
        )
        self._locator_store.save(locators)
        logger.info(
            "[publish-retry] Persisted retry locators for issue=%s branch=%s",
            session.issue.number,
            session.branch_name,
        )

    # ------------------------------------------------------------------
    # Retry request (called from the web thread)
    # ------------------------------------------------------------------

    def can_retry_publish(self, issue_number: int, state: OrchestratorState) -> bool:
        """Return whether retry-publish is currently available for an issue."""
        return self._retry_decision(issue_number, state).allowed

    def retry_publish(self, issue_number: int, state: OrchestratorState) -> RetryPublishResult:
        """Retry publish for a publish-failed issue or recover an already-created PR."""
        decision = self._retry_decision(issue_number, state)
        if not decision.allowed or decision.locators is None:
            logger.info(
                "[publish-retry] Rejecting retry request for issue=%s reason=%s",
                issue_number,
                decision.reason,
            )
            return RetryPublishResult(status="rejected", message=decision.reason)

        locators = decision.locators
        existing_pr = self._matching_open_pr(issue_number, locators.branch_name)
        if existing_pr is not None:
            self._finalize_success(
                state=state,
                issue_number=issue_number,
                issue_title=decision.issue_title,
                agent_label=decision.agent_label,
                pr_url=existing_pr.url,
                pr_number=existing_pr.number,
                worktree_path=locators.worktree_path,
                history_reason="Recovered awaiting-merge state from existing PR",
            )
            self._locator_store.clear(issue_number)
            logger.info(
                "[publish-retry] Recovered existing PR for issue=%s pr=%s branch=%s",
                issue_number,
                existing_pr.number,
                existing_pr.branch,
            )
            return RetryPublishResult(
                status="recovered_existing_pr",
                message=f"Recovered existing PR #{existing_pr.number}",
                pr_url=existing_pr.url,
                pr_number=existing_pr.number,
            )

        submitted = self._submit_republish(
            locators,
            issue_title=decision.issue_title,
            agent_label=decision.agent_label,
        )
        if not submitted:
            message = "A publish job is already active for this issue"
            logger.info(
                "[publish-retry] Skipping duplicate retry submit for issue=%s",
                issue_number,
            )
            return RetryPublishResult(status="rejected", message=message)

        logger.info(
            "[publish-retry] Submitted publish retry for issue=%s branch=%s",
            issue_number,
            locators.branch_name,
        )
        return RetryPublishResult(
            status="submitted",
            message="Publish retry queued",
            job_id=self._job_id(issue_number),
        )

    # ------------------------------------------------------------------
    # Termination (called when an issue's attempt is reset / torn down)
    # ------------------------------------------------------------------

    def abandon_issue(self, issue_number: int) -> None:
        """Abandon any in-flight publish retry and drop the durable locators.

        Reset (and any issue-runtime teardown) calls this so a republish worker
        that finishes *after* the attempt is discarded cannot repopulate the
        superseded attempt — removing ``publish-failed``, adding ``pr-pending``,
        appending completed history, or leaving an unsuperseded stale PR.

        The daemon worker thread cannot be force-killed mid-``process``, so this
        does the honest thing instead of pretending to cancel it:

        - drops the in-flight republish context/result so a late drain is
          ignored rather than reconciled as success,
        - clears the stored locators (this attempt is no longer retryable),
        - tombstones the issue so :meth:`drain_completed_retries` supersedes any
          PR the late worker created before it was abandoned.

        The tombstone is scoped to the single in-flight job: it is consumed by
        the next drain of that job (which fires within a tick, long before any
        fresh attempt could fail publish and submit a new republish), so it
        never drops a legitimate later retry.
        """
        with self._lock:
            had_context = self._pending.pop(issue_number, None) is not None
            self._results.pop(issue_number, None)
            if had_context or self._runner.is_running(self._job_id(issue_number)):
                self._tombstoned.add(issue_number)
                logger.info(
                    "[publish-retry] Abandoned in-flight republish for issue=%s; "
                    "late completion will be superseded",
                    issue_number,
                )
        self._locator_store.clear(issue_number)

    # ------------------------------------------------------------------
    # Reconciliation (drained on the tick thread)
    # ------------------------------------------------------------------

    def drain_completed_retries(self, state: OrchestratorState) -> None:
        """Reconcile republish jobs that finished since the last tick.

        On success: clear publish-failed state + stored locators. On failure or
        error: leave the publish-failed label and locators in place so the issue
        stays retryable (no permanent lockout). Jobs whose issue was abandoned
        (reset/termination) are superseded, not reconciled.
        """
        for job in self._runner.drain_completed():
            issue_number = self._issue_from_job_id(job.job_id)
            if issue_number is None:
                continue
            with self._lock:
                tombstoned = issue_number in self._tombstoned
                self._tombstoned.discard(issue_number)
                context = self._pending.pop(issue_number, None)
                result = self._results.pop(issue_number, None)
            if tombstoned:
                self._supersede_abandoned_retry(issue_number, job, result)
                continue
            if context is None:
                # A job_id we didn't dispatch (or already drained) — ignore.
                continue
            if job.error is not None:
                logger.error(
                    "[publish-retry] Republish job for issue=%s raised: %s",
                    issue_number,
                    job.error,
                )
                continue
            if result is None:
                logger.error(
                    "[publish-retry] Republish job for issue=%s finished without a result",
                    issue_number,
                )
                continue
            if not result.success:
                logger.warning(
                    "[publish-retry] Republish for issue=%s failed: %s",
                    issue_number,
                    result.message,
                )
                continue
            self.reconcile_retry_publish_success(
                state=state,
                issue_number=issue_number,
                issue_title=context.issue_title,
                agent_label=context.agent_label,
                pr_url=result.pr_url,
                pr_number=self._extract_pr_number(result.pr_url),
                worktree_path=context.worktree_path,
            )
            self._locator_store.clear(issue_number)

    def reconcile_retry_publish_success(
        self,
        *,
        state: OrchestratorState,
        issue_number: int,
        issue_title: str,
        agent_label: str | None,
        pr_url: str | None,
        pr_number: int | None,
        worktree_path: str | None,
    ) -> None:
        """Clear stale publish-failed state after a manual publish retry succeeds."""
        self._finalize_success(
            state=state,
            issue_number=issue_number,
            issue_title=issue_title,
            agent_label=agent_label,
            pr_url=pr_url,
            pr_number=pr_number,
            worktree_path=worktree_path,
            history_reason="Publish retry succeeded",
        )
        logger.info(
            "[publish-retry] Finalized successful retry for issue=%s pr=%s",
            issue_number,
            pr_number,
        )

    def _supersede_abandoned_retry(
        self,
        issue_number: int,
        job: CompletedJob,
        result: "ProcessingResult | None",
    ) -> None:
        """Close any PR left open by a republish that finished after abandon.

        No labels/history are touched — the attempt was discarded by reset.
        Only a PR the late worker created past reset's supersede scan needs
        closing, and only while it is still open (reset may have already
        superseded it if the push landed before the scan).
        """
        if job.error is not None:
            logger.warning(
                "[publish-retry] Abandoned republish for issue=%s raised after "
                "reset: %s",
                issue_number,
                job.error,
            )
            return
        pr_number = self._extract_pr_number(result.pr_url) if result else None
        if result is None or not result.success or pr_number is None:
            logger.info(
                "[publish-retry] Abandoned republish for issue=%s produced no "
                "PR to supersede",
                issue_number,
            )
            return
        open_prs = self._repository_host.get_prs_for_issue(issue_number, state="open")
        if not any(pr.number == pr_number for pr in open_prs):
            logger.info(
                "[publish-retry] Late retry PR #%s for reset issue=%s already "
                "closed/superseded",
                pr_number,
                issue_number,
            )
            return
        outcome = self._action_applier.apply(
            SupersedePullRequestAction(
                issue_number=issue_number,
                pr_number=pr_number,
                comment=(
                    "Superseded: this PR was created by a publish retry that "
                    "finished after the issue was reset. The orchestrator has "
                    "discarded that attempt; a fresh attempt will publish "
                    "separately."
                ),
                reason="publish retry completed after reset",
            )
        )
        if not outcome.success:
            logger.error(
                "[publish-retry] Failed to supersede late retry PR #%s for "
                "issue=%s: %s",
                pr_number,
                issue_number,
                outcome.error,
            )
            return
        logger.info(
            "[publish-retry] Superseded late retry PR #%s for reset issue=%s",
            pr_number,
            issue_number,
        )

    # ------------------------------------------------------------------
    # Decision / gating
    # ------------------------------------------------------------------

    def _retry_decision(self, issue_number: int, state: OrchestratorState) -> _RetryDecision:
        issue = self._repository_host.get_issue(issue_number)
        if issue is None:
            return _RetryDecision(False, f"Issue #{issue_number} not found")

        labels = tuple(self._current_labels(issue_number))
        block_reason = self._retry_state_block_reason(issue_number, state, labels)
        if block_reason:
            return _RetryDecision(False, block_reason)

        locators = self._locator_store.get(issue_number)
        if locators is None:
            return _RetryDecision(False, "No publish-retry locators found for issue")

        locator_reason = self._retry_locator_block_reason(locators)
        if locator_reason:
            return _RetryDecision(False, locator_reason)

        issue_title = str(getattr(issue, "title", "") or locators.issue_title or f"Issue #{issue_number}")
        agent_label = self._resolve_agent_label(issue, locators)
        return _RetryDecision(
            True,
            "ok",
            locators=locators,
            issue_title=issue_title,
            agent_label=agent_label,
        )

    def _retry_state_block_reason(
        self,
        issue_number: int,
        state: OrchestratorState,
        labels: tuple[str, ...],
    ) -> str | None:
        if self._lm.publish_failed not in labels:
            return "Issue is not blocked by a publish failure"
        if any(session.issue.number == issue_number for session in state.active_sessions):
            return "Issue has an active session"
        if self._runner.is_running(self._job_id(issue_number)):
            return "Issue already has a running publish job"
        return None

    def _retry_locator_block_reason(self, locators: PublishRetryLocators) -> str | None:
        worktree = Path(locators.worktree_path)
        if not worktree.exists():
            return "Retry worktree no longer exists"
        completion_path = worktree / locators.completion_path
        if not completion_path.exists():
            return "Completion record for retry is missing"
        return None

    # ------------------------------------------------------------------
    # Republish submission
    # ------------------------------------------------------------------

    def _submit_republish(
        self,
        locators: PublishRetryLocators,
        *,
        issue_title: str,
        agent_label: str | None,
    ) -> bool:
        issue_number = locators.issue_number
        job_id = self._job_id(issue_number)

        def run() -> None:
            result = self._completion_processor.process(
                Path(locators.worktree_path),
                issue_number,
                issue_title,
                run_assets=locators.run_assets,
                pr_number=locators.pr_number,
                completion_path=locators.completion_path,
                agent_label=agent_label,
            )
            with self._lock:
                self._results[issue_number] = result

        if not self._runner.submit(job_id, run):
            return False
        with self._lock:
            self._pending[issue_number] = _RepublishContext(
                issue_number=issue_number,
                issue_title=issue_title,
                agent_label=agent_label,
                worktree_path=locators.worktree_path,
            )
        return True

    @staticmethod
    def _job_id(issue_number: int) -> str:
        return f"{_JOB_PREFIX}{issue_number}"

    @staticmethod
    def _issue_from_job_id(job_id: str) -> int | None:
        if not job_id.startswith(_JOB_PREFIX):
            return None
        try:
            return int(job_id[len(_JOB_PREFIX):])
        except ValueError:
            return None

    @staticmethod
    def _extract_pr_number(pr_url: str | None) -> int | None:
        if not pr_url:
            return None
        parts = pr_url.rstrip("/").split("/")
        if len(parts) >= 2 and parts[-2] == "pull":
            try:
                return int(parts[-1])
            except ValueError:
                return None
        return None

    # ------------------------------------------------------------------
    # Success finalization + labels
    # ------------------------------------------------------------------

    def _matching_open_pr(self, issue_number: int, expected_branch: str) -> PRInfo | None:
        prs = self._repository_host.get_prs_for_issue(issue_number, state="open")
        scoped = scope_prs_to_active_issue_branch(
            issue_number,
            prs,
            expected_branch=expected_branch,
        )
        if scoped.ignored:
            logger.info(
                "[publish-retry] Ignoring %d prior-attempt PR(s) for issue=%s expected_branch=%s",
                len(scoped.ignored),
                issue_number,
                expected_branch,
            )
        return scoped.first_matching

    def _finalize_success(
        self,
        *,
        state: OrchestratorState,
        issue_number: int,
        issue_title: str,
        agent_label: str | None,
        pr_url: str | None,
        pr_number: int | None,
        worktree_path: str | None,
        history_reason: str,
    ) -> None:
        labels = tuple(self._current_labels(issue_number))
        label_actions: list[AddLabelAction | RemoveLabelAction] = []
        if self._lm.publish_failed in labels:
            label_actions.append(
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=self._lm.publish_failed,
                    reason="retry publish succeeded",
                )
            )
        if pr_url and self._lm.pr_pending not in labels:
            label_actions.append(
                AddLabelAction(
                    issue_number=issue_number,
                    label=self._lm.pr_pending,
                    reason="publish retry recovered or succeeded",
                )
            )
        for label in labels:
            if self._lm.extract_publish_fail_count([label]) > 0:
                label_actions.append(
                    RemoveLabelAction(
                        issue_number=issue_number,
                        label=label,
                        reason="publish retry succeeded",
                    )
                )
        self._apply_label_actions(label_actions)

        state.failed_this_cycle.discard(issue_number)
        state.discovered_failures = [
            failure for failure in state.discovered_failures
            if failure.issue_number != issue_number
        ]
        state.session_history = [
            entry for entry in state.session_history
            if not self._is_publish_failure_history(entry, issue_number)
        ]
        if issue_number not in state.completed_today:
            state.completed_today.append(issue_number)
        state.session_history.append(
            SessionHistoryEntry(
                issue_number=issue_number,
                title=issue_title,
                agent_type=agent_label or "agent:unknown",
                status="completed",
                runtime_minutes=0,
                pr_url=pr_url,
                status_reason=history_reason,
                worktree_path=Path(worktree_path) if worktree_path else None,
                completed_at=datetime.now(timezone.utc),
            )
        )

    def _current_labels(self, issue_number: int) -> list[str]:
        return [str(label) for label in self._fresh_issue_reader.read_issue_labels(issue_number)]

    def _resolve_agent_label(self, issue: Any, locators: PublishRetryLocators) -> str | None:
        for label in getattr(issue, "labels", ()) or ():
            if str(label).startswith("agent:"):
                return str(label)
        if locators.agent_label and locators.agent_label.strip():
            return locators.agent_label
        return None

    def _apply_label_actions(self, actions: list[AddLabelAction | RemoveLabelAction]) -> None:
        for action in actions:
            result = self._action_applier.apply(action)
            if result.success:
                continue
            raise RuntimeError(result.error or f"Label action failed for issue {action.issue_number}")

    @staticmethod
    def _is_publish_failure_history(entry: SessionHistoryEntry, issue_number: int) -> bool:
        if entry.issue_number != issue_number:
            return False
        if entry.status not in {"blocked", "failed"}:
            return False
        reason = (entry.status_reason or "").strip().lower()
        if not reason:
            return False
        return (
            "push or pr creation failed" in reason
            or "publishing failed" in reason
            or "publish failed" in reason
        )
