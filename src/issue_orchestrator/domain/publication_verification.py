"""Pure remote identity and phase rules for recovery publication."""

from dataclasses import dataclass

from .publication_remote import PublicationPullRequest, PublicationPrState, publication_marker
from .validated_head_publication import PublishValidatedHeadCommand, RemoteHeadExpectation
from .validated_work import DispositionPhase, ValidatedWorkFailure


def publication_pr_identity_failure(command: PublishValidatedHeadCommand,
                                    pr: PublicationPullRequest) -> ValidatedWorkFailure | None:
    if pr.state is not PublicationPrState.OPEN:
        return ValidatedWorkFailure.PR_CLOSED_OR_MERGED
    if (pr.head_repo, pr.base_repo, pr.branch, pr.base_branch) != (
        command.repo_slug, command.repo_slug, command.branch_name, command.pr_base_branch,
    ):
        return ValidatedWorkFailure.PR_BRANCH_MISMATCH
    return None


def require_attributable_pr(command: PublishValidatedHeadCommand,
                            pr: PublicationPullRequest) -> ValidatedWorkFailure | None:
    mismatch = publication_pr_identity_failure(command, pr)
    if mismatch is not None:
        return mismatch
    if command.pr_number == pr.number:
        return None
    if command.pr_number is not None or publication_marker(command.issue_number, command.branch_name) not in pr.body.splitlines():
        return ValidatedWorkFailure.PR_BRANCH_MISMATCH
    return None


def baseline_matches(command: PublishValidatedHeadCommand, observed: str | None,
                     phase: DispositionPhase) -> bool:
    if command.expectation is RemoteHeadExpectation.UNCONSTRAINED:
        return False
    if phase is DispositionPhase.RECONCILING and observed == command.target_head_sha:
        return True
    return observed == command.expected_remote_head_sha


@dataclass(frozen=True, slots=True)
class PublicationVerification:
    branch_head: str | None
    pull_request: PublicationPullRequest | None
    failure: ValidatedWorkFailure | None
    message: str

    @property
    def verified(self) -> bool:
        return self.failure is None
