"""Publication-owned assets, distinct from the original coding session run."""

from dataclasses import dataclass
from pathlib import Path

from .validated_work import ValidatedWorkKey, canonical_record_id, require_sha
from .validated_work_escrow import EscrowArtifacts


@dataclass(frozen=True, slots=True)
class PublicationWorkspace:
    key: ValidatedWorkKey
    evidence_id: str
    checkout: Path
    artifacts: EscrowArtifacts

    def __post_init__(self) -> None:
        if not self.checkout.is_absolute() or self.checkout.name != "workspace":
            raise ValueError("publication checkout must be an absolute owned workspace")
        if type(self.key) is not ValidatedWorkKey or not self.evidence_id.startswith("e1:"):
            raise ValueError("publication workspace requires canonical evidence identity")
        require_sha(self.evidence_id[3:], size=64)
        run = self.checkout.parent / "run"
        if self.artifacts.completion != run / "completion.json" or self.artifacts.validation != run / "validation.json":
            raise ValueError("publication artifacts must belong to this publication run")
        if self.artifacts.exchange_summary not in {None, run / "exchange-summary.md"}:
            raise ValueError("publication exchange summary belongs to another run")

    @property
    def record_id(self) -> str:
        return canonical_record_id(self.key)
