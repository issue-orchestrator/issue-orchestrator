"""Authority preserved when temporary lineage restrictions are reconsidered."""

from dataclasses import dataclass
from enum import StrEnum

from .validated_work import ValidatedWorkFailure as Failure, ValidatedWorkState as State
from .validated_work_store import EvidenceRow


LINEAGE_FAILURES = frozenset(
    {
        Failure.ANCESTOR_OF_PENDING_HEAD,
        Failure.DIVERGENT_VALIDATED_HEADS,
        Failure.AWAITING_LINEAGE_PREDECESSOR,
        Failure.REMOTE_BASELINE_UNPROVEN,
    }
)


class GateSource(StrEnum):
    CURRENT_DISPOSITION = "current_disposition"
    DURABLE_FAILURE = "durable_failure"
    LINEAGE_RESTRICTION = "lineage_restriction"


@dataclass(frozen=True, slots=True)
class DispositionGate:
    state: State
    failure: Failure | None
    reason: str

    def __post_init__(self) -> None:
        if type(self.state) is not State or self.state not in {
            State.QUEUED,
            State.PARKED,
            State.FAILED,
        }:
            raise ValueError("gate requires a typed pre-publication state")
        if self.failure is not None and type(self.failure) is not Failure:
            raise ValueError("gate failure must be typed")
        if self.state is State.FAILED and self.failure is None:
            raise ValueError("failed gate requires a failure")

    @property
    def source(self) -> GateSource:
        # The persisted state is the discriminator: derived restrictions park;
        # accepted failure transitions fail, regardless of their failure code.
        if self.state is State.FAILED:
            return GateSource.DURABLE_FAILURE
        if self.state is State.PARKED and self.failure in LINEAGE_FAILURES:
            return GateSource.LINEAGE_RESTRICTION
        return GateSource.CURRENT_DISPOSITION

    @classmethod
    def from_evidence(cls, evidence: EvidenceRow) -> "DispositionGate":
        return cls(evidence.base_state, evidence.base_failure, evidence.base_reason)

    def restore(self, base: "DispositionGate") -> "DispositionGate":
        """Only a removable restriction may recover the evidence's admission gate."""
        if self.source is GateSource.LINEAGE_RESTRICTION:
            return base
        return self

    def tracks(self, base: "DispositionGate") -> bool:
        """Whether changing the durable base may reconsider this disposition."""
        same_authority = (self.state, self.failure) == (base.state, base.failure)
        return same_authority or self.source is GateSource.LINEAGE_RESTRICTION
