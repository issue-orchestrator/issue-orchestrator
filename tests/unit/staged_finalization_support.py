"""Real SQLite/execution owner with interruptible remote and store port boundaries."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from issue_orchestrator.control.actions import ActionResult
from issue_orchestrator.control.retry_review_routing import RetryReviewPolicy
from issue_orchestrator.control.staged_published_work_finalizer import (
    StagedPublishedWorkFinalizer,
)
from issue_orchestrator.control.validated_work_effects import FencedValidatedWorkEffects
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.domain.published_work_finalization import (
    PublishedWorkFinalizationRequest,
    PublishedWorkTarget,
    RecoveryBlockReleaseOutcome,
    RecoveryBlockReleaseStatus,
)
from issue_orchestrator.domain.retry_review_routing import RetryReviewRouting
from issue_orchestrator.domain.validated_work import (
    FinalizationPhase as Phase,
    ReviewDisposition,
)
from issue_orchestrator.domain.validated_work_execution import RecordExecutionBusy
from issue_orchestrator.domain.validated_work_store import PublishValidatedHeadStatus
from issue_orchestrator.execution.validated_work_execution import (
    LocalValidatedWorkExecutionOwner,
)
from tests.unit.validated_work_support import AT, LATER, Rig, begin, capture, claim


class Crash(BaseException):
    """Process interruption must escape all ordinary retry/failure handlers."""


@dataclass
class Interruptions:
    at: str = ""
    error: BaseException = field(default_factory=Crash)
    calls: list[str] = field(default_factory=list)
    callbacks: dict = field(default_factory=dict)

    def hit(self, point):
        self.calls.append(point)
        callback = self.callbacks.get(point)
        if callback:
            callback()
        if point == self.at:
            raise self.error


@dataclass
class Remote:
    points: Interruptions
    labels: set[str] = field(
        default_factory=lambda: {"recovery-pending", "blocked-failed"}
    )
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    observe_written: bool = True
    write_success: bool = True

    def apply(self, action):
        self.points.hit("before:write")
        if not self.write_success:
            return ActionResult.fail(action, "routing rejected")
        self.labels.add(action.label)
        self.added.append(action.label)
        self.points.hit("after:write")
        return ActionResult.ok(action)

    def read_issue_labels(self, issue_number):
        self.points.hit("before:observe")
        result = sorted(
            self.labels if self.observe_written else self.labels - {"pr-pending"}
        )
        self.points.hit("after:observe")
        return result


@dataclass
class PhaseBoundary:
    store: object
    points: Interruptions
    refuse: Phase | None = None
    refuse_failure: bool = False

    def read_finalization_checkpoint(self, claim, target):
        self.points.hit("before:admit")
        phase = self.store.read_finalization_checkpoint(claim, target)
        self.points.hit("after:admit")
        return phase

    def record_finalization_phase(self, claim, *, phase, recorded_at):
        self.points.hit(f"before:{phase.value}")
        if self.refuse is phase:
            return False
        result = self.store.record_finalization_phase(
            claim, phase=phase, recorded_at=recorded_at
        )
        self.points.hit(f"after:{phase.value}")
        return result

    def fail(self, claim, **kwargs):
        self.points.hit("before:fail")
        if self.refuse_failure:
            return False
        result = self.store.fail(claim, **kwargs)
        self.points.hit("after:fail")
        return result


@dataclass
class Aggregate:
    """A test-only aggregate fake, never offered as production sibling policy."""

    store: object
    remote: Remote
    effects: object
    points: Interruptions
    refuse: bool = False
    requests: list = field(default_factory=list)

    def release_published_record(self, request):
        self.points.hit("before:release")
        self.requests.append(request)
        assert request.phase is Phase.REVIEW_ROUTED
        assert (
            self.store.finalization_phase(request.claim.record_id)
            is Phase.REVIEW_ROUTED
        )
        if self.refuse:
            return RecoveryBlockReleaseOutcome(
                request.claim.record_id,
                RecoveryBlockReleaseStatus.REFUSED,
                (),
                "sibling projection refused",
            )
        siblings = [
            row
            for row in self.store.for_issue(
                request.target.key.issue_number
            ).dispositions
            if row.record_id != request.claim.record_id and row.unresolved
        ]
        removed = []
        if not siblings:
            for label in (request.recovery_label, *request.observed_blocking_labels):
                if label in self.remote.labels:

                    def remove(label=label):
                        self.points.hit(f"before:remove:{label}")
                        self.remote.labels.remove(label)
                        self.remote.removed.append(label)
                        removed.append(label)
                        self.points.hit(f"after:remove:{label}")

                    self.effects.perform(request.execution_token, request.claim, remove)
            self.effects.perform(
                request.execution_token,
                request.claim,
                lambda: self.points.hit("observe:release"),
            )
        self.points.hit("after:release")
        return RecoveryBlockReleaseOutcome(
            request.claim.record_id,
            RecoveryBlockReleaseStatus.RELEASED,
            tuple(removed),
            "own interest released",
        )


class FinalizationRig:
    def __init__(
        self,
        path: Path,
        *,
        disposition=ReviewDisposition.ROUTE_TO_PR_REVIEW,
        configured=True,
    ):
        self.rig = Rig(path)
        self.store = self.rig.open()
        admission = capture()
        if disposition is ReviewDisposition.EXCHANGE_APPROVED:
            # Admitted exchange approval requires its independently captured artifact.
            from issue_orchestrator.domain.validated_work import (
                AdmittedArtifact,
                ArtifactSlot,
            )

            identity = replace(
                admission.evidence.identity,
                review_disposition=disposition,
                reviewer_proof_digest="d" * 64,
                exchange_summary_artifact=AdmittedArtifact(
                    ArtifactSlot.EXCHANGE_SUMMARY, "c" * 64, 25
                ),
            )
        else:
            identity = replace(
                admission.evidence.identity, review_disposition=disposition
            )
        self.admission = replace(
            admission, evidence=replace(admission.evidence, identity=identity)
        )
        self.store.admit(self.admission)
        self.claim = claim(self.store, self.admission)
        attempt = begin(self.store, self.claim)
        assert attempt is not None
        assert self.store.record_attempt_outcome(
            self.claim,
            attempt,
            outcome=PublishValidatedHeadStatus.PUBLISHED,
            failure=None,
            finished_at=LATER,
        )
        self.points = Interruptions()
        self.remote = Remote(self.points)
        self.configured = configured
        self.compose()

    def compose(self):
        self.execution = LocalValidatedWorkExecutionOwner(self.store)
        self.effects = FencedValidatedWorkEffects(
            execution=self.execution, fence=self.store
        )
        self.phases = PhaseBoundary(self.store, self.points)
        self.aggregate = Aggregate(self.store, self.remote, self.effects, self.points)
        self.finalizer = StagedPublishedWorkFinalizer(
            effects=self.effects,
            phases=self.phases,
            recovery=self.aggregate,
            fresh_issue_reader=self.remote,
            action_applier=self.remote,
            review_policy=RetryReviewPolicy(
                code_review_agent_configured=self.configured
            ),
            routing_label="pr-pending",
            clock=lambda: datetime.fromisoformat(AT),
        )

    def request(self, token, state, **changes):
        key = self.admission.evidence.identity.key
        disposition = self.admission.evidence.identity.review_disposition
        request = PublishedWorkFinalizationRequest(
            state,
            token,
            self.claim,
            self.store.finalization_phase(key.record_id),
            PublishedWorkTarget(
                key, 91, "https://github.com/owner/repo/pull/91", disposition
            ),
            RetryReviewRouting(
                key.branch_name,
                False,
                disposition is ReviewDisposition.EXCHANGE_APPROVED,
                False,
            ),
            "Preserved work",
            "agent:developer",
            "recovered validated publication",
            "recovery-pending",
            ("blocked-failed",),
            "/preserved/worktree",
        )
        return replace(request, **changes)

    def invoke(self, state=None, **changes):
        if state is None:
            state = OrchestratorState()
        lease = self.execution.try_enter(self.claim.record_id)
        assert not isinstance(lease, RecordExecutionBusy)
        with lease as token:
            if self.execution.claim(token) is None:
                self.execution.remember_claim(token, self.claim)
            return self.finalizer.finalize(self.request(token, state, **changes))

    def reopen(self):
        # Quiescent same-process restart of collaborators, preserving the private
        # handle. Separate tests prove process successor claim acquisition.
        self.store = self.rig.open()
        self.points.at = ""
        self.points.callbacks.clear()
        self.compose()
