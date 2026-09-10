"""Real retained custody, Git remote, and SQLite attempt ordering/replay."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from issue_orchestrator.control.fenced_validated_head_publisher import FencedValidatedHeadPublisher
from issue_orchestrator.control.recovery_publication_attempt import RecoveryPublicationAttempt
from issue_orchestrator.control.validated_work_effects import FencedValidatedWorkEffects
from issue_orchestrator.domain.published_work_finalization import PublishedWorkTarget
from issue_orchestrator.domain.recovery_attempt import RecoveryAttemptPending
from issue_orchestrator.domain.validated_work import ValidatedWorkState, PublishValidatedHeadStatus
from issue_orchestrator.domain.publication_remote import PublicationPullRequest, PublicationPrState
from issue_orchestrator.domain.validated_work_capture import ValidatedWorkRemoteFacts
from issue_orchestrator.domain.validated_work_execution import RecordExecutionBusy
from issue_orchestrator.execution.git_validated_head_executor import GitValidatedHeadExecutor
from issue_orchestrator.execution.publication_verifier import RemotePublicationVerifier
from issue_orchestrator.execution.validated_work_execution import LocalValidatedWorkExecutionOwner
from issue_orchestrator.execution.validated_work_ancestry import GitValidatedWorkAncestry
from issue_orchestrator.infra.validated_work_store import SqliteValidatedWorkStore
from tests.unit.control.test_retained_completion_preparation import retained as retained, prepare
from tests.unit.test_git_validated_head_executor import Remote
from tests.unit.validated_work_support import Liveness
from tests.unit.test_validated_work_preservation import custody as custody


class RecoveryRemote(Remote):
    def add_pr(self, command, **kwargs):
        return super().add_pr(command, url=f"https://example.invalid/{command.repo_slug}/pull/{len(self.prs) + 1}", **kwargs)


@pytest.fixture
def publication(retained):
    head = retained.git.head_sha(retained.worktree)
    retained.observer.observe.return_value = ValidatedWorkRemoteFacts(None, tuple(
        PublicationPullRequest(
            number, f"https://example.invalid/owner/repo/pull/{number}",
            "owner/repo", "owner/repo", "feature", "main", head,
            PublicationPrState.OPEN, "duplicate fixture",
        )
        for number in (10, 11)
    ))
    rig = prepare(retained)
    prepared = rig.owner.prepare(rig.row, rig.workspace, "Recovered feature")
    retained.remote = retained.repo.parent / "remote.git"
    retained.git.run(retained.repo, ["clone", "--bare", str(retained.repo), str(retained.remote)])
    retained.git.run(retained.remote, ["update-ref", "-d", "refs/heads/feature"])
    retained.git.run(retained.repo, ["remote", "add", "origin", str(retained.remote)])
    store = SqliteValidatedWorkStore(retained.state / "work.sqlite",
        ancestry=GitValidatedWorkAncestry(repository=retained.repo, repo_slug="owner/repo", git=retained.wc),
        artifacts=retained.escrow, retention=retained.escrow, liveness=Liveness())
    execution = LocalValidatedWorkExecutionOwner(store)
    effects = FencedValidatedWorkEffects(execution=execution, fence=store)
    remote = RecoveryRemote(retained)
    publisher = FencedValidatedHeadPublisher(GitValidatedHeadExecutor(retained.wc, remote), effects)
    verifier = RemotePublicationVerifier(remote)
    worker = RecoveryPublicationAttempt(store=store, effects=effects, publisher=publisher, verifier=verifier)
    return SimpleNamespace(custody=retained, prepared=prepared, store=store, execution=execution,
        effects=effects, remote=remote, publisher=publisher, verifier=verifier, worker=worker,
        authority=rig.row.authority, preparation=rig.owner)


@contextmanager
def held(rig):
    key = rig.prepared.workspace.record_id
    lease = rig.execution.try_enter(key)
    assert not isinstance(lease, RecordExecutionBusy)
    with lease as token:
        claim = rig.store.acquire_claim(key, expected_states=frozenset({ValidatedWorkState.PARKED, ValidatedWorkState.PUBLISHING}),
            evidence_id=rig.prepared.workspace.evidence_id)
        assert claim is not None
        rig.execution.remember_claim(token, claim)
        try:
            yield token, claim
        finally:
            assert rig.execution.relinquish(token)


def test_exact_publication_records_success_before_read_only_finalization_resume(publication):
    rig = publication
    with held(rig) as (token, claim):
        result = rig.worker.advance(token, claim, rig.prepared, approved=rig.authority)
        assert isinstance(result, PublishedWorkTarget)
        assert result.key.validated_head_sha == rig.remote.read_branch(rig.prepared.command)
        attempts = rig.store.publish_attempts(claim.record_id)
        assert len(attempts) == 1 and attempts[0].succeeded
        assert rig.store.get(claim.record_id).pr_number == result.pr_number
        assert rig.worker.advance(token, claim, rig.prepared) == result
        assert rig.store.publish_attempts(claim.record_id) == attempts
        assert rig.remote.created == 1
        assert rig.store.holds_claim(claim)
        assert isinstance(rig.execution.try_enter(claim.record_id), RecordExecutionBusy)
    assert not rig.custody.worktree.exists()


def test_parked_work_cannot_publish_without_exact_approval(publication):
    rig = publication
    with held(rig) as (token, claim):
        result = rig.worker.advance(token, claim, rig.prepared)
        assert isinstance(result, RecoveryAttemptPending)
        assert rig.store.publish_attempts(claim.record_id) == ()
        assert rig.remote.read_branch(rig.prepared.command) is None
        assert rig.remote.created == 0


def test_lost_outcome_needs_a_successor_and_keeps_original_attempt(publication):
    rig = publication
    class LostOutcome(FencedValidatedHeadPublisher):
        def publish(self, token, claim, command):
            super().publish(token, claim, command)
            raise OSError("process interrupted after publication")
    interrupted = RecoveryPublicationAttempt(store=rig.store, effects=rig.effects,
        publisher=LostOutcome(GitValidatedHeadExecutor(rig.custody.wc, rig.remote), rig.effects), verifier=rig.verifier)
    with held(rig) as (token, claim):
        with pytest.raises(OSError, match="interrupted"):
            interrupted.advance(token, claim, rig.prepared, approved=rig.authority)
        first = rig.store.publish_attempts(claim.record_id)[0]
        assert first.outcome is None
        assert isinstance(rig.worker.advance(token, claim, rig.prepared), RecoveryAttemptPending)
        assert rig.store.publish_attempts(claim.record_id) == (first,)
    with held(rig) as (token, claim):
        target = rig.worker.advance(token, claim, rig.prepared)
        assert isinstance(target, PublishedWorkTarget)
        first, second = rig.store.publish_attempts(claim.record_id)
        assert first.outcome is None
        assert second.outcome is PublishValidatedHeadStatus.ALREADY_AT_TARGET
        assert second.fence > first.fence
        assert rig.remote.created == 1


def test_unreadable_remote_does_not_start_or_spend_an_attempt(publication):
    rig = publication
    rig.remote.read_error = True
    with held(rig) as (token, claim):
        result = rig.worker.advance(token, claim, rig.prepared, approved=rig.authority)
        assert isinstance(result, RecoveryAttemptPending)
        assert rig.store.publish_attempts(claim.record_id) == ()
        assert rig.remote.created == 0


def test_completed_attempt_never_republishes_a_branch_that_moved(publication):
    rig = publication
    with held(rig) as (token, claim):
        target = rig.worker.advance(token, claim, rig.prepared, approved=rig.authority)
        assert isinstance(target, PublishedWorkTarget)
        before = rig.store.publish_attempts(claim.record_id)
        base = rig.custody.git.head_sha(rig.custody.repo)
        rig.custody.git.run(rig.custody.remote, ["update-ref", "refs/heads/feature", base])
        result = rig.worker.advance(token, claim, rig.prepared)
        assert isinstance(result, RecoveryAttemptPending)
        assert rig.store.publish_attempts(claim.record_id) == before
        assert rig.remote.read_branch(rig.prepared.command) == base
        assert rig.remote.created == 1
