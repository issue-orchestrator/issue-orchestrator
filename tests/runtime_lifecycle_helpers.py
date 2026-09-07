"""Explicit lifecycle bundle with controlled preservation-port responses for release tests."""

from unittest.mock import Mock
from issue_orchestrator.control.review_exchange_lifecycle import CoreIssueRuntimeOwners, IssueRuntimeLifecycleOwners
from issue_orchestrator.domain.issue_run_evidence import IssueRunEvidence, IssueRunEvidenceOrigin, IssueRunEvidenceStatus
from issue_orchestrator.domain.validated_work_commands import ValidatedWorkDispositionBatch
from issue_orchestrator.ports.issue_run_evidence import IssueRunEvidenceSource
from issue_orchestrator.ports.validated_work_preservation import ValidatedWorkPreservation


def runtime_owners(*, session_manager=None, active_sessions=None, pair_registry=None,
                   job_supervisor=None, publish_recovery=None, completion_intake=None):
    source = Mock(spec=IssueRunEvidenceSource)
    source.evidence_for_issue.side_effect = lambda issue: IssueRunEvidence(issue,
        IssueRunEvidenceStatus.NO_RUNS_RECORDED, (), IssueRunEvidenceOrigin.RUN_LEDGER, "2026-09-07")
    source.issue_numbers.return_value = ()
    preservation = Mock(spec=ValidatedWorkPreservation)
    def capture(command):
        if completion_intake is not None:
            completion_intake.close_and_drain(command.issue_number)
        return ValidatedWorkDispositionBatch.no_work(command.issue_number, "controlled preservation response")
    preservation.dispose_at_termination.side_effect = capture
    preservation.has_unresolved_work.return_value = False
    preservation.for_issue.side_effect = lambda issue: ValidatedWorkDispositionBatch.no_work(issue, "fixture")
    retry = publish_recovery
    if retry is None:
        retry = Mock()
        retry.has_active_retry.return_value = False
    return IssueRuntimeLifecycleOwners(CoreIssueRuntimeOwners(session_manager,
        [] if active_sessions is None else active_sessions, pair_registry, job_supervisor, retry),
        preservation, source, Mock())


def reset_snapshot(issue_number: int, busy: bool = False):
    """Explicit runtime port response for reset executor policy tests."""
    from issue_orchestrator.control.review_exchange_lifecycle import (
        IssueRuntimeActivity, IssueRuntimeOwnerKind, IssueRuntimeResetSnapshot,
    )
    return IssueRuntimeResetSnapshot(
        IssueRuntimeActivity(frozenset({IssueRuntimeOwnerKind.SESSIONS}) if busy else frozenset(), frozenset()),
        ValidatedWorkDispositionBatch.no_work(issue_number, "fixture"),
    )


def make_action_applier(*args, **kwargs):
    """Action tests bind an explicit preservation port through the real bundle."""
    from issue_orchestrator.control.action_applier import ActionApplier
    applier = ActionApplier(*args, **kwargs)
    applier.runtime_lifecycle = runtime_owners(session_manager=applier.sessions,
        pair_registry=applier.pair_registry, job_supervisor=applier.background_job_supervisor,
        publish_recovery=applier.publish_recovery, completion_intake=applier.completion_intake)
    return applier
