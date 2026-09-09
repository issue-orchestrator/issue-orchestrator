"""Read-only remote gates using the same identity policy as exact publication."""

from dataclasses import replace

import pytest

from issue_orchestrator.domain.publication_remote import PublicationPrState, PublicationRemoteError
from issue_orchestrator.domain.validated_head_publication import RemoteHeadExpectation
from issue_orchestrator.domain.validated_work import DispositionPhase, ValidatedWorkFailure
from issue_orchestrator.execution.publication_verifier import RemotePublicationVerifier
from tests.unit.test_git_validated_head_executor import setup as setup


def test_verified_baseline_does_not_publish_or_require_a_pr(setup):
    rig, remote, _, command = setup
    verifier = RemotePublicationVerifier(remote)
    result = verifier.before_publication(command, DispositionPhase.PRE_SUBMISSION)
    assert result.verified and result.branch_head == rig.base and result.pull_request is None
    assert not verifier.confirm_target(command).verified
    assert remote.created == 0
    assert remote.read_branch(command) == rig.base


@pytest.mark.parametrize("state", [PublicationPrState.CLOSED, PublicationPrState.MERGED])
def test_recorded_closed_or_merged_pr_refuses_before_branch_write(setup, state):
    rig, remote, _, command = setup
    pr = remote.add_pr(command, state=state)
    command = replace(command, pr_number=pr.number)
    result = RemotePublicationVerifier(remote).before_publication(command, DispositionPhase.PRE_SUBMISSION)
    assert result.failure is ValidatedWorkFailure.PR_CLOSED_OR_MERGED
    assert remote.read_branch(command) == rig.base
    assert remote.created == 0


@pytest.mark.parametrize("mutation", ["foreign", "unmarked", "duplicate", "missing-recorded"])
def test_ambiguous_pr_refuses_without_adoption(setup, mutation):
    rig, remote, _, command = setup
    pr = remote.add_pr(command)
    if mutation == "foreign":
        remote.prs[0] = replace(pr, head_repo="other/repo")
    elif mutation == "unmarked":
        remote.prs[0] = replace(pr, body="An unrelated PR")
    elif mutation == "duplicate":
        remote.add_pr(command)
    else:
        command = replace(command, pr_number=99)
    result = RemotePublicationVerifier(remote).before_publication(command, DispositionPhase.PRE_SUBMISSION)
    assert not result.verified
    assert remote.read_branch(command) == rig.base
    assert remote.created == 0


def test_recorded_pr_does_not_need_retrofitted_attribution(setup):
    _, remote, _, command = setup
    pr = remote.add_pr(command, body="Existing recorded PR")
    result = RemotePublicationVerifier(remote).before_publication(replace(command, pr_number=pr.number), DispositionPhase.PRE_SUBMISSION)
    assert result.verified


def test_target_is_acceptable_for_reconciliation_but_not_fresh_submission(setup):
    rig, remote, executor, command = setup
    assert executor.publish_or_reconcile(command).pr_head_sha == rig.target
    verifier = RemotePublicationVerifier(remote)
    assert not verifier.before_publication(command, DispositionPhase.PRE_SUBMISSION).verified
    assert verifier.before_publication(command, DispositionPhase.RECONCILING).verified
    assert verifier.confirm_target(command).verified
    assert remote.created == 1


def test_unavailable_is_not_absence(setup):
    _, remote, _, command = setup
    remote.read_error = True
    command = replace(command, expectation=RemoteHeadExpectation.ABSENT, expected_remote_head_sha=None)
    result = RemotePublicationVerifier(remote).before_publication(command, DispositionPhase.PRE_SUBMISSION)
    assert result.failure is ValidatedWorkFailure.REMOTE_UNREADABLE
    assert not result.verified


def test_remote_advance_during_read_is_not_a_consistent_observation(setup):
    rig, remote, _, command = setup
    remote.on_branch_read = lambda: rig.run("push", "origin", f"{rig.target}:refs/heads/feature")
    result = RemotePublicationVerifier(remote).before_publication(command, DispositionPhase.PRE_SUBMISSION)
    assert result.failure is ValidatedWorkFailure.PUBLISH_TARGET_MISMATCH
    assert remote.created == 0


def test_lagging_pr_observation_cannot_confirm_target(setup):
    rig, remote, executor, command = setup
    executor.publish_or_reconcile(command)
    remote.stale_pr_head = rig.base
    result = RemotePublicationVerifier(remote).confirm_target(command)
    assert result.failure is ValidatedWorkFailure.PUBLISH_TARGET_MISMATCH
