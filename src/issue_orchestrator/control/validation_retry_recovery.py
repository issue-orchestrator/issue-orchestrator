"""Single owner for re-queueing the validation retries a restart must resume.

A validation retry lives in two places: the queue entry in memory, and the
durable artifacts in the checkout the agent will resume. A restart loses the
first and has to rebuild it from the second, or the work is not merely delayed
-- startup worktree reconciliation then sees a checkout no session and no queue
entry claims, classifies it as an inactive disposable, and removes it WITH its
branch.

Two shapes of checkout hold a retry, and they are found in different ways:

- An ordinary coding retry sits in the worktree derived from its issue number.
  It is commonly LOCAL-ONLY, because validation runs before the first push, so
  the registered-checkout inventory is what finds it; the remote issue-branch
  scan stays as a compatibility path for legacy checkouts with no ownership
  marker.
- A Tech Lead failure investigation runs on an UNPUSHED ``tech-lead-investigation``
  branch in a run-scoped disposable checkout under the focus issue's number, so
  neither the numeric branch scan nor the derived worktree path reaches it. Its
  branch exists nowhere else, so losing it loses the work (#7273).

Both live here so the rules cannot drift apart: one pass over the artifacts and
one queue-owner call. When both shapes name the same issue, the INVESTIGATION
wins: reconciliation may delete its disposable, never-pushed branch, while the
ordinary checkout stays under the normal review-gated lifecycle.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..domain.models import PendingValidationRetry
from ..infra.validation_state import ValidationRetryArtifacts, find_pending_retry_artifacts
from .recovered_run_identity import registered_run
from .worktree_manager import get_worktree_path

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports.issue_run_evidence import IssueRunLedger
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .worktree_reconciliation import StartupWorktreeReconciler

logger = logging.getLogger(__name__)


class ValidationRetryRecovery:
    """Rebuilds the pending-retry queue from what the checkouts still hold."""

    def __init__(
        self,
        config: "Config",
        worktree_reconciler: "StartupWorktreeReconciler",
        session_exists: Callable[[str], bool],
        issue_run_ledger: "IssueRunLedger | None" = None,
        tech_lead_authority: "TechLeadAuthorityStore | None" = None,
    ) -> None:
        self._config = config
        self._worktree_reconciler = worktree_reconciler
        self._session_exists = session_exists
        self._issue_run_ledger = issue_run_ledger
        self._tech_lead_authority = tech_lead_authority

    def recover(
        self, state: "OrchestratorState", issue_branches: dict[int, str]
    ) -> int:
        """Re-queue every resumable retry, and report how many were found."""
        claimed = {retry.issue_number for retry in state.pending_validation_retries}
        recovered = 0
        for issue_number, checkout, branch_name, kind in self._candidates(issue_branches):
            if issue_number in claimed:
                continue
            # A live terminal is named for the ISSUE, and both checkout shapes
            # share the issue number. Skipping on the name alone let a restored
            # ordinary session suppress an investigation retry in a different
            # checkout -- and reconciliation then saw activity evidence only for
            # the ordinary one and deleted the scratch branch (round 10
            # finding 2). Identity here is the CHECKOUT.
            session_name = f"issue-{issue_number}"
            running = self._session_exists(session_name)
            active = next(
                (
                    session
                    for session in state.active_sessions
                    if session.terminal_id == session_name
                ),
                None,
            )
            if (
                running
                and active is not None
                and Path(active.worktree_path).resolve() == checkout.resolve()
            ):
                logger.info(
                    "[startup] Validation retry checkout already has a running "
                    "session: issue=%d",
                    issue_number,
                )
                continue
            artifacts = find_pending_retry_artifacts(checkout)
            if artifacts is None or not artifacts.state.can_retry:
                continue
            retry = self._queue_entry(issue_number, checkout, branch_name, artifacts)
            if running and active is None:
                # The terminal exists but no restored session names it, so this
                # retry is held rather than launched: queued keeps the
                # reconciliation hold, the error keeps it unlaunchable.
                retry = replace(
                    retry,
                    recovery_error=(
                        retry.recovery_error
                        or "a live issue terminal exists but startup could not "
                        "match it to a restored checkout"
                    ),
                )
            state.replace_pending_validation_retry(retry)
            claimed.add(issue_number)
            recovered += 1
            logger.info(
                "[startup] Recovered pending validation retry: issue=%d kind=%s "
                "retry_count=%d/%d authority_run=%s recovery_error=%s",
                issue_number,
                kind,
                artifacts.state.retry_count,
                artifacts.state.max_retries,
                state.pending_validation_retries[-1].authority_run,
                state.pending_validation_retries[-1].recovery_error,
            )
        return recovered

    def _candidates(
        self, issue_branches: dict[int, str]
    ) -> list[tuple[int, Path, str, str]]:
        """Every checkout that could hold a retry, INVESTIGATIONS first.

        The queue admits one retry per issue, so when both shapes expose retry
        artifacts for the same issue one of them loses the slot. I had this
        backwards: I ordered issue branches first, reasoning that the ordinary
        checkout holds the issue's own work. But losing the slot means
        reconciliation sees no activity evidence for the other checkout -- and
        for the investigation that means deleting a never-pushed branch with no
        second copy anywhere (round 8 finding 1).

        The claim that the loser is recoverable is only true if a later restart
        can still FIND it. Reading the ordinary shape from the registered
        inventory rather than from the remote issue branches is what makes that
        true for a retry whose branch was never pushed (round 9 finding 1).
        """
        registered = sorted(
            self._worktree_reconciler.validation_retry_checkouts(),
            key=lambda candidate: candidate[2] != "investigation",
        )
        candidates: list[tuple[int, Path, str, str]] = []
        seen: set[Path] = set()
        for issue_number, item, kind in registered:
            assert item.branch is not None
            candidates.append((issue_number, item.path, item.branch, kind))
            seen.add(item.path.resolve())
        # Compatibility path for a legacy ordinary checkout with no ownership
        # marker, which the registered inventory declines to claim but which does
        # have a published numeric branch.
        for issue_number, branch_name in issue_branches.items():
            worktree_path = get_worktree_path(self._config, issue_number)
            if worktree_path.exists() and worktree_path.resolve() not in seen:
                candidates.append((issue_number, worktree_path, branch_name, "issue"))
        return candidates

    def _queue_entry(
        self,
        issue_number: int,
        checkout: Path,
        branch_name: str,
        artifacts: ValidationRetryArtifacts,
    ) -> PendingValidationRetry:
        """The queue entry a relaunch reads, rebuilt from durable artifacts.

        The canonical directory key is joined to the ISSUE-RUN LEDGER, which
        supplies the allocation-owned role and the exact run identity, and the
        authority store then says whether the corresponding launch grant still
        exists. The agent-writable manifest supplies neither fact: one naming
        another retained run made a retry inherit that run's grant, and one
        naming a different role made a damaged investigation relaunch as
        ordinary work (round 3 finding 1).
        """
        state = artifacts.state
        recovered_run, recovery_error = registered_run(
            issue_number,
            checkout,
            artifacts,
            ledger=self._issue_run_ledger,
            authority=self._tech_lead_authority,
        )
        return PendingValidationRetry(
            issue_number=issue_number,
            issue_title=f"Issue #{issue_number}",  # The full title is not on disk
            # The role the run ACTUALLY had. Blank here fell through to the
            # focus issue's label in `_resolve_validation_retry_issue`, and for
            # an investigation that is the CODER's label -- so a recovered
            # investigation relaunched as ordinary coding work and the carried
            # authority was bypassed entirely (round 2 finding 1).
            agent_label=recovered_run.agent_label if recovered_run else "",
            worktree_path=str(checkout),
            branch_name=branch_name,
            original_prompt=self._retry_prompt(artifacts),
            validation_error=state.last_error or "Unknown validation error",
            validation_error_file=state.last_error_file,
            retry_count=state.retry_count,
            source_task=(
                recovered_run.source_task
                if recovered_run is not None
                else artifacts.source_task
            ),
            validation_cmd=state.validation_cmd,
            authority_run=(
                recovered_run.authority_run if recovered_run is not None else None
            ),
            recovery_error=recovery_error,
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
