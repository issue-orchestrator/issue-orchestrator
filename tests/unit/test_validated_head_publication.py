"""The publication algebra preserves effects and refuses invented stage facts."""

from dataclasses import replace
from pathlib import Path

import pytest

from issue_orchestrator.domain.exact_git import ExactPushOutcome
from issue_orchestrator.domain.validated_head_publication import (
    BranchWriteOutcome,
    BranchWriteStatus,
    PrEnsureOutcome,
    PrEnsureStatus,
    PullRequestAttribution,
    PublishValidatedHeadCommand,
    PublicationContent,
    RemoteHeadExpectation,
    SupersededStage,
    compose_publication_outcome,
    superseded_outcome,
)
from issue_orchestrator.domain.validated_work import (
    PublishValidatedHeadStatus,
    ValidatedWorkFailure,
)

SHA = "a" * 40


def command():
    return PublishValidatedHeadCommand(
        1,
        "owner/repo",
        "feature",
        SHA,
        RemoteHeadExpectation.ABSENT,
        None,
        Path("/repo"),
        None,
        "main",
        PublicationContent("#1: Feature", "Closes #1\n\nImplementation details", True),
    )


def branch_result(status):
    success = status in {BranchWriteStatus.PUSHED, BranchWriteStatus.ALREADY_AT_TARGET}
    return BranchWriteOutcome(
        status,
        SHA,
        ExactPushOutcome.PUSHED if status is BranchWriteStatus.PUSHED else None,
        None if success else ValidatedWorkFailure.PUSH_FAILED,
        "branch",
    )


def pr_result(status):
    failure = status in {PrEnsureStatus.REFUSED, PrEnsureStatus.TRANSIENT_FAILURE}
    return PrEnsureOutcome(
        status,
        3,
        "https://example/pull/3",
        SHA,
        ValidatedWorkFailure.REMOTE_UNREADABLE if failure else None,
        "pr",
        PullRequestAttribution.CREATED,
    )


@pytest.mark.parametrize("branch_status", list(BranchWriteStatus))
@pytest.mark.parametrize("pr_status", [None, *PrEnsureStatus])
def test_total_composition_matrix(branch_status, pr_status):
    branch = branch_result(branch_status)
    pr = pr_result(pr_status) if pr_status else None
    if branch.at_target != (pr is not None):
        with pytest.raises(ValueError):
            compose_publication_outcome(branch, pr)
        return
    outcome = compose_publication_outcome(branch, pr)
    assert outcome.observed_remote_head_sha == branch.observed_remote_head_sha
    assert outcome.push_outcome is branch.push_outcome
    assert outcome.pr_number == (pr.pr_number if pr else None)
    assert outcome.pr_url == (pr.pr_url if pr else None)
    assert outcome.pr_head_sha == (pr.pr_head_sha if pr else None)
    assert outcome.pr_attribution is (
        pr.attribution if pr else PullRequestAttribution.NONE
    )
    if pr_status is PrEnsureStatus.REFUSED:
        expected = PublishValidatedHeadStatus.REJECTED
    elif pr_status is PrEnsureStatus.TRANSIENT_FAILURE:
        expected = PublishValidatedHeadStatus.TRANSIENT_FAILURE
    elif branch_status is BranchWriteStatus.PUSHED:
        expected = PublishValidatedHeadStatus.PUBLISHED
    else:
        expected = PublishValidatedHeadStatus(branch_status.value)
    assert outcome.status is expected
    assert outcome.failure == (pr.failure if pr else branch.failure)


@pytest.mark.parametrize("stage", list(SupersededStage))
@pytest.mark.parametrize("branch_status", [None, *BranchWriteStatus])
def test_supersession_stage_matrix(stage, branch_status):
    branch = branch_result(branch_status) if branch_status else None
    valid = (stage is SupersededStage.BEFORE_BRANCH_WRITE and branch is None) or (
        stage is SupersededStage.BETWEEN_STEPS
        and branch is not None
        and branch.at_target
    )
    if not valid:
        with pytest.raises(ValueError):
            superseded_outcome(stage, branch)
        return
    outcome = superseded_outcome(stage, branch)
    assert outcome.status is PublishValidatedHeadStatus.SUPERSEDED
    assert outcome.superseded_stage is stage
    assert outcome.push_outcome is (branch.push_outcome if branch else None)
    assert outcome.observed_remote_head_sha == (SHA if branch else None)
    assert outcome.pr_number is outcome.pr_url is outcome.pr_head_sha is None
    assert outcome.pr_attribution is PullRequestAttribution.NONE
    assert outcome.attributable_pr_number is None


def test_observed_only_pr_is_not_attributable_for_cleanup():
    pr = replace(
        pr_result(PrEnsureStatus.REFUSED),
        attribution=PullRequestAttribution.NONE,
    )
    outcome = compose_publication_outcome(branch_result(BranchWriteStatus.PUSHED), pr)
    assert outcome.pr_number == 3
    assert outcome.attributable_pr_number is None


def test_successful_pr_requires_attribution():
    with pytest.raises(ValueError, match="requires attribution"):
        replace(
            pr_result(PrEnsureStatus.CREATED),
            attribution=PullRequestAttribution.NONE,
        )


@pytest.mark.parametrize("expectation", list(RemoteHeadExpectation))
@pytest.mark.parametrize("expected", [None, SHA])
def test_expectation_contract(expectation, expected):
    valid = (expectation is RemoteHeadExpectation.EXACT) == (expected is not None)
    if valid:
        replace(command(), expectation=expectation, expected_remote_head_sha=expected)
    else:
        with pytest.raises(ValueError):
            replace(
                command(), expectation=expectation, expected_remote_head_sha=expected
            )


@pytest.mark.parametrize("protected", ["main", "master"])
def test_publication_command_refuses_protected_target_branch(protected):
    with pytest.raises(ValueError, match="target branch is protected"):
        replace(command(), branch_name=protected)


def test_publication_command_allows_protected_base_branch():
    assert replace(command(), pr_base_branch="master").pr_base_branch == "master"


def test_successful_stages_cannot_name_different_commits():
    with pytest.raises(ValueError, match="same target"):
        compose_publication_outcome(
            branch_result(BranchWriteStatus.PUSHED),
            replace(pr_result(PrEnsureStatus.CREATED), pr_head_sha="b" * 40),
        )


@pytest.mark.parametrize(
    "status,push",
    [
        (BranchWriteStatus.PUSHED, None),
        (BranchWriteStatus.ALREADY_AT_TARGET, ExactPushOutcome.PUSHED),
    ],
)
def test_no_fabricated_push(status, push):
    with pytest.raises(ValueError):
        BranchWriteOutcome(status, SHA, push, None, "bad")
