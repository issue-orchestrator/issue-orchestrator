"""Typed boundary fakes and deterministic validated-work fixtures."""

from dataclasses import dataclass, field, replace
from pathlib import Path

from issue_orchestrator.domain.models import RequestedAction
from issue_orchestrator.domain.session_run import SessionRunIdentity
from issue_orchestrator.domain.validated_work import (
    AdmittedArtifact,
    ArtifactSlot,
    ReviewDisposition,
    ValidatedWorkEvidence,
    ValidatedWorkFailure,
    ValidatedWorkIdentity,
    ValidatedWorkKey,
    ValidatedWorkObservations,
    ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_claim import (
    ProcessIdentity,
    ValidatedWorkClaim,
)
from issue_orchestrator.domain.validated_work_store import (
    AncestryRelation,
    CommitReference,
    DispositionPhase,
    EvidenceAdmission,
    EvidenceRow,
    FinalizationPhase,
    PublicationResolution,
    PublishAttempt,
    PublishValidatedHeadStatus,
)
from issue_orchestrator.infra.validated_work_store import SqliteValidatedWorkStore

AT = "2026-09-06T12:00:00+00:00"
LATER = "2026-09-06T13:00:00+00:00"
ROOT, V, L, TIP, DIVERGENT = (f"{n:040x}" for n in range(1, 6))
OWNER = ProcessIdentity("test-host", 101, AT, "engine-a")
OTHER = ProcessIdentity("test-host", 102, AT, "engine-b")


@dataclass
class GraphAncestry:
    parents: dict[str, str] = field(
        default_factory=lambda: {V: ROOT, L: V, TIP: L, DIVERGENT: ROOT}
    )
    missing: set[str] = field(default_factory=set)
    raise_on: set[str] = field(default_factory=set)

    def compare(
        self, left: CommitReference, right: CommitReference
    ) -> AncestryRelation:
        a, b = left.key.validated_head_sha, right.key.validated_head_sha
        if a in self.raise_on or b in self.raise_on:
            raise RuntimeError("injected ancestry interruption")
        if a in self.missing and b in self.missing:
            return AncestryRelation.BOTH_UNREACHABLE
        if a in self.missing:
            return AncestryRelation.LEFT_UNREACHABLE
        if b in self.missing:
            return AncestryRelation.RIGHT_UNREACHABLE
        if a == b:
            return AncestryRelation.EQUAL
        if self.contains(b, a):
            return AncestryRelation.ANCESTOR
        if self.contains(a, b):
            return AncestryRelation.DESCENDANT
        return AncestryRelation.DIVERGENT

    def contains(self, head: str, ancestor: str) -> bool:
        while head in self.parents:
            head = self.parents[head]
            if head == ancestor:
                return True
        return False


@dataclass
class ArtifactVerifier:
    released: list[str] = field(default_factory=list)
    invalid: set[str] = field(default_factory=set)
    raise_on: set[str] = field(default_factory=set)

    def release(self, evidence: EvidenceRow) -> None:
        self.released.append(evidence.evidence_id)

    def verifies(self, evidence: EvidenceRow) -> bool:
        if evidence.record_id in self.raise_on:
            raise RuntimeError("injected escrow interruption")
        return evidence.record_id not in self.invalid


@dataclass
class Liveness:
    identity: ProcessIdentity = OWNER
    dead: set[ProcessIdentity] = field(default_factory=set)
    checked: list[ProcessIdentity] = field(default_factory=list)

    def current(self) -> ProcessIdentity:
        return self.identity

    def is_provably_dead(self, owner: ProcessIdentity) -> bool:
        self.checked.append(owner)
        return owner in self.dead


@dataclass
class Rig:
    path: Path
    graph: GraphAncestry = field(default_factory=GraphAncestry)
    artifacts: ArtifactVerifier = field(default_factory=ArtifactVerifier)
    liveness: Liveness = field(default_factory=Liveness)

    def open(self, liveness: Liveness | None = None) -> SqliteValidatedWorkStore:
        return SqliteValidatedWorkStore(
            self.path,
            ancestry=self.graph,
            artifacts=self.artifacts,
            retention=self.artifacts,
            liveness=self.liveness if liveness is None else liveness,
        )


def capture(
    head: str = V,
    *,
    run: str = "run-1",
    state: ValidatedWorkState = ValidatedWorkState.QUEUED,
    failure: ValidatedWorkFailure | None = None,
    reason: str = "admitted",
    expected: str | None = ROOT,
    branch: str = "feature",
    issue: int = 6914,
    pr: int | None = 91,
    observed: str | None = None,
    bound: bool = True,
    at: str = AT,
) -> EvidenceAdmission:
    key = ValidatedWorkKey("owner/repo", issue, branch, head)
    identity = ValidatedWorkIdentity(
        1,
        key,
        SessionRunIdentity("coding-6914", run, AT),
        AdmittedArtifact(ArtifactSlot.COMPLETION, "a" * 64, 120),
        AdmittedArtifact(ArtifactSlot.VALIDATION, "b" * 64, 250),
        None,
        (RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR),
        ReviewDisposition.ROUTE_TO_PR_REVIEW,
        None,
        bound,
        None,
    )
    observations = ValidatedWorkObservations(
        at,
        observed or head,
        expected,
        pr,
        ("blocked-failed",),
        {ArtifactSlot.COMPLETION: "/audit/completion"},
    )
    ev = ValidatedWorkEvidence(identity, observations)
    return EvidenceAdmission(
        ev,
        state,
        failure,
        reason,
        f"{issue}/{ev.evidence_id}",
        f"refs/issue-orchestrator/validated/{issue}/{ev.evidence_id}",
        "",
        at,
    )


def claim(
    store: SqliteValidatedWorkStore, admission: EvidenceAdmission
) -> ValidatedWorkClaim:
    row = store.get(admission.evidence.record_id)
    result = store.acquire_claim(
        row.record_id,
        expected_states=frozenset({row.state}),
        evidence_id=row.evidence_id,
    )
    assert result is not None
    return result


def begin(
    store: SqliteValidatedWorkStore,
    token: ValidatedWorkClaim,
    *,
    approved: bool = False,
) -> PublishAttempt | None:
    row = store.get(token.record_id)
    lookup = store.evidence_for_id(row.evidence_id)
    assert lookup is not None
    authority = lookup.evidence.authority
    return store.begin_publish_attempt(
        token,
        expected_attempt_no=len(store.publish_attempts(row.record_id)),
        target_head_sha=row.key.validated_head_sha,
        expected_remote_head=authority.expected_remote_head_sha or "",
        phase=DispositionPhase.RECONCILING
        if row.state is ValidatedWorkState.PUBLISHING
        else DispositionPhase.PRE_SUBMISSION,
        started_at=AT,
        authority=authority if approved else None,
    )


def finalize(
    store: SqliteValidatedWorkStore, token: ValidatedWorkClaim, attempt: PublishAttempt
) -> PublicationResolution:
    assert store.record_attempt_outcome(
        token,
        attempt,
        outcome=PublishValidatedHeadStatus.PUBLISHED,
        failure=None,
        finished_at=LATER,
    )
    for phase in (FinalizationPhase.REVIEW_ROUTED, FinalizationPhase.RECOVERY_CLEARED):
        assert store.record_finalization_phase(token, phase=phase, recorded_at=LATER)
    result = store.resolve_published(
        token,
        record_id=token.record_id,
        published_head_sha=attempt.target_head_sha,
        pre_push_expected=attempt.expected_remote_head,
        finalized_at=LATER,
    )
    assert isinstance(result, PublicationResolution)
    return result


def changed_observations(
    admission: EvidenceAdmission, **kwargs: object
) -> EvidenceAdmission:
    return replace(
        admission,
        evidence=replace(
            admission.evidence,
            observations=replace(admission.evidence.observations, **kwargs),
        ),
    )
