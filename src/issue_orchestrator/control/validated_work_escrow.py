"""Crash reconciliation and retention; evidence roles belong to normal admission."""

from datetime import datetime, timedelta, timezone

from ..domain.validated_work import require_positive
from ..domain.validated_work_escrow import EscrowProblem, EscrowReport
from ..domain.validated_work_store import EvidenceAdmission
from ..ports.validated_work_escrow import EvidenceReader, ValidatedWorkEscrow
from ..ports.validated_work_store import ValidatedWorkStore


class EscrowInspection:
    def __init__(self, *, escrow: ValidatedWorkEscrow, reader: EvidenceReader) -> None:
        self._escrow = escrow
        self._reader = reader

    def inspect_orphans(self) -> EscrowReport:
        locators, inventory = self._escrow.inventory()
        problems = list(inventory.problems)
        admissions: list[EvidenceAdmission] = []
        for locator in locators:
            try:
                admission = self._escrow.inspect(locator)
                admissions.append(admission)
                self._escrow.verify_pins(admission)
                if self._reader.evidence_for_id(admission.evidence.evidence_id) is None:
                    problems.append(
                        EscrowProblem(
                            locator,
                            "capture has no evidence row; retained for reconciliation",
                        )
                    )
            except Exception as exc:
                problems.append(EscrowProblem(locator, str(exc)))
        problems.extend(self._escrow.orphan_pins(tuple(admissions)))
        return EscrowReport(problems=tuple(problems))


class ValidatedWorkEscrowMaintenance:
    def __init__(
        self,
        *,
        escrow: ValidatedWorkEscrow,
        store: ValidatedWorkStore,
        retention_days: int,
    ) -> None:
        require_positive(retention_days, "escrow_retention_days")
        self._escrow = escrow
        self._store = store
        self._retention_days = retention_days

    def reconcile_escrow_orphans(self) -> EscrowReport:
        locators, inventory = self._escrow.inventory()
        repaired: list[str] = []
        problems = list(inventory.problems)
        admissions: list[EvidenceAdmission] = []
        for locator in locators:
            try:
                admission = self._escrow.inspect(locator)
                admissions.append(admission)
                if (
                    self._store.evidence_for_id(admission.evidence.evidence_id)
                    is not None
                ):
                    self._escrow.verify_pins(admission)
                    continue
                # A rename-before-pin crash can repin ONLY the recorded exact object.
                self._escrow.ensure_pins(admission)
                self._escrow.verify_pins(admission)
                self._store.admit(admission)
                repaired.append(admission.evidence.evidence_id)
            except Exception as exc:
                problems.append(EscrowProblem(locator, str(exc)))
        problems.extend(self._escrow.orphan_pins(tuple(admissions)))
        return EscrowReport(tuple(repaired), tuple(problems))

    def sweep(self, *, now: datetime) -> EscrowReport:
        if now.tzinfo is None:
            raise ValueError("retention requires an aware time")
        cutoff = (
            now.astimezone(timezone.utc) - timedelta(days=self._retention_days)
        ).isoformat()
        released: list[str] = []
        problems: list[EscrowProblem] = []
        # No filesystem inventory, role filter or inferred resolution authorizes deletion.
        for evidence in self._store.evidence_for_retention(released_before=cutoff):
            try:
                if self._store.release_evidence_for_retention(
                    evidence.evidence_id,
                    released_before=cutoff,
                    released_at=now.astimezone(timezone.utc).isoformat(),
                ):
                    released.append(evidence.evidence_id)
            except Exception as exc:
                problems.append(EscrowProblem(evidence.admission.escrow_dir, str(exc)))
        return EscrowReport(tuple(released), tuple(problems))
