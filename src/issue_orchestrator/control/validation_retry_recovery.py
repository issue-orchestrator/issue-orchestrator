"""Single owner for re-queueing the validation retries a restart must resume.

A validation retry lives in two places: the queue entry in memory, and the
durable artifacts in the checkout the agent will resume. A restart loses the
first and has to rebuild it from the second, or the work is not merely delayed
-- startup worktree reconciliation then sees a checkout no session and no queue
entry claims, classifies it as an inactive disposable, and removes it WITH its
branch.

Two shapes of checkout hold a retry, and they are found in different ways:

- An ordinary coding retry sits in the worktree derived from its issue number,
  on a numeric branch git reports. The issue-branch scan finds it.
- A Tech Lead failure investigation runs on an UNPUSHED ``tech-lead-investigation``
  branch in a run-scoped disposable checkout under the focus issue's number, so
  neither the numeric branch scan nor the derived worktree path reaches it. Its
  branch exists nowhere else, so losing it loses the work (#7273).

Both live here so the rules cannot drift apart: one pass over the artifacts, one
queue owner call, one first-wins rule when both shapes name the same issue.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..domain.models import PendingValidationRetry
from ..domain.registered_completion import CompletionProcessingPolicy
from ..domain.tech_lead_scratch_identity import scratch_worktree_focus_issue
from ..infra.validation_state import ValidationRetryArtifacts, find_pending_retry_artifacts
from .worktree_manager import get_worktree_path

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from .worktree_reconciliation import StartupWorktreeReconciler

logger = logging.getLogger(__name__)


class ValidationRetryRecovery:
    """Rebuilds the pending-retry queue from what the checkouts still hold."""

    def __init__(
        self,
        config: "Config",
        worktree_reconciler: "StartupWorktreeReconciler",
        session_exists: Callable[[str], bool],
    ) -> None:
        self._config = config
        self._worktree_reconciler = worktree_reconciler
        self._session_exists = session_exists

    def recover(
        self, state: "OrchestratorState", issue_branches: dict[int, str]
    ) -> int:
        """Re-queue every resumable retry, and report how many were found."""
        claimed = {retry.issue_number for retry in state.pending_validation_retries}
        recovered = 0
        for issue_number, checkout, branch_name, kind in self._candidates(issue_branches):
            if issue_number in claimed:
                continue
            if self._session_exists(f"issue-{issue_number}"):
                logger.info(
                    "[startup] Validation retry already has a running session: issue=%d",
                    issue_number,
                )
                continue
            artifacts = find_pending_retry_artifacts(checkout)
            if artifacts is None or not artifacts.state.can_retry:
                continue
            state.replace_pending_validation_retry(
                self._queue_entry(issue_number, checkout, branch_name, artifacts)
            )
            claimed.add(issue_number)
            recovered += 1
            logger.info(
                "[startup] Recovered pending validation retry: issue=%d kind=%s "
                "retry_count=%d/%d authority_run=%s",
                issue_number,
                kind,
                artifacts.state.retry_count,
                artifacts.state.max_retries,
                artifacts.run.run_id if artifacts.run else None,
            )
        return recovered

    def _candidates(
        self, issue_branches: dict[int, str]
    ) -> list[tuple[int, Path, str, str]]:
        """Every checkout that could hold a retry, issue branches first.

        Issue branches are listed first so an ordinary checkout wins over an
        investigation checkout of the same focus issue: the ordinary one holds
        the issue's own work, and an investigation reads that issue as evidence.
        """
        candidates: list[tuple[int, Path, str, str]] = []
        for issue_number, branch_name in issue_branches.items():
            worktree_path = get_worktree_path(self._config, issue_number)
            if worktree_path.exists():
                candidates.append((issue_number, worktree_path, branch_name, "issue"))
        for item in self._worktree_reconciler.investigation_checkouts():
            focus = scratch_worktree_focus_issue(item.path.name)
            if focus is not None and item.branch is not None:
                candidates.append((focus, item.path, item.branch, "investigation"))
        return candidates

    def _queue_entry(
        self,
        issue_number: int,
        checkout: Path,
        branch_name: str,
        artifacts: ValidationRetryArtifacts,
    ) -> PendingValidationRetry:
        """The queue entry a relaunch reads, rebuilt from durable artifacts.

        ``authority_run`` carries the ORIGINAL run's identity, read back from its
        manifest. A Tech Lead completion is accepted only against the create-once
        authority row keyed by that run, so a resumed investigation that could not
        name it would be rejected as ``missing_authority`` -- pre-action, with the
        work pushed nowhere.

        Which retries name one is NOT decided here. The same
        ``CompletionProcessingPolicy`` that decides it on the live completion
        path decides it here, from the agent the run recorded. Deciding it
        locally -- "the manifest had an identity, so carry it" -- named a source
        run for every ordinary coder retry too, and the launcher hard-refuses a
        retry whose named authority has no row: every recovered coder retry
        would sit in the queue forever (round 1 finding 2).
        """
        state = artifacts.state
        policy = CompletionProcessingPolicy.for_unprocessed_session(
            artifacts.run_agent_label, self._config.tech_lead_review_agent
        )
        return PendingValidationRetry(
            issue_number=issue_number,
            issue_title=f"Issue #{issue_number}",  # The full title is not on disk
            agent_label="",  # Determined when launching
            worktree_path=str(checkout),
            branch_name=branch_name,
            original_prompt=self._retry_prompt(artifacts),
            validation_error=state.last_error or "Unknown validation error",
            validation_error_file=state.last_error_file,
            retry_count=state.retry_count,
            source_task=artifacts.source_task,
            validation_cmd=state.validation_cmd,
            authority_run=policy.inheritable_launch_authority(artifacts.run),
        )

    @staticmethod
    def _retry_prompt(artifacts: ValidationRetryArtifacts) -> str | None:
        """The prompt the agent was given, when the run still has it on disk."""
        if artifacts.retry_prompt_path is None:
            return None
        try:
            return artifacts.retry_prompt_path.read_text()
        except OSError:
            return None


__all__ = ["ValidationRetryRecovery"]
