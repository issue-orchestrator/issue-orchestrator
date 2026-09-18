"""Joining a recovered retry to the run the orchestrator actually allocated.

A run directory proves only that artifacts were written there. The manifest
INSIDE it is agent-writable, so reading authority-bearing facts out of it let an
edited one name another retained run and inherit that run's grant, or name a
different role and relaunch a damaged investigation as ordinary work (#7273
round 3 finding 1).

The durable issue-run ledger is what says a run was allocated, with which role
and which identity; the authority store then says whether its launch grant still
exists. This module owns that join, and the sentence an operator reads when it
does not hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.registered_completion import CompletionProcessingPolicy
from ..domain.session_key import TaskKind
from ..domain.session_run import SessionRunIdentity
from ..domain.tech_lead_session import TechLeadAuthorityKey

if TYPE_CHECKING:
    from ..domain.models import PendingValidationRetry
    from ..infra.validation_state import ValidationRetryArtifacts
    from ..ports.issue_run_evidence import IssueRunLedger
    from ..ports.tech_lead_authority import TechLeadAuthorityStore


@dataclass(frozen=True, slots=True)
class RecoveredRun:
    """What the DURABLE allocation says about the run a retry came from."""

    agent_label: str
    source_task: TaskKind
    authority_run: SessionRunIdentity | None





def registered_run(
    issue_number: int,
    checkout: Path,
    artifacts: "ValidationRetryArtifacts",
    *,
    ledger: "IssueRunLedger | None",
    authority: "TechLeadAuthorityStore | None",
) -> "tuple[RecoveredRun | None, str | None]":
    """Join a run-scoped retry to its exact orchestrator-owned allocation."""
    if artifacts.run_dir is None:
        # Legacy worktree-level retry state predates run allocation.
        return RecoveredRun("", artifacts.source_task, None), None
    if ledger is None or authority is None:
        return None, (
            "Validation retry recovery cannot verify its source because the "
            "durable issue-run ledger or tech-lead authority store is unavailable"
        )
    try:
        key = TechLeadAuthorityKey.from_run_dir(artifacts.run_dir)
    except ValueError as exc:
        return None, f"Validation retry run identity is damaged: {exc}"
    matches = tuple(
        record
        for record in ledger.recorded_runs(issue_number)
        if TechLeadAuthorityKey.from_identity(record.run.identity) == key
        and record.run.run_dir == artifacts.run_dir
        and record.run.worktree_path == checkout
    )
    if len(matches) != 1:
        return None, (
            f"Validation retry run {key.run_id}/{key.session_name} has "
            f"{len(matches)} exact durable allocation records; relaunch is refused"
        )
    record = matches[0]
    if record.agent_label is None or record.completion_task is None:
        return None, (
            f"Validation retry run {key.run_id}/{key.session_name} has no "
            "durably recorded completion role"
        )
    if record.session_key.task.is_review_only:
        return None, (
            f"Validation retry run {key.run_id}/{key.session_name} is "
            f"review-only work ({record.session_key.task.value})"
        )
    policy = CompletionProcessingPolicy(record.agent_label, record.completion_task)
    authority_run = policy.inheritable_launch_authority(record.run.identity)
    grant = authority.load(run_id=key.run_id, session_name=key.session_name)
    recovered = RecoveredRun(
        record.agent_label, record.session_key.task, authority_run
    )
    if authority_run is not None and grant is None:
        return recovered, (
            f"Validation retry run {key.run_id}/{key.session_name} is durably "
            "recorded as tech-lead work, but its original launch authority is "
            "missing"
        )
    if authority_run is None and grant is not None:
        return recovered, (
            f"Validation retry run {key.run_id}/{key.session_name} is durably "
            "recorded as ordinary work but owns a tech-lead authority row"
        )
    return recovered, None


def unlaunchable_recovery_refusal(retry: "PendingValidationRetry") -> str | None:
    """Why a recovered retry must not be launched, or ``None``.

    Asked BEFORE worktree preparation, and it leaves the queue entry untouched:
    the entry's checkout and problem-artifact holds are the only remaining
    protection for work that exists nowhere else. Spending an agent session on a
    retry whose completion the orchestrator is already guaranteed to reject
    protects nothing and burns the budget that would have retried it.
    """
    if retry.recovery_error is None:
        return None
    return f"Recovered validation retry is not launchable: {retry.recovery_error}"


__all__ = ["RecoveredRun", "registered_run", "unlaunchable_recovery_refusal"]
