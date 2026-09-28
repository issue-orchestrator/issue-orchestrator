"""Exact remote authority refresh without storage or network dependencies."""

from dataclasses import dataclass, replace

from .publication_remote import PublicationPrState
from .validated_work import (
    LineageRole,
    RemoteBaselineStatus,
    ValidatedWorkFailure,
    ValidatedWorkObservations,
    ValidatedWorkState,
    require_text,
)
from .validated_work_capture import ValidatedWorkRemoteFacts
from .validated_work_commands import ValidatedWorkAuthoritySnapshot
from .validated_work import require_positive, require_sha
from .validated_work_store import AncestryRelation, ValidatedWorkRecord


@dataclass(frozen=True, slots=True)
class RemoteAuthorityRefreshRequest:
    """One exact current-evidence snapshot selected by the bounded drain."""

    authority: ValidatedWorkAuthoritySnapshot
    state: ValidatedWorkState
    failure: ValidatedWorkFailure | None

    def __post_init__(self) -> None:
        if type(self.authority) is not ValidatedWorkAuthoritySnapshot:
            raise ValueError("remote authority refresh requires a typed snapshot")
        if type(self.state) is not ValidatedWorkState:
            raise ValueError("remote authority refresh requires a typed state")
        if self.failure is not None and type(self.failure) is not ValidatedWorkFailure:
            raise ValueError("remote authority refresh requires a typed failure")
        if self.authority.remote_baseline_status is not RemoteBaselineStatus.UNOBSERVED:
            raise ValueError("remote authority refresh requires unobserved authority")
        if self.state is ValidatedWorkState.PUBLISHING and self.failure is None:
            return
        if self.state is ValidatedWorkState.PARKED and self.failure in {
            None,
            ValidatedWorkFailure.REMOTE_UNREADABLE,
        }:
            return
        raise ValueError("remote authority refresh requires a retryable disposition")

    @property
    def record_id(self) -> str:
        return self.authority.record_id

    @property
    def evidence_id(self) -> str:
        return self.authority.evidence_id

    def refusal(self, record: ValidatedWorkRecord) -> str | None:
        current = record.current_evidence
        disposition = record.disposition
        if current.authority != self.authority:
            return "Remote authority refresh evidence is no longer current"
        if disposition.state is not self.state or disposition.failure is not self.failure:
            return "Remote authority refresh disposition changed"
        if self.authority.remote_baseline_status is not RemoteBaselineStatus.UNOBSERVED:
            return "Remote authority was already observed"
        if self.state is ValidatedWorkState.PUBLISHING:
            return None
        if self.state is ValidatedWorkState.PARKED and self.failure in {
            None,
            ValidatedWorkFailure.REMOTE_UNREADABLE,
        } and disposition.lineage_role is LineageRole.HEAD:
            return None
        return "Retained work is not eligible for automatic remote refresh"


@dataclass(frozen=True, slots=True)
class RemoteAuthorityDecision:
    observations: ValidatedWorkObservations
    state: ValidatedWorkState
    failure: ValidatedWorkFailure | None
    reason: str

    def __post_init__(self) -> None:
        if type(self.observations) is not ValidatedWorkObservations:
            raise ValueError("remote authority decision requires typed observations")
        if self.observations.remote_baseline_status is not RemoteBaselineStatus.OBSERVED:
            raise ValueError("remote authority decision requires an observed baseline")
        if self.state not in {ValidatedWorkState.QUEUED, ValidatedWorkState.PARKED}:
            raise ValueError("remote authority decision must queue or park")
        if type(self.state) is not ValidatedWorkState:
            raise ValueError("remote authority decision requires a typed state")
        if self.failure is not None and type(self.failure) is not ValidatedWorkFailure:
            raise ValueError("remote authority decision requires a typed failure")
        if self.state is ValidatedWorkState.QUEUED and self.failure is not None:
            raise ValueError("queued remote authority decisions cannot carry a failure")
        if self.failure not in {
            None,
            ValidatedWorkFailure.DUPLICATE_OPEN_PR,
            ValidatedWorkFailure.PR_BRANCH_MISMATCH,
        }:
            raise ValueError("remote authority decision carries an unsupported failure")
        require_text(self.reason, "remote authority decision reason")

    def require_preserved_capture(self, record: ValidatedWorkRecord) -> None:
        """Reject any refresh that changes facts outside the remote authority."""
        before = record.current_evidence.admission.evidence.observations
        restored = replace(
            self.observations,
            expected_remote_head_sha=before.expected_remote_head_sha,
            pr_number=before.pr_number,
            remote_baseline_status=before.remote_baseline_status,
        )
        if restored != before:
            raise ValueError("remote authority refresh changed immutable capture facts")


