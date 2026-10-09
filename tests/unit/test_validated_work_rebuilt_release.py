"""porchpin #262 end to end: releasing work rebuilt with rewritten history (#9092).

The same real-Git rig as #8137's republished-work tests. The issue's records
park as divergent heads; the work is republished on ``<branch>-r1`` as PR
#479, which is then rebased with a conflict resolved and merged, so its head
contains none of the parked heads. The scope sweep cannot release them
(#8137 leaves them parked). An approved ``release_validated_work`` does,
through the real abandonment owner and store, and ``recovery-pending`` comes
off. Nothing else changes.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.adapters.issue_disposition_gate import (
    FileIssueDispositionMutationGate,
)
from issue_orchestrator.control.actions import ReleaseValidatedWorkAction
from issue_orchestrator.control.action_results import ActionResultType
from issue_orchestrator.control.operator_validated_work_abandonment import (
    OperatorValidatedWorkAbandonment,
)
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.tech_lead_reset_retry import STALE_DOWNGRADE_MODE
from issue_orchestrator.control.tech_lead_validated_work_release import (
    TechLeadValidatedWorkReleaseExecutor,
)
from issue_orchestrator.control.validated_work_recovery_authority import (
    ValidatedWorkRecoveryAuthority,
)
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.domain.publication_remote import PublicationPrState
from issue_orchestrator.domain.recovery_drain import RecoveryDrainMode
from issue_orchestrator.domain.tech_lead_approval import ApprovalVerdict, ApprovalVerdictKind
from issue_orchestrator.domain.validated_work import (
    ResolutionKind,
    ValidatedWorkFailure,
    ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_release import ValidatedWorkRelease, bind_release
from issue_orchestrator.domain.validated_work_release_intent import ValidatedWorkReleaseIntent
from issue_orchestrator.events import EventName
from tests.unit.test_validated_work_published_rework import (
    ISSUE, RECOVERY_PENDING, REPO, _commit, _drain, _legacy_parked_records, _sweep, build_rig,
)
from tests.unit.test_validated_work_republished_elsewhere import ELSEWHERE, _republish

PROPOSAL = 485


@pytest.fixture
def rig(tmp_path):
    return build_rig(tmp_path)


def _rebuilt_with_rewritten_history(rig, make_session, monkeypatch, *, state=PublicationPrState.MERGED):
    """porchpin #262 today: PR #479 was rebased with a conflict resolved and
    merged, so its head carries none of the parked heads."""
    _, _, parked = _legacy_parked_records(rig, make_session, monkeypatch)
    _republish(rig)
    rig.git.run(rig.worktree, ["checkout", "-q", "--detach", "main"])
    _commit(rig.git, rig.worktree, "rebuilt", "the slice rebuilt with a conflict resolved")
    rig.git.run(rig.worktree, ["push", "-q", "--force", "origin", f"HEAD:refs/pull/{ELSEWHERE}/head"])
    rig.github.elsewhere[ELSEWHERE] = (f"{parked[0].key.branch_name}-r1", state)
    # The content proof (#8137) cannot release them: they stay parked.
    _drain(rig, _sweep(rig)).tick(OrchestratorState(), lambda: RecoveryDrainMode.ACTIVE)
    for disposition in parked:
        record = rig.store.get(disposition.record_id)
        assert (record.state, record.failure) == (
            ValidatedWorkState.PARKED, ValidatedWorkFailure.DIVERGENT_VALIDATED_HEADS)
    assert RECOVERY_PENDING in rig.labels.labels
    return parked


def _executor(rig) -> tuple[TechLeadValidatedWorkReleaseExecutor, ValidatedWorkRecoveryAuthority]:
    grants = ValidatedWorkRecoveryAuthority(rig.aggregate)
    abandonment = OperatorValidatedWorkAbandonment(
        repo_slug=REPO, store=rig.store, execution=rig.execution,
        gate=FileIssueDispositionMutationGate(rig.state), blocks=rig.aggregate, events=rig.events,
    )
    return TechLeadValidatedWorkReleaseExecutor(
        events=rig.events,
        grants=grants,
        pull_requests=rig.github,
        approval=lambda number: ApprovalVerdict(number, ApprovalVerdictKind.MAINTAINER, "operator", 9),
        abandon_all=abandonment.abandon_all,
        committed=abandonment.committed,
    ), grants


def _approved_release(grants, parked) -> ReleaseValidatedWorkAction:
    """What launch binds and approval runs: the parked records' launch grants."""
    intent = ValidatedWorkReleaseIntent(tuple(d.record_id for d in parked), ELSEWHERE)
    authorities = bind_release(intent, issue_number=ISSUE, grants=grants.release_grants_for((ISSUE,)))
    assert authorities is not None
    return ReleaseValidatedWorkAction(
        release=ValidatedWorkRelease(ELSEWHERE, authorities, "PR #479 rebuilt slice 2 after a conflicted rebase"),
        proposal_id="A1", anchor_issue_number=PROPOSAL, proposal_issue_number=PROPOSAL,
        expected=build_expected_for_mutation(),
    )


