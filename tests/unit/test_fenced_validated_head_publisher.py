"""Real retained claims and local Git across the two publication authority checks."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.fenced_validated_head_publisher import FencedValidatedHeadPublisher
from issue_orchestrator.control.validated_work_effects import FencedValidatedWorkEffects
from issue_orchestrator.domain.validated_head_publication import (
    BranchWriteStatus, PrEnsureStatus, RemoteHeadExpectation, SupersededStage,
    compose_publication_outcome,
)
from issue_orchestrator.domain.validated_work import PublishValidatedHeadStatus
from issue_orchestrator.domain.validated_work_execution import (
    RecordExecutionBusy, ValidatedWorkAuthorityUnavailable,
)
from issue_orchestrator.execution.validated_work_execution import LocalValidatedWorkExecutionOwner
from issue_orchestrator.ports.validated_head_publication import ValidatedHeadExecutor
from issue_orchestrator.ports.validated_work_store import ValidatedWorkFence
from tests.unit.validated_work_support import Rig, begin, capture, claim
from tests.unit.test_validated_head_publication import branch_result, pr_result, command
from tests.unit.test_git_validated_head_executor import setup as git_setup


@contextmanager
def publication(path, cmd, *, executor=None, fence=None):
    store = Rig(path / "work.sqlite").open()
    admission = capture(cmd.target_head_sha, expected=cmd.expected_remote_head_sha,
                        branch=cmd.branch_name, issue=cmd.issue_number, pr=cmd.pr_number)
    store.admit(admission)
    execution = LocalValidatedWorkExecutionOwner(store)
    lease = execution.try_enter(admission.evidence.record_id)
    assert not isinstance(lease, RecordExecutionBusy)
    with lease as token:
        held = claim(store, admission)
        execution.remember_claim(token, held)
        assert begin(store, held) is not None
        executor = executor or Mock(spec=ValidatedHeadExecutor)
        authority = FencedValidatedWorkEffects(execution=execution, fence=fence or store)
        publisher = FencedValidatedHeadPublisher(executor, authority)
        yield store, execution, token, held, executor, publisher


@pytest.mark.parametrize("state", list(BranchWriteStatus))
def test_composes_only_real_stage_results_and_retains_lease(tmp_path, state):
    cmd = command()
    with publication(tmp_path, cmd) as (store, execution, token, held, executor, publisher):
        branch = branch_result(state)
        pr = pr_result(PrEnsureStatus.CREATED) if branch.at_target else None
        executor.push_validated_head.return_value = branch
        executor.ensure_pull_request.return_value = pr
        assert publisher.publish(token, held, cmd) == compose_publication_outcome(branch, pr)
        assert executor.ensure_pull_request.call_count == int(branch.at_target)
        executor.publish_or_reconcile.assert_not_called()
        assert isinstance(execution.try_enter(held.record_id), RecordExecutionBusy)
        assert store.holds_claim(held)
        assert store.publish_attempts(held.record_id)[0].outcome is None


@pytest.mark.parametrize("when", ["before", "between"])
def test_lost_claim_stops_next_effect_without_inventing_pr(tmp_path, when):
    cmd = command()
    with publication(tmp_path, cmd) as (store, execution, token, held, executor, publisher):
        branch = branch_result(BranchWriteStatus.PUSHED)
        if when == "before":
            assert execution.relinquish(token)
        else:
            def push(_command):
                assert store.relinquish_claim(held)
                return branch
            executor.push_validated_head.side_effect = push
        outcome = publisher.publish(token, held, cmd)
        assert outcome.status is PublishValidatedHeadStatus.SUPERSEDED
        assert outcome.superseded_stage is (
            SupersededStage.BEFORE_BRANCH_WRITE if when == "before"
            else SupersededStage.BETWEEN_STEPS
        )
        assert outcome.observed_remote_head_sha == (None if when == "before" else cmd.target_head_sha)
        assert outcome.push_outcome == (None if when == "before" else branch.push_outcome)
        assert outcome.pr_number is None and outcome.pr_head_sha is None and outcome.pr_url is None
        executor.ensure_pull_request.assert_not_called()
        assert executor.push_validated_head.call_count == int(when == "between")


@pytest.mark.parametrize("changes", [
    {"issue_number": 2}, {"repo_slug": "someone/else"}, {"branch_name": "other"},
    {"target_head_sha": "b" * 40}, {"expectation": RemoteHeadExpectation.UNCONSTRAINED},
])
def test_command_cannot_repurpose_claim_or_use_manual_expectation(tmp_path, changes):
    cmd = command()
    with publication(tmp_path, cmd) as (_store, _execution, token, held, executor, publisher):
        with pytest.raises(ValueError):
            publisher.publish(token, held, replace(cmd, **changes))
        assert not executor.mock_calls


@pytest.mark.parametrize("when", ["before", "between"])
def test_unknown_authority_preserves_unfinished_attempt_and_dispatches_no_next_step(tmp_path, when):
    cmd = command()
    fence = Mock(spec=ValidatedWorkFence)
    fence.holds_claim.side_effect = (
        [OSError("database unreadable")] if when == "before"
        else [True, OSError("database unreadable")]
    )
    with publication(tmp_path, cmd, fence=fence) as (store, execution, token, held, executor, publisher):
        executor.push_validated_head.return_value = branch_result(BranchWriteStatus.PUSHED)
        with pytest.raises(ValidatedWorkAuthorityUnavailable, match="database unreadable"):
            publisher.publish(token, held, cmd)
        executor.ensure_pull_request.assert_not_called()
        assert executor.push_validated_head.call_count == int(when == "between")
        assert store.publish_attempts(held.record_id)[0].outcome is None
        assert execution.claim(token) is held


def test_expired_execution_token_cannot_publish(tmp_path):
    cmd = command()
    with publication(tmp_path, cmd) as (_store, _execution, token, held, executor, publisher):
        pass
    result = publisher.publish(token, held, cmd)
    assert result.superseded_stage is SupersededStage.BEFORE_BRANCH_WRITE
    assert not executor.mock_calls


def test_real_existing_pr_receives_exact_validated_commit_through_fenced_path(tmp_path, git_setup):
    rig, remote, executor, cmd = git_setup
    remote.add_pr(cmd)
    with publication(tmp_path, cmd, executor=executor) as (store, _execution, token, held, _executor, publisher):
        outcome = publisher.publish(token, held, cmd)
        assert outcome.status is PublishValidatedHeadStatus.PUBLISHED
        assert outcome.pr_head_sha == rig.target
        assert remote.read_branch(cmd) == rig.target
        assert rig.run("rev-parse", "HEAD") == rig.tip
        assert remote.created == 0
        assert store.publish_attempts(held.record_id)[0].outcome is None


def test_copied_claim_cannot_use_retained_execution_authority(tmp_path):
    cmd = command()
    with publication(tmp_path, cmd) as (_store, _execution, token, held, executor, publisher):
        outcome = publisher.publish(token, replace(held), cmd)
        assert outcome.superseded_stage is SupersededStage.BEFORE_BRANCH_WRITE
        assert not executor.mock_calls


def test_other_execution_owners_token_is_not_authority(tmp_path):
    cmd = command()
    with publication(tmp_path, cmd) as (store, _execution, _token, held, executor, publisher):
        other = LocalValidatedWorkExecutionOwner(store)
        lease = other.try_enter(held.record_id)
        assert not isinstance(lease, RecordExecutionBusy)
        with lease as token:
            other.remember_claim(token, held)
            outcome = publisher.publish(token, held, cmd)
            assert outcome.superseded_stage is SupersededStage.BEFORE_BRANCH_WRITE
        assert not executor.mock_calls


def test_real_landed_push_with_lost_claim_never_creates_pr(tmp_path, git_setup):
    rig, remote, executor, cmd = git_setup
    boundary = Mock(spec=ValidatedHeadExecutor, wraps=executor)
    with publication(tmp_path, cmd, executor=boundary) as (store, _execution, token, held, _executor, publisher):
        def push_then_lose_claim(command):
            result = executor.push_validated_head(command)
            assert store.relinquish_claim(held)
            return result
        boundary.push_validated_head.side_effect = push_then_lose_claim
        outcome = publisher.publish(token, held, cmd)
        assert outcome.status is PublishValidatedHeadStatus.SUPERSEDED
        assert outcome.superseded_stage is SupersededStage.BETWEEN_STEPS
        assert outcome.observed_remote_head_sha == rig.target
        assert remote.read_branch(cmd) == rig.target
        assert remote.created == 0
        assert outcome.pr_number is None
        boundary.ensure_pull_request.assert_not_called()
        assert store.publish_attempts(held.record_id)[0].outcome is None
