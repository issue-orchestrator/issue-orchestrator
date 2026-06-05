"""Owner for manual publish recovery of previously failed publish jobs."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..control.job_store import JobRecord, get_worktree_id
from ..control.actions import AddLabelAction, RemoveLabelAction
from ..domain.models import OrchestratorState, PublishJob, SessionHistoryEntry
from ..domain.pr_attempt_scope import scope_prs_to_active_issue_branch
from ..domain.session_run import SessionRunAssets
from ..ports.fresh_issue_reader import FreshIssueReader
from ..ports.pull_request_tracker import PRInfo

if TYPE_CHECKING:
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


class _RepositoryHost(Protocol):
    def get_issue(self, issue_number: int) -> Any: ...
    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]: ...


class _PublishExecutor(Protocol):
    def submit(self, job: PublishJob) -> bool: ...
    def get_job_history(self, issue_number: int | None = None, limit: int = 100) -> list[JobRecord]: ...
    def get_running_jobs(self) -> list[PublishJob]: ...


class _ActionApplier(Protocol):
    def apply(self, action: AddLabelAction | RemoveLabelAction) -> Any: ...


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
    job_record: JobRecord | None = None
    issue_title: str = ""
    agent_label: str | None = None


class PublishRecoveryService:
    """Backend owner for retrying or recovering publish-failed issues."""

    def __init__(
        self,
        repository_host: _RepositoryHost,
        publish_executor: _PublishExecutor,
        label_manager: "LabelManager",
        fresh_issue_reader: FreshIssueReader,
        action_applier: _ActionApplier,
    ) -> None:
        self._repository_host = repository_host
        self._publish_executor = publish_executor
        self._lm = label_manager
        self._fresh_issue_reader = fresh_issue_reader
        self._action_applier = action_applier

    def can_retry_publish(self, issue_number: int, state: OrchestratorState) -> bool:
        """Return whether retry-publish is currently available for an issue."""
        return self._retry_decision(issue_number, state).allowed

    def retry_publish(self, issue_number: int, state: OrchestratorState) -> RetryPublishResult:
        """Retry publish for a publish-failed issue or recover an already-created PR."""
        decision = self._retry_decision(issue_number, state)
        if not decision.allowed or decision.job_record is None:
            logger.info(
                "[publish-retry] Rejecting retry request for issue=%s reason=%s",
                issue_number,
                decision.reason,
            )
            return RetryPublishResult(status="rejected", message=decision.reason)

        existing_pr = self._matching_open_pr(issue_number, decision.job_record.branch_name)
        if existing_pr is not None:
            self._finalize_success(
                state=state,
                issue_number=issue_number,
                issue_title=decision.issue_title,
                agent_label=decision.agent_label,
                pr_url=existing_pr.url,
                pr_number=existing_pr.number,
                worktree_path=decision.job_record.worktree_path,
                history_reason="Recovered awaiting-merge state from existing PR",
            )
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

        job = self._rebuild_retry_job(
            decision.job_record,
            issue_title=decision.issue_title,
            agent_label=decision.agent_label,
        )
        submitted = self._publish_executor.submit(job)
        if not submitted:
            message = "A publish job is already active for this issue"
            logger.info(
                "[publish-retry] Skipping duplicate retry submit for issue=%s session_key=%s",
                issue_number,
                job.session_key,
            )
            return RetryPublishResult(status="rejected", message=message)

        state.pending_publish_jobs[job.job_id] = job
        logger.info(
            "[publish-retry] Submitted publish retry job=%s issue=%s branch=%s",
            job.job_id,
            issue_number,
            job.branch_name,
        )
        return RetryPublishResult(
            status="submitted",
            message="Publish retry queued",
            job_id=job.job_id,
        )

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

    def _retry_decision(self, issue_number: int, state: OrchestratorState) -> _RetryDecision:
        issue = self._repository_host.get_issue(issue_number)
        if issue is None:
            return _RetryDecision(False, f"Issue #{issue_number} not found")

        labels = tuple(self._current_labels(issue_number))
        block_reason = self._retry_state_block_reason(issue_number, state, labels)
        if block_reason:
            return _RetryDecision(False, block_reason)

        latest_job, job_reason = self._latest_failed_publish_job(issue_number)
        if latest_job is None:
            return _RetryDecision(False, job_reason or "No publish job history found for issue")

        job_reason = self._retry_job_record_block_reason(latest_job)
        if job_reason:
            return _RetryDecision(False, job_reason)

        meta, _ = self._job_metadata(latest_job)
        if meta is None:
            return _RetryDecision(False, "Publish job metadata unavailable")
        issue_title = str(getattr(issue, "title", "") or meta.get("issue_title") or f"Issue #{issue_number}")
        agent_label = self._resolve_agent_label(issue, meta)
        return _RetryDecision(
            True,
            "ok",
            job_record=latest_job,
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
        if any(job.issue_number == issue_number for job in state.pending_publish_jobs.values()):
            return "Issue already has a pending publish job"
        if any(job.issue_number == issue_number for job in self._publish_executor.get_running_jobs()):
            return "Issue already has a running publish job"
        return None

    def _latest_failed_publish_job(self, issue_number: int) -> tuple[JobRecord | None, str | None]:
        jobs = self._publish_executor.get_job_history(issue_number=issue_number, limit=100)
        if not jobs:
            return None, "No publish job history found for issue"

        latest_job = jobs[0]
        if latest_job.status != "failed":
            return None, f"Latest publish job is {latest_job.status}, not failed"
        return latest_job, None

    def _retry_job_record_block_reason(self, latest_job: JobRecord) -> str | None:
        meta, meta_error = self._job_metadata(latest_job)
        if meta is None:
            return meta_error or "Publish job metadata unavailable"

        worktree = Path(latest_job.worktree_path)
        if not worktree.exists():
            return "Retry worktree no longer exists"

        if latest_job.worktree_id:
            current_id = get_worktree_id(worktree)
            if current_id != latest_job.worktree_id:
                return "Retry worktree identity no longer matches"

        completion_rel = str(meta.get("completion_path") or "").strip()
        if not completion_rel:
            return "Publish job is missing completion_path metadata"

        completion_path = worktree / completion_rel
        if not completion_path.exists():
            return "Completion record for retry is missing"

        requested_actions = tuple(str(action) for action in (meta.get("requested_actions") or ()))
        if not requested_actions:
            return "Publish job is missing requested_actions metadata"
        return None

    def _job_metadata(self, job: JobRecord) -> tuple[dict[str, Any] | None, str | None]:
        if not job.metadata_json:
            return None, "Publish job metadata missing"
        try:
            metadata = json.loads(job.metadata_json)
        except json.JSONDecodeError:
            return None, "Publish job metadata is invalid JSON"
        if not isinstance(metadata, dict):
            return None, "Publish job metadata is malformed"
        return metadata, None

    def _rebuild_retry_job(
        self,
        job_record: JobRecord,
        *,
        issue_title: str,
        agent_label: str | None,
    ) -> PublishJob:
        metadata, error = self._job_metadata(job_record)
        if metadata is None:
            raise ValueError(error or "Publish job metadata missing")
        run_assets_raw = metadata.get("run_assets")
        if not isinstance(run_assets_raw, dict):
            raise ValueError("Publish job metadata missing run_assets")
        run_assets = SessionRunAssets.from_dict(run_assets_raw)

        return PublishJob(
            job_id=str(uuid.uuid4()),
            issue_number=job_record.issue_number,
            session_key=job_record.session_key,
            run_assets=run_assets,
            created_at=time.monotonic(),
            worktree_path=job_record.worktree_path,
            branch_name=job_record.branch_name,
            completion_path=str(metadata["completion_path"]),
            issue_title=issue_title,
            pr_number=job_record.pr_number,
            agent_label=agent_label,
            outcome=str(metadata.get("outcome") or ""),
            requested_actions=tuple(str(action) for action in metadata.get("requested_actions") or ()),
            retry_publish=True,
        )

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
                completed_at=datetime.now(),
            )
        )

    def _current_labels(self, issue_number: int) -> list[str]:
        return [str(label) for label in self._fresh_issue_reader.read_issue_labels(issue_number)]

    def _resolve_agent_label(self, issue: Any, metadata: dict[str, Any]) -> str | None:
        for label in getattr(issue, "labels", ()) or ():
            if str(label).startswith("agent:"):
                return str(label)
        agent_label = metadata.get("agent_label")
        if isinstance(agent_label, str) and agent_label.strip():
            return agent_label
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