def test_an_approved_release_resolves_exactly_the_named_records_and_unblocks(rig, make_session, monkeypatch):
    parked = _rebuilt_with_rewritten_history(rig, make_session, monkeypatch)
    others = {d.record_id: d.state for d in rig.store.for_issue(ISSUE).dispositions
              if d.record_id not in {p.record_id for p in parked}}
    executor, grants = _executor(rig)

    result = executor.apply(_approved_release(grants, parked))

    assert result.success, result.details
    for disposition in parked:
        record = rig.store.record_for_id(disposition.record_id)
        assert record.disposition.state is ValidatedWorkState.ABANDONED
        assert record.resolution_kind is ResolutionKind.OPERATOR_ABANDONED
        resolution = record.disposition.resolution
        assert resolution is not None
        assert resolution.actor == f"approved by maintainer @operator on tech-lead proposal #{PROPOSAL}"
        assert f"merged PR #{ELSEWHERE}" in resolution.reason
    # Nothing else moved.
    assert {d.record_id: d.state for d in rig.store.for_issue(ISSUE).dispositions
            if d.record_id in others} == others
    assert not rig.store.has_unresolved_work(ISSUE)
    assert RECOVERY_PENDING not in rig.labels.labels
    audited = [event.data["record_id"] for event in rig.events.events
               if event.event_type is EventName.VALIDATED_WORK_ABANDONED]
    assert sorted(audited) == sorted(d.record_id for d in parked)


def test_a_superseding_pr_still_open_releases_nothing(rig, make_session, monkeypatch):
    parked = _rebuilt_with_rewritten_history(rig, make_session, monkeypatch, state=PublicationPrState.OPEN)
    executor, grants = _executor(rig)

    result = executor.apply(_approved_release(grants, parked))

    assert result.result_type is ActionResultType.SKIPPED
    assert result.details["mode"] == STALE_DOWNGRADE_MODE
    for disposition in parked:
        assert rig.store.get(disposition.record_id).state is ValidatedWorkState.PARKED
    assert RECOVERY_PENDING in rig.labels.labels


def test_a_retry_after_a_failed_reprojection_completes_the_release(rig, make_session, monkeypatch):
    """Review r1 F1 on the real store: the batch committed, the block's
    reprojection failed, and the retried approved op must complete the
    release (not close it stale, not leave recovery-pending on)."""
    parked = _rebuilt_with_rewritten_history(rig, make_session, monkeypatch)
    executor, grants = _executor(rig)
    action = _approved_release(grants, parked)
    real = rig.aggregate.reconcile_issue_block
    with monkeypatch.context() as broken:
        broken.setattr(rig.aggregate, "reconcile_issue_block",
                       lambda _issue: (_ for _ in ()).throw(OSError("label write lost")))
        with pytest.raises(OSError):
            executor.apply(action)
    assert rig.aggregate.reconcile_issue_block == real
    assert RECOVERY_PENDING in rig.labels.labels
    # Review r2 F1: GitHub may be unreadable by the retry; the commit stands.
    rig.github.unreadable = True

    result = executor.apply(action)

    assert result.success and result.details["replayed"] is True
    assert result.result_type is not ActionResultType.SKIPPED
    assert RECOVERY_PENDING not in rig.labels.labels
    for disposition in parked:
        assert rig.store.get(disposition.record_id).state is ValidatedWorkState.ABANDONED
