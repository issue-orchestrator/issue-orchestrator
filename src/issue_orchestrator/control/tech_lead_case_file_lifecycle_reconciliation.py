"""Reviewable, bounded reconciliation of existing pattern case-file lifecycles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Mapping, cast

from ..domain.tech_lead_findings import (
    TERMINAL_CASE_FILE_DISPOSITIONS,
    VALID_CASE_FILE_DISPOSITIONS,
    CaseFileDisposition,
    CaseFileLifecycleTransition,
)
from ..ports.pattern_registry import (
    admit_lifecycle_transition,
    PatternRegistryError,
    require_canonical_case_file,
    require_reviewed_revision,
)
from .tech_lead_case_file_lifecycle import PatternCaseFileLifecycleOwner

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.pattern_registry import PatternCaseFileRegistry, PatternRegistryEntry


class CaseFileLifecycleReconciliationRefused(ValueError):
    """THE single refusal this command can end on without applying anything.

    Everything that can stop a reviewed plan before its first write is the same
    operational answer to the operator — the plan does not match the registry it
    claims to reconcile, or the registry could not be read at all — so it is one
    type rather than a family the entrypoint must keep enumerating. The owner
    converts every internal cause into this: a plan bound to another repository,
    incomplete registry coverage, a case file registered to a different issue,
    an inadmissible transition, a stale reviewed revision, and a shared-authority
    read failure.

    That last one is why the boundary has to be typed at all. ``list_entries``
    is a live GitHub-ref read, so transport, authentication, CAS-store, and
    malformed-registry failures are ordinary operational outcomes of running
    this command — and validation reads the registry AFTER composition, so a
    successful bootstrap read proves nothing about the read that matters. Left
    untyped they escaped the supported CLI as a traceback instead of the bounded
    nonzero stop the command promises (#7248 review F3).

    Subclasses ``ValueError`` so the entrypoint's pre-composition guard, which
    already refuses malformed plans the same way, keeps catching it.
    """


@dataclass(frozen=True)
class CaseFileLifecycleOutcome:
    """The reviewed disposition of one exact canonical signature mapping."""

    signature: str
    issue_number: int
    disposition: CaseFileDisposition
    expected_revision: str
    reason: str
    evidence: tuple[str, ...]

    def transition(
        self, *, plan_id: str, recorded_at: str
    ) -> CaseFileLifecycleTransition:
        return CaseFileLifecycleTransition(
            transition_id=f"{plan_id}:{self.signature}",
            disposition=self.disposition,
            reason=self.reason,
            evidence=self.evidence,
            recorded_at=recorded_at,
        )


@dataclass(frozen=True)
class CaseFileLifecycleReconciliationPlan:
    """The complete immutable outcome set for one repository registry snapshot."""

    plan_id: str
    repository: str
    recorded_at: str
    outcomes: tuple[CaseFileLifecycleOutcome, ...]

    @classmethod
    def from_mapping(cls, data: Any) -> "CaseFileLifecycleReconciliationPlan":
        mapping = _object(data, "plan")
        _reject_unknown(
            mapping, {"plan_id", "repository", "recorded_at", "outcomes"}, "plan"
        )
        plan_id = _text(mapping, "plan_id", "plan")
        repository = _text(mapping, "repository", "plan")
        recorded_at = _text(mapping, "recorded_at", "plan")
        datetime.fromisoformat(recorded_at)
        raw = mapping.get("outcomes")
        if not isinstance(raw, list) or not raw:
            raise ValueError("plan 'outcomes' must be a non-empty list")
        outcomes = tuple(_outcome(item, position) for position, item in enumerate(raw))
        _reject_collisions(outcomes)
        return cls(
            plan_id=plan_id,
            repository=repository,
            recorded_at=recorded_at,
            outcomes=outcomes,
        )


@dataclass(frozen=True)
class CaseFileLifecycleReconciliationResult:
    plan_id: str
    dry_run: bool
    active: int
    needs_human: int
    terminal: int
    applied: int
    failures: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


def require_plan_repository(
    plan: CaseFileLifecycleReconciliationPlan, configured_repository: str | None
) -> None:
    """THE repository-binding rule for one reviewed lifecycle plan, in one place.

    A plan names the repository whose registry an operator reviewed. Applying it
    anywhere else would retire case files that were never in the reviewed
    snapshot, so the rule is checked twice on purpose: the command checks it
    BEFORE composing write-capable authority, because composition itself may
    publish a durable seed (#7248 review P2), and the reconciler checks it again
    as the first thing it validates, so no caller can reach ``run`` without it.
    Both checks call this, so neither can drift into a different comparison.
    """
    if configured_repository is None:
        raise CaseFileLifecycleReconciliationRefused(
            "the configured repository could not be resolved"
        )
    if plan.repository.casefold() != configured_repository.casefold():
        raise CaseFileLifecycleReconciliationRefused(
            f"plan targets {plan.repository!r}, but this repository is"
            f" {configured_repository!r}"
        )


class CaseFileLifecycleReconciler:
    """Validate full registry coverage, then apply each outcome through its owner."""

    def __init__(
        self,
        *,
        registry: "PatternCaseFileRegistry",
        repository_host: "RepositoryHost",
        require_mutation_authority: Callable[[int], None],
    ) -> None:
        self._registry = registry
        self._repository_host = repository_host
        self._require_mutation_authority = require_mutation_authority

    def run(
        self,
        plan: CaseFileLifecycleReconciliationPlan,
        *,
        configured_repository: str | None,
        apply_writes: bool,
    ) -> CaseFileLifecycleReconciliationResult:
        self._require_reviewed_snapshot(plan, configured_repository)
        active, needs_human, terminal = self._counts(plan.outcomes)
        if not apply_writes:
            return CaseFileLifecycleReconciliationResult(
                plan_id=plan.plan_id,
                dry_run=True,
                active=active,
                needs_human=needs_human,
                terminal=terminal,
                applied=0,
            )
        applied, failures = self._apply(plan)
        return CaseFileLifecycleReconciliationResult(
            plan_id=plan.plan_id,
            dry_run=False,
            active=active,
            needs_human=needs_human,
            terminal=terminal,
            applied=applied,
            failures=failures,
        )

    def _require_reviewed_snapshot(
        self,
        plan: CaseFileLifecycleReconciliationPlan,
        configured_repository: str | None,
    ) -> None:
        require_plan_repository(plan, configured_repository)
        entries = {entry.signature: entry for entry in self._snapshot()}
        planned = {outcome.signature: outcome for outcome in plan.outcomes}
        missing = sorted(set(entries) - set(planned))
        unknown = sorted(set(planned) - set(entries))
        if missing or unknown:
            raise CaseFileLifecycleReconciliationRefused(
                "lifecycle plan must cover the complete shared registry snapshot;"
                f" missing={missing}, unknown={unknown}"
            )
        for signature, outcome in planned.items():
            try:
                self._require_admissible(plan, entries[signature], outcome)
            except PatternRegistryError as exc:
                # Every rule preflight consults is the registries' own, and
                # they speak PatternRegistryError. One conversion here keeps
                # those rules shared, instead of forcing reconciliation-local
                # copies that raise the type this command wants (#7248 F2).
                raise CaseFileLifecycleReconciliationRefused(str(exc)) from exc

    def _snapshot(self) -> tuple["PatternRegistryEntry", ...]:
        """Read the registry this plan claims to reconcile, or refuse bounded.

        A live shared read fails for ordinary operational reasons — transport,
        authentication, a CAS-store conflict, a malformed registry — and the
        composition's own read succeeding earlier proves nothing about this one.
        Translating it here is what keeps every stop this command can make a
        nonzero, explained refusal rather than a traceback (#7248 review F3).
        """
        try:
            return self._registry.list_entries()
        except PatternRegistryError as exc:
            raise CaseFileLifecycleReconciliationRefused(
                f"shared pattern authority could not be read: {exc}"
            ) from exc

    def _require_admissible(
        self,
        plan: CaseFileLifecycleReconciliationPlan,
        entry: "PatternRegistryEntry",
        outcome: CaseFileLifecycleOutcome,
    ) -> None:
        """Ask THE owning rules whether this one outcome may be written at all.

        Preflight exists so a plan is validated as one decision set: a reviewed
        plan must not land outcome #1 and then discover outcome #40 was never
        admissible. That only holds if preflight asks the same question the
        write path asks. It previously asked a narrower one — a private copy of
        the successful-replay half of :func:`admit_lifecycle_transition` — so a
        signature already terminal under a DIFFERENT transition, or a
        ``transition_id`` reused with a changed payload, passed preflight
        whenever its ``expected_revision`` still matched, and the registry only
        refused it mid-apply, after earlier rows had already commented on and
        closed their case files (#7248 review F2/A2).

        So every rule here is the shared owner, called in write order:
        :func:`require_canonical_case_file` for the issue mapping,
        :func:`admit_lifecycle_transition` for admission and idempotent replay,
        and :func:`require_reviewed_revision` for staleness. The one rule that
        is genuinely local to reconciliation is the pending-retirement resume:
        an in-flight terminal write of the SAME reviewed intent is this plan's
        own interrupted apply being repeated, which the registry resumes rather
        than re-admits, and whose entry has no review revision to compare.
        """
        require_canonical_case_file(entry, outcome.issue_number)
        transition = outcome.transition(
            plan_id=plan.plan_id, recorded_at=plan.recorded_at
        )
        pending = entry.pending_retirement
        if pending is not None and pending.transition.same_intent(transition):
            return
        if admit_lifecycle_transition(entry, transition):
            return
        # The same rule the registries enforce inside their reserving
        # compare-and-swap. Running it here too is not a second policy: it
        # is what lets a DRY RUN report a stale plan, and what stops an
        # apply from landing outcome #1 before discovering outcome #40 was
        # reviewed against facts that have since moved.
        require_reviewed_revision(entry, outcome.expected_revision)

    def _apply(
        self, plan: CaseFileLifecycleReconciliationPlan
    ) -> tuple[int, tuple[str, ...]]:
        applied = 0
        failures: list[str] = []
        for outcome in plan.outcomes:
            transition = outcome.transition(
                plan_id=plan.plan_id, recorded_at=plan.recorded_at
            )
            owner = self._owner_for(outcome.issue_number)
            try:
                if outcome.disposition in TERMINAL_CASE_FILE_DISPOSITIONS:
                    owner.retire(
                        signature=outcome.signature,
                        transition=transition,
                        issue_number=outcome.issue_number,
                        expected_revision=outcome.expected_revision,
                    )
                else:
                    owner.classify(
                        signature=outcome.signature,
                        transition=transition,
                        expected_revision=outcome.expected_revision,
                    )
                applied += 1
            except Exception as exc:
                failures.append(f"{outcome.signature}: {exc}")
                break
        return applied, tuple(failures)

    def _owner_for(self, issue_number: int) -> PatternCaseFileLifecycleOwner:
        """Bind ONE lifecycle owner to the issue it is authorized to mutate.

        The owner's ``before_write`` hook takes no arguments on purpose — it is
        the last gate before an external effect, and it must not be able to
        check a different issue than the one about to be written. Binding the
        subject here, once per outcome, is what makes that impossible: there is
        no owner in this module that can write to an issue whose mutation
        authority was not required first.
        """
        return PatternCaseFileLifecycleOwner(
            registry=self._registry,
            repository_host=self._repository_host,
            before_write=lambda: self._require_mutation_authority(issue_number),
        )

    @staticmethod
    def _counts(
        outcomes: tuple[CaseFileLifecycleOutcome, ...],
    ) -> tuple[int, int, int]:
        return (
            sum(item.disposition == "active" for item in outcomes),
            sum(item.disposition == "needs_human" for item in outcomes),
            sum(
                item.disposition in TERMINAL_CASE_FILE_DISPOSITIONS for item in outcomes
            ),
        )


def _outcome(value: Any, position: int) -> CaseFileLifecycleOutcome:
    where = f"outcome #{position}"
    mapping = _object(value, where)
    _reject_unknown(
        mapping,
        {
            "signature",
            "issue",
            "disposition",
            "expected_revision",
            "reason",
            "evidence",
        },
        where,
    )
    disposition = _text(mapping, "disposition", where)
    if disposition not in VALID_CASE_FILE_DISPOSITIONS:
        raise ValueError(
            f"{where} disposition {disposition!r} is not one of"
            f" {sorted(VALID_CASE_FILE_DISPOSITIONS)}"
        )
    evidence = mapping.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or not all(isinstance(item, str) and item.strip() for item in evidence)
    ):
        raise ValueError(f"{where} evidence must be a non-empty string list")
    issue = mapping.get("issue")
    if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
        raise ValueError(f"{where} issue must be a positive integer")
    return CaseFileLifecycleOutcome(
        signature=_text(mapping, "signature", where),
        issue_number=issue,
        disposition=cast(CaseFileDisposition, disposition),
        expected_revision=_revision(mapping, where),
        reason=_text(mapping, "reason", where),
        evidence=tuple(item.strip() for item in evidence),
    )


def _object(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a mapping")
    return value


def _text(mapping: Mapping[str, Any], key: str, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} {key!r} must be a non-empty string")
    return value.strip()


def _revision(mapping: Mapping[str, Any], where: str) -> str:
    value = _text(mapping, "expected_revision", where).lower()
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{where} 'expected_revision' must be a SHA-256 hex digest")
    return value


def _reject_unknown(mapping: Mapping[str, Any], known: set[str], where: str) -> None:
    unknown = sorted(set(mapping) - known)
    if unknown:
        raise ValueError(f"{where} has unknown keys {unknown}")


def _reject_collisions(outcomes: tuple[CaseFileLifecycleOutcome, ...]) -> None:
    signatures = [item.signature for item in outcomes]
    issues = [item.issue_number for item in outcomes]
    if len(set(signatures)) != len(signatures):
        raise ValueError("a lifecycle plan cannot repeat a signature")
    if len(set(issues)) != len(issues):
        raise ValueError("a lifecycle plan cannot repeat an issue number")
