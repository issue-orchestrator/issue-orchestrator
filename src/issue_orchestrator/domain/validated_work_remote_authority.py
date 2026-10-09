"""Exact remote authority refresh without storage or network dependencies."""

from dataclasses import dataclass, replace

from .branch_naming import extract_issue_number_from_branch
from .publication_remote import PublicationPrState, PublicationPullRequest
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
from .validated_work import PublicationProvenance, require_positive, require_sha
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

    @property
    def provenance(self) -> PublicationProvenance:
        return PublicationProvenance.OBSERVED_OPEN_PR

    def describe(self, validated_head_sha: str) -> str:
        return (
            f"validated head {validated_head_sha[:12]} is already published: open PR "
            f"#{self.pr_number}'s head {self.head_sha[:12]} carries it"
        )


@dataclass(frozen=True, slots=True)
class LandedViaMergedPullRequest:
    """Proof a validated head landed: a merged PR of its branch carried it.

    ``head_sha`` is the PR's head at merge - GitHub's ``head.sha`` for a merged
    PR, kept at ``refs/pull/N/head`` after the branch is deleted - and the
    validated head is that head or an ancestor of it. A squash merge's commit
    on the base contains none of the branch's commits, so the PR head, not
    the merge commit, is the proof.
    """

    pr_number: int
    head_sha: str

    def __post_init__(self) -> None:
        require_positive(self.pr_number, "pr_number")
        require_sha(self.head_sha)

    @property
    def provenance(self) -> PublicationProvenance:
        return PublicationProvenance.OBSERVED_MERGE

    def describe(self, validated_head_sha: str) -> str:
        return (
            f"validated head {validated_head_sha[:12]} landed: merged PR "
            f"#{self.pr_number}'s head {self.head_sha[:12]} carries it"
        )


@dataclass(frozen=True, slots=True)
class CarriedByIssuePullRequest:
    """Proof a validated head is published on ANOTHER branch's PR of its issue (#8137).

    The work was republished: a completion whose PR collided moved to a
    suffixed branch, or an agent rebuilt the slice on a fresh one. Its record
    keeps the branch it validated on, but what protects the work is the PR that
    carries it: open (``merged`` False) or merged, of this repository, whose
    head - fetched from ``refs/pull/N/head`` and agreeing with GitHub - is the
    validated head or one of its descendants. Content, not the branch name,
    is the proof.
    """

    pr_number: int
    head_sha: str
    branch_name: str
    merged: bool

    def __post_init__(self) -> None:
        require_positive(self.pr_number, "pr_number")
        require_sha(self.head_sha)
        require_text(self.branch_name, "branch_name")
        if type(self.merged) is not bool:
            raise ValueError("an issue PR's carriage says whether it merged")

    @property
    def provenance(self) -> PublicationProvenance:
        return (
            PublicationProvenance.OBSERVED_MERGE if self.merged
            else PublicationProvenance.OBSERVED_OPEN_PR
        )

    def describe(self, validated_head_sha: str) -> str:
        state = "merged" if self.merged else "open"
        return (
            f"validated head {validated_head_sha[:12]} is published on another branch: "
            f"{state} PR #{self.pr_number} ({self.branch_name})'s head "
            f"{self.head_sha[:12]} carries it"
        )


#: Every route by which a PR, not recovery, published a validated head.
PullRequestPublication = (
    PublishedOnOpenPullRequest | LandedViaMergedPullRequest | CarriedByIssuePullRequest
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


def landed_via_merged_pull_request(
    pr: PublicationPullRequest,
    *,
    repo_slug: str,
    branch_name: str,
    fetched_head_sha: str | None,
    relation: AncestryRelation | None,
) -> LandedViaMergedPullRequest | None:
    """Whether this merged PR is PROVEN to have landed the validated head.

    Every fact must agree: the PR is merged, of this repository (head and
    base), from this branch; its head at merge, as GitHub reports it, is the
    head just fetched from ``refs/pull/N/head``; and the validated head is
    that head or an ancestor of it. Anything else leaves the work held.
    """
    if (
        pr.state is not PublicationPrState.MERGED
        or pr.head_repo != repo_slug
        or pr.base_repo != repo_slug
        or pr.branch != branch_name
        or fetched_head_sha != pr.head_sha
        or relation not in (AncestryRelation.EQUAL, AncestryRelation.ANCESTOR)
    ):
        return None
    return LandedViaMergedPullRequest(pr.number, pr.head_sha)


def carried_by_issue_pull_request(
    pr: PublicationPullRequest,
    *,
    repo_slug: str,
    issue_number: int,
    branch_name: str,
    fetched_head_sha: str | None,
    relation: AncestryRelation | None,
) -> CarriedByIssuePullRequest | None:
    """Whether another branch's PR of the issue is PROVEN to carry the head (#8137).

    ``pr`` is one of the PRs that reference the issue, as the observer read
    them. Every fact must agree: it is open or merged, of this repository
    (head and base), on a branch OTHER than the record's own - that branch is
    judged by the stricter same-branch routes, which a PR elsewhere must never
    bypass - and that branch is the ISSUE's (``<issue>-...``, as every branch
    the orchestrator cuts for it, a collision's ``-rN`` included). A batch or
    integration PR that merely names the issue is not the issue's PR, so
    custody never routes the issue's review to it. Its head is the head just
    fetched from ``refs/pull/N/head``, and the validated head is that head or
    an ancestor of it. Anything else proves nothing and leaves the work held.
    """
    if (
        pr.state not in (PublicationPrState.OPEN, PublicationPrState.MERGED)
        or pr.head_repo != repo_slug
        or pr.base_repo != repo_slug
        or pr.branch == branch_name
        or extract_issue_number_from_branch(pr.branch) != issue_number
        or fetched_head_sha != pr.head_sha
        or relation not in (AncestryRelation.EQUAL, AncestryRelation.ANCESTOR)
    ):
        return None
    return CarriedByIssuePullRequest(
        pr.number, pr.head_sha, pr.branch, merged=pr.state is PublicationPrState.MERGED,
    )
