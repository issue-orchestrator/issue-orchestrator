"""Immutable publication intent and honest, independently composable stage facts."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .exact_git import ExactPushOutcome
from .validated_work import (
    PublishValidatedHeadStatus,
    ValidatedWorkFailure,
    require_positive,
    require_sha,
)


class RemoteHeadExpectation(StrEnum):
    EXACT = "exact"
    ABSENT = "absent"
    UNCONSTRAINED = "unconstrained"


@dataclass(frozen=True, slots=True)
class PublicationContent:
    title: str
    body: str
    draft: bool

    def __post_init__(self) -> None:
        if type(self.title) is not str or not self.title.strip():
            raise ValueError("publication title must be nonempty")
        if type(self.body) is not str or type(self.draft) is not bool:
            raise ValueError("publication content must have a body and typed draft state")


@dataclass(frozen=True, slots=True)
class PublishValidatedHeadCommand:
    issue_number: int
    repo_slug: str
    branch_name: str
    target_head_sha: str
    expectation: RemoteHeadExpectation
    expected_remote_head_sha: str | None
    source_workspace: Path
    pr_number: int | None
    pr_base_branch: str
    content: PublicationContent

    def __post_init__(self) -> None:
        if type(self.content) is not PublicationContent:
            raise ValueError("publication requires typed content")
        require_sha(self.target_head_sha)
        if type(self.expectation) is not RemoteHeadExpectation:
            raise ValueError("remote expectation must be typed")
        if (self.expectation is RemoteHeadExpectation.EXACT) != (
            self.expected_remote_head_sha is not None
        ):
            raise ValueError("exact expectation requires exactly one remote SHA")
        if self.expected_remote_head_sha is not None:
            require_sha(self.expected_remote_head_sha)
        if not self.source_workspace.is_absolute():
            raise ValueError("publication workspace must be absolute")
        require_positive(self.issue_number, "issue_number")
        if self.pr_number is not None:
            require_positive(self.pr_number, "pr_number")
        if len(self.repo_slug.split("/")) != 2 or not all(self.repo_slug.split("/")):
            raise ValueError("repository must be owner/name")
        for branch in (self.branch_name, self.pr_base_branch):
            if (
                type(branch) is not str
                or not branch
                or branch == "@"
                or branch.startswith(("-", "refs/"))
                or branch.endswith((".", "/"))
                or any(
                    char.isspace() or ord(char) < 32 or char in "~^:?*[\\"
                    for char in branch
                )
                or ".." in branch
                or "@{" in branch
                or any(
                    not part or part.startswith(".") or part.endswith(".lock")
                    for part in branch.split("/")
                )
            ):
                raise ValueError("publication requires valid short branch names")


class BranchWriteStatus(StrEnum):
    PUSHED = "pushed"
    ALREADY_AT_TARGET = "already_at_target"
    DIVERGED = "diverged"
    REJECTED = "rejected"
    TRANSIENT_FAILURE = "transient_failure"


@dataclass(frozen=True, slots=True)
class BranchWriteOutcome:
    status: BranchWriteStatus
    observed_remote_head_sha: str | None
    push_outcome: ExactPushOutcome | None
    failure: ValidatedWorkFailure | None
    message: str

    def __post_init__(self) -> None:
        if type(self.status) is not BranchWriteStatus:
            raise ValueError("branch status must be typed")
        if (
            self.push_outcome is not None
            and type(self.push_outcome) is not ExactPushOutcome
        ):
            raise ValueError("push outcome must be typed")
        if self.observed_remote_head_sha is not None:
            require_sha(self.observed_remote_head_sha)
        if self.at_target and (
            self.observed_remote_head_sha is None or self.failure is not None
        ):
            raise ValueError(
                "successful branch stage requires observed target and no failure"
            )
        if (self.status is BranchWriteStatus.PUSHED) != (
            self.push_outcome is ExactPushOutcome.PUSHED
        ):
            raise ValueError("pushed status requires an actual successful push")
        if (
            self.status is BranchWriteStatus.ALREADY_AT_TARGET
            and self.push_outcome is not None
        ):
            raise ValueError("already at target cannot report a push")
        if not self.at_target and type(self.failure) is not ValidatedWorkFailure:
            raise ValueError("unsuccessful branch stage requires a typed failure")
        permitted = {
            BranchWriteStatus.PUSHED: {ExactPushOutcome.PUSHED},
            BranchWriteStatus.ALREADY_AT_TARGET: {None},
            BranchWriteStatus.DIVERGED: {
                None,
                ExactPushOutcome.LEASE_REJECTED,
                ExactPushOutcome.NOT_FAST_FORWARD,
            },
            BranchWriteStatus.REJECTED: {None},
            BranchWriteStatus.TRANSIENT_FAILURE: {
                None,
                ExactPushOutcome.AUTH_FAILED,
                ExactPushOutcome.TRANSIENT,
            },
        }
        if self.push_outcome not in permitted[self.status]:
            raise ValueError("push effect does not match branch status")

    @property
    def at_target(self) -> bool:
        return self.status in {
            BranchWriteStatus.PUSHED,
            BranchWriteStatus.ALREADY_AT_TARGET,
        }


class PrEnsureStatus(StrEnum):
    RECONCILED = "reconciled"
    ADOPTED = "adopted"
    CREATED = "created"
    REFUSED = "refused"
    TRANSIENT_FAILURE = "transient_failure"


@dataclass(frozen=True, slots=True)
class PrEnsureOutcome:
    status: PrEnsureStatus
    pr_number: int | None
    pr_url: str | None
    pr_head_sha: str | None
    failure: ValidatedWorkFailure | None
    message: str

    def __post_init__(self) -> None:
        if type(self.status) is not PrEnsureStatus:
            raise ValueError("PR status must be typed")
        metadata = (self.pr_number, self.pr_url, self.pr_head_sha)
        if any(value is not None for value in metadata):
            if any(value is None for value in metadata):
                raise ValueError("observed PR metadata must be complete")
            assert self.pr_number is not None and self.pr_head_sha is not None
            require_positive(self.pr_number, "pr_number")
            require_sha(self.pr_head_sha)
            if type(self.pr_url) is not str or not self.pr_url:
                raise ValueError("observed PR URL must be nonempty")
        success = self.status not in {
            PrEnsureStatus.REFUSED,
            PrEnsureStatus.TRANSIENT_FAILURE,
        }
        if success:
            if (
                self.pr_number is None
                or self.pr_number <= 0
                or not self.pr_url
                or self.pr_head_sha is None
            ):
                raise ValueError(
                    "successful PR stage requires complete observed PR identity"
                )
            require_sha(self.pr_head_sha)
            if self.failure is not None:
                raise ValueError("successful PR stage cannot carry failure")
        elif type(self.failure) is not ValidatedWorkFailure:
            raise ValueError("unsuccessful PR stage requires typed failure")


class SupersededStage(StrEnum):
    BEFORE_BRANCH_WRITE = "before_branch_write"
    BETWEEN_STEPS = "between_steps"


@dataclass(frozen=True, slots=True)
class PublishValidatedHeadOutcome:
    status: PublishValidatedHeadStatus
    observed_remote_head_sha: str | None
    pr_number: int | None
    pr_url: str | None
    pr_head_sha: str | None
    push_outcome: ExactPushOutcome | None
    failure: ValidatedWorkFailure | None
    message: str
    superseded_stage: SupersededStage | None = None

    @property
    def retryable(self) -> bool:
        return self.status is PublishValidatedHeadStatus.TRANSIENT_FAILURE


def compose_publication_outcome(
    branch: BranchWriteOutcome, pr: PrEnsureOutcome | None
) -> PublishValidatedHeadOutcome:
    """Compose real observations only; an unobserved recorded PR is not a fact."""
    if type(branch) is not BranchWriteOutcome or branch.at_target != (pr is not None):
        raise ValueError("PR stage must run exactly when branch is at target")
    if pr is not None and type(pr) is not PrEnsureOutcome:
        raise ValueError("PR stage must be typed")
    if pr is None:
        status = PublishValidatedHeadStatus(branch.status.value)
        failure, message = branch.failure, branch.message
    elif pr.status is PrEnsureStatus.REFUSED:
        status, failure, message = (
            PublishValidatedHeadStatus.REJECTED,
            pr.failure,
            pr.message,
        )
    elif pr.status is PrEnsureStatus.TRANSIENT_FAILURE:
        status, failure, message = (
            PublishValidatedHeadStatus.TRANSIENT_FAILURE,
            pr.failure,
            pr.message,
        )
    else:
        if pr.pr_head_sha != branch.observed_remote_head_sha:
            raise ValueError("successful stages must identify the same target")
        status = (
            PublishValidatedHeadStatus.PUBLISHED
            if branch.status is BranchWriteStatus.PUSHED
            else PublishValidatedHeadStatus.ALREADY_AT_TARGET
        )
        failure, message = None, pr.message
    return PublishValidatedHeadOutcome(
        status,
        branch.observed_remote_head_sha,
        pr.pr_number if pr else None,
        pr.pr_url if pr else None,
        pr.pr_head_sha if pr else None,
        branch.push_outcome,
        failure,
        message,
    )


def superseded_outcome(
    stage: SupersededStage, branch: BranchWriteOutcome | None
) -> PublishValidatedHeadOutcome:
    if type(stage) is not SupersededStage or (
        stage is SupersededStage.BETWEEN_STEPS
    ) != (branch is not None):
        raise ValueError(
            "supersession requires the branch result exactly between stages"
        )
    if branch is not None and (
        type(branch) is not BranchWriteOutcome or not branch.at_target
    ):
        raise ValueError("between-steps supersession requires successful branch stage")
    return PublishValidatedHeadOutcome(
        PublishValidatedHeadStatus.SUPERSEDED,
        branch.observed_remote_head_sha if branch else None,
        None,
        None,
        None,
        branch.push_outcome if branch else None,
        None,
        "Publication authority superseded",
        stage,
    )