def classify_remote_pr(
    facts: ValidatedWorkRemoteFacts, repo_slug: str, branch_name: str,
) -> tuple[int | None, ValidatedWorkFailure | None]:
    """Classify the complete branch/PR snapshot used by capture and refresh."""
    if len(facts.pull_requests) > 1:
        return None, ValidatedWorkFailure.DUPLICATE_OPEN_PR
    if not facts.pull_requests:
        return None, None
    pr = facts.pull_requests[0]
    if (
        pr.state is not PublicationPrState.OPEN
        or pr.head_repo != repo_slug
        or pr.base_repo != repo_slug
        or pr.branch != branch_name
        or facts.branch_head_sha is None
        or pr.head_sha != facts.branch_head_sha
    ):
        return None, ValidatedWorkFailure.PR_BRANCH_MISMATCH
    return pr.number, None


def refreshed_remote_authority(
    record: ValidatedWorkRecord, facts: ValidatedWorkRemoteFacts,
) -> RemoteAuthorityDecision:
    """Replace only mutable remote facts and derive the durable recovery gate."""
    identity = record.current_evidence.admission.evidence.identity
    key = identity.key
    pr_number, failure = classify_remote_pr(facts, key.repo_slug, key.branch_name)
    observations = replace(
        record.current_evidence.admission.evidence.observations,
        expected_remote_head_sha=facts.branch_head_sha,
        pr_number=pr_number,
        remote_baseline_status=RemoteBaselineStatus.OBSERVED,
    )
    if failure is not None:
        return RemoteAuthorityDecision(
            observations,
            ValidatedWorkState.PARKED,
            failure,
            f"Remote authority refreshed; {failure.value}",
        )
    if (
        record.disposition.state is ValidatedWorkState.PARKED
        and record.disposition.failure is None
    ):
        return RemoteAuthorityDecision(
            observations,
            ValidatedWorkState.PARKED,
            None,
            "Remote authority refreshed; explicit recovery approval still required",
        )
    return RemoteAuthorityDecision(
        observations,
        ValidatedWorkState.QUEUED,
        None,
        "Remote authority refreshed; automatic recovery authorized",
    )


@dataclass(frozen=True, slots=True)
class PublishedOnOpenPullRequest:
    """Proof a validated head is published: its issue's one open PR carries it.

    ``head_sha`` is the PR branch head, read fresh from the remote, and the
    validated head is that head or an ancestor of it. It is a publication
    route of its own - the completion's push, not recovery's - and the store
    records it as the lineage's published head (``OBSERVED_OPEN_PR``).
    """

    pr_number: int
    head_sha: str

    def __post_init__(self) -> None:
        require_positive(self.pr_number, "pr_number")
        require_sha(self.head_sha)

    def describe(self, validated_head_sha: str) -> str:
        return (
            f"validated head {validated_head_sha[:12]} is already published: open PR "
            f"#{self.pr_number}'s head {self.head_sha[:12]} carries it"
        )


def carried_by_open_pull_request(
    facts: ValidatedWorkRemoteFacts,
    *,
    repo_slug: str,
    branch_name: str,
    fetched_head_sha: str | None,
    relation: AncestryRelation | None,
) -> PublishedOnOpenPullRequest | None:
    """Whether an open PR is PROVEN to carry the validated head; None proves nothing.

    Every fact must agree: exactly one open PR, of this repository, on this
    branch, whose head is the branch head the remote reported AND the head just
    fetched; and the validated head is that head or an ancestor of it. A closed
    PR, no PR, several PRs, a moved branch or an unanswerable ancestry leaves
    the head to recovery, which publishes it and opens its PR.
    """
    pr_number, failure = classify_remote_pr(facts, repo_slug, branch_name)
    if pr_number is None or failure is not None or fetched_head_sha != facts.branch_head_sha:
        return None
    if relation not in (AncestryRelation.EQUAL, AncestryRelation.ANCESTOR):
        return None
    assert fetched_head_sha is not None  # classify_remote_pr proved the branch head
    return PublishedOnOpenPullRequest(pr_number, fetched_head_sha)
