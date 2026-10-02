"""The improver's evidence, assembled from an engine's records (#7490).

Pure assembly: every function here takes records already read (through the
read ports, from snapshots) and returns the typed document the staging writes
(:mod:`..contracts.improver_inputs`). The rule every one of them keeps is the
improver prompt's: *absence of evidence is not evidence*. A source reports
``complete`` coverage only over a span it can prove it holds every record of:

* the charter ledger and the tech-lead run history are never pruned, so from
  their FIRST record on they are complete; an empty one proves nothing, since
  it cannot say when it started recording;
* a run-history row this build cannot read back is a hole, not a skipped row.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from ..contracts.engine_start import EngineStartRecord
from ..contracts.improver_inputs import (
    CaseFileObservationInput,
    CaseFilesInput,
    CharterDecisionsInput,
    Coverage,
    EngineStartInput,
    Intervention,
    InterventionKind,
    InterventionsInput,
    ScorecardHead,
    StagedCaseFile,
    StagedDecision,
    StagedDiagnosis,
)
from ..domain.pause_state import PauseActor, PauseTransition
from ..domain.tech_lead_charter_decisions import (
    CharterExecutionResult,
    CharterProposalLifecycle,
    TechLeadCharterDecision,
)
from ..events.catalog import EventName
from ..domain.tech_lead_run_record import TechLeadRunPhase, TechLeadRunRecord
from ..ports.engine_audit import CaseFileRecord, TechLeadRunHistoryRead, TimelineEvent

#: The timeline reason the dashboard's Reset & Retry records (``ISSUE_UNBLOCKED``).
RESET_RETRY_REASON = "reset_retry_requested"

#: What no local record shows, so ``interventions.json`` is never complete.
NOT_DERIVABLE_INTERVENTIONS: tuple[str, ...] = (
    "needs-human or other labels removed by a human on GitHub",
    "the dashboard's retry/dismiss buttons (logged, not recorded)",
    "comments or direct edits an operator makes on GitHub",
)
LEDGER_INTERVENTIONS = "charter ledger: proposal approvals and declines"
TIMELINE_INTERVENTIONS = "timeline: Reset & Retry requests"
PAUSE_INTERVENTIONS = "pause journal: pauses an operator surface requested"


class UnparseableTimestampError(ValueError):
    """A stored timestamp is not an ISO-8601 instant with a zone."""


def instant(value: str) -> datetime:
    """A stored ISO timestamp as an aware datetime; naive or garbled raises."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise UnparseableTimestampError(f"not an ISO timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        raise UnparseableTimestampError(f"timestamp has no zone: {value!r}")
    return parsed


def engine_start_input(record: EngineStartRecord) -> EngineStartInput:
    if record.engine_commit is None:
        raise ValueError(
            "the engine recorded no source commit (a non-source install): its source"
            " cannot be staged, and nothing can be reproduced against it"
        )
    return EngineStartInput(
        started_at=record.started_at,
        engine_commit=record.engine_commit,
        package_version=record.package_version,
        repo_head=record.repo_head,
    )


def applied_at(decision: TechLeadCharterDecision) -> datetime | None:
    """When the decision's effect was applied, or None if it never was (or
    the record does not say when: an applied result with no time is not an
    application time, and ``decided_at`` never stands in for one)."""
    if (
        decision.execution is CharterExecutionResult.APPLIED
        and decision.execution_at is not None
    ):
        return instant(decision.execution_at)
    if (
        decision.lifecycle is CharterProposalLifecycle.APPROVED_APPLIED
        and decision.lifecycle_updated_at is not None
    ):
        return instant(decision.lifecycle_updated_at)
    return None


def as_of(decision: TechLeadCharterDecision, cutoff: datetime) -> TechLeadCharterDecision:
    """``decision`` as the ledger held it at ``cutoff``.

    The snapshot is copied after the audit's cutoff, so an applier result or
    a proposal outcome linked in between is not part of what the audit saw:
    it is projected away (an executed decision back to unlinked, a gated one
    back to awaiting approval). A link with no time is treated as late.
    """
    if decision.execution is not None and (
        decision.execution_at is None or instant(decision.execution_at) > cutoff
    ):
        decision = replace(decision, execution=None, execution_reason=None, execution_at=None)
    if (
        decision.lifecycle is not None
        and decision.lifecycle is not CharterProposalLifecycle.AWAITING_APPROVAL
        and (decision.lifecycle_updated_at is None or instant(decision.lifecycle_updated_at) > cutoff)
    ):
        decision = replace(
            decision,
            lifecycle=CharterProposalLifecycle.AWAITING_APPROVAL,
            lifecycle_updated_at=decision.decided_at,
        )
    return decision


def charter_decisions_input(
    ledger: Sequence[TechLeadCharterDecision],
    *,
    window_start: datetime,
    cutoff: datetime,
    coverage_proven: bool,
) -> CharterDecisionsInput:
    """Every decision made in the window, and every older one applied in it.

    ``ledger`` is the whole ledger, oldest first. A decision made before the
    window but applied inside it is kept: that is exactly the fix whose signal
    may still persist (``acted_not_effective``).
    """
    staged = []
    for recorded in ledger:
        decided = instant(recorded.decided_at)
        if decided > cutoff:
            continue
        decision = as_of(recorded, cutoff)
        applied = applied_at(decision)
        if decided < window_start and (applied is None or applied < window_start):
            continue
        staged.append(staged_decision(decision, decided, applied))
    earliest = instant(ledger[0].decided_at) if ledger else None
    return CharterDecisionsInput(
        coverage=ledger_coverage(
            earliest,
            window_start=window_start,
            cutoff=cutoff,
            what="the charter ledger",
            empty="the ledger holds no decision, so it cannot show when it began recording",
            proven=coverage_proven,
        ),
        decisions=tuple(staged),
    )


def staged_decision(
    decision: TechLeadCharterDecision, decided: datetime, applied: datetime | None
) -> StagedDecision:
    return StagedDecision(
        decision_id=decision.decision_id,
        run_id=decision.run_id,
        action_id=decision.action_id,
        role=decision.role.value,
        action_kind=decision.action_kind,
        binding=decision.binding.value,
        outcome=decision.outcome.value,
        reason_code=decision.reason_code.value,
        reason=decision.reason,
        effect=decision.effect,
        execution_reason=decision.execution_reason,
        target_number=decision.target_number,
        anchor_issue_number=decision.anchor_issue_number,
        proposal_issue_number=decision.proposal_issue_number,
        decided_at=decided,
        applied_at=applied,
    )


def case_files_input(
    case_files: Sequence[CaseFileRecord],
    runs: TechLeadRunHistoryRead,
    *,
    window_start: datetime,
    cutoff: datetime,
    coverage_proven: bool,
) -> CaseFilesInput:
    """Every case file (the ledger is small and never pruned) and every
    tech-lead run that overlaps the window, with what each said.

    Coverage is the case-file ledger's: its writes are the orchestrator's own
    durable ones (a failed one fails the action that noticed), so it is
    complete from its first record on. The run history is not: its writer
    logs and drops a failed write, so its diagnoses are evidence that the
    tech lead looked, never proof that it did not.
    """
    diagnoses = tuple(
        diagnosis
        for diagnosis in (
            _diagnosis(run_as_of(record, cutoff))
            for record in runs.records
            # Only a run that had started by the cutoff under EVERY reading
            # of its naive start (a daylight-saving fold names two).
            if engine_local(record.started_at, latest=True) <= cutoff
        )
        if diagnosis.started_at <= cutoff
        and (diagnosis.ended_at is None or diagnosis.ended_at >= window_start)
    )
    staged = tuple(
        staged_file
        for staged_file in (_case_file(record, cutoff) for record in case_files)
        if staged_file.recorded_at <= cutoff
    )
    stamps = [t for c in staged for t in (c.recorded_at, *(o.recorded_at for o in c.observations))]
    coverage = ledger_coverage(
        min(stamps) if stamps else None,
        window_start=window_start,
        cutoff=cutoff,
        what="the case-file ledger",
        empty="no case file is recorded, so the ledger cannot show when it began",
        proven=coverage_proven,
    )
    # An observation after the cutoff may have supplied a case file's
    # diagnosis (the ledger keeps only the current one), so that body is not
    # known as of the cutoff: nothing proves what the ledger said then.
    revised = sorted(
        record.signature
        for record in case_files
        if any(instant(o.recorded_at) > cutoff for o in record.observations)
    )
    if revised:
        coverage = coverage.model_copy(
            update={
                "complete": False,
                "detail": "observed after the cutoff, so their bodies as of it are unknown: "
                + ", ".join(revised),
            }
        )
    return CaseFilesInput(
        coverage=coverage,
        case_files=staged,
        diagnoses_coverage=Coverage(
            from_=None,
            to=cutoff,
            complete=False,
            detail="the tech-lead run history is best-effort (a failed write is dropped)"
            + (f"; {runs.unreadable} row(s) could not be read back" if runs.unreadable else ""),
        ),
        diagnoses=diagnoses,
    )


def _case_file(record: CaseFileRecord, cutoff: datetime) -> StagedCaseFile:
    """One case file as of ``cutoff``: an observation recorded after it (the
    snapshot is copied later) is not part of what the audit cut off at, and
    since such an observation may have supplied the diagnosis (the ledger
    keeps only the current one), the body as of the cutoff is then unknown:
    staged empty, with ``body_known`` False, so it can never read as a notice."""
    kept = tuple(o for o in record.observations if instant(o.recorded_at) <= cutoff)
    revised = len(kept) != len(record.observations)
    return StagedCaseFile(
        id=f"case-file:{record.signature}",
        signature=record.signature,
        issue_number=record.issue_number,
        recorded_at=instant(record.recorded_at),
        observation_count=len(kept) if revised else record.observation_count,
        fix_class=record.fix_class,
        area=record.area,
        disposition=record.disposition,
        retirement_pending=record.retirement_pending,
        body="" if revised else record.diagnosis,
        body_known=not revised,
        observations=tuple(
            CaseFileObservationInput(
                observation_id=o.observation_id, recorded_at=instant(o.recorded_at)
            )
            for o in kept
        ),
    )


def engine_local(moment: datetime, *, latest: bool = False) -> datetime:
    """A run record's time as an instant.

    The engine stamps a session's ``started_at`` with ``datetime.now()``, so
    run records carry the ENGINE HOST's local wall-clock time with no zone.
    Staging reads an engine's state on that same host (its snapshots are byte
    copies of local files), so the host's zone is the engine's. An aware time
    is kept as it is.

    A naive time inside a daylight-saving fold names two instants and the
    record does not say which. The earliest is returned, or the latest with
    ``latest=True``, so a run's span is widened to contain both readings
    rather than guessed.
    """
    if moment.tzinfo is not None:
        return moment
    readings = (moment.replace(fold=0).astimezone(), moment.replace(fold=1).astimezone())
    return max(readings) if latest else min(readings)


def run_as_of(record: TechLeadRunRecord, cutoff: datetime) -> TechLeadRunRecord:
    """``record`` as the run history held it at ``cutoff``.

    A run that ended after the cutoff was still running then: what it
    concluded (its detail and counts, written at the end) is not part of the
    window, and what it said before is not recorded, so it is staged as
    running with nothing said.
    """
    if record.ended_at is None or engine_local(record.ended_at, latest=True) <= cutoff:
        return record
    return replace(
        record,
        phase=TechLeadRunPhase.RUNNING,
        ended_at=None,
        detail="",
        findings=0,
        proposals=0,
        artifacts=None,
    )


def _diagnosis(record: TechLeadRunRecord) -> StagedDiagnosis:
    return StagedDiagnosis(
        id=f"tech-lead-run:{record.run_id}:{record.session_name}",
        run_key=record.run_key,
        scope_kind=record.scope_kind.value,
        flavor=record.flavor.value,
        phase=record.phase.value,
        started_at=engine_local(record.started_at),
        ended_at=None if record.ended_at is None else engine_local(record.ended_at, latest=True),
        subject_issue_number=record.subject_issue_number,
        subject_title=record.subject_title,
        anchor_issue_number=record.anchor_issue_number,
        body=record.detail,
        findings=record.findings,
        proposals=record.proposals,
    )


#: Why a live ledger's coverage is not proven complete (#7525).
UNPROVEN_COMMIT_BOUNDARY = (
    "not proven: a record dated before the cutoff can commit after the copy (a"
    " decision is dated when planned and recorded when applied, or replayed"
    " after a crash with its original date), and nothing yet records commit"
    " order (#7525)"
)


def ledger_coverage(
    earliest: datetime | None,
    *,
    window_start: datetime,
    cutoff: datetime,
    what: str,
    empty: str,
    proven: bool,
) -> Coverage:
    """The span a never-pruned ledger covers: from its first record on, when
    ``proven`` (every record dated by the cutoff is known to be in the
    copy); otherwise the same span, never claimed complete."""
    if earliest is None or earliest > cutoff:
        # Nothing recorded by the cutoff (the snapshot is copied after it, so
        # a first record can land in between): nothing shows when it began.
        return Coverage(from_=None, to=cutoff, complete=False, detail=empty)
    start = max(window_start, earliest)
    if not proven:
        return Coverage(from_=start, to=cutoff, complete=False, detail=f"{what}: {UNPROVEN_COMMIT_BOUNDARY}")
    return Coverage(
        from_=start,
        to=cutoff,
        complete=True,
        detail=f"{what} is never pruned; it is complete from its first record"
        f" ({earliest.isoformat()}) on",
    )


def interventions_input(
    ledger: Iterable[TechLeadCharterDecision],
    timeline: Iterable[TimelineEvent] | str,
    pauses: Iterable[PauseTransition],
    *,
    window_start: datetime,
    cutoff: datetime,
) -> InterventionsInput:
    """The operator interventions the engine's records show in the window.

    ``timeline`` is its events, or why it could not be read; its Reset &
    Retry requests are then named among what is not derivable.
    """
    unread = (f"{TIMELINE_INTERVENTIONS} (unread: {timeline})",) if isinstance(timeline, str) else ()
    found = [
        *_proposal_interventions(ledger),
        *(() if isinstance(timeline, str) else _reset_interventions(timeline)),
        *(
            Intervention(
                at=p.at,
                kind="operator_pause",
                subject="engine",
                detail=f"{p.actor.value}: {p.reason}; {p.detail}".strip(),
            )
            for p in pauses
            if p.paused and p.actor is not PauseActor.SYSTEM
        ),
    ]
    return InterventionsInput(
        window_from=window_start,
        window_to=cutoff,
        derived_from=tuple(
            source
            for source in (LEDGER_INTERVENTIONS, TIMELINE_INTERVENTIONS, PAUSE_INTERVENTIONS)
            if not (unread and source == TIMELINE_INTERVENTIONS)
        ),
        not_derivable=NOT_DERIVABLE_INTERVENTIONS + unread,
        interventions=tuple(
            sorted(
                (i for i in found if window_start <= i.at <= cutoff),
                key=lambda i: (i.at, i.kind, i.subject),
            )
        ),
    )


def _proposal_interventions(ledger: Iterable[TechLeadCharterDecision]) -> Iterable[Intervention]:
    approved = {
        CharterProposalLifecycle.APPROVED_APPLIED,
        CharterProposalLifecycle.APPROVED_STALE,
    }
    for decision in ledger:
        if decision.lifecycle is None or decision.lifecycle_updated_at is None:
            continue
        kind: InterventionKind
        if decision.lifecycle in approved:
            kind = "proposal_approved"
        elif decision.lifecycle is CharterProposalLifecycle.DECLINED:
            kind = "proposal_declined"
        else:
            continue
        yield Intervention(
            at=instant(decision.lifecycle_updated_at),
            kind=kind,
            subject=f"#{decision.proposal_issue_number}"
            if decision.proposal_issue_number
            else decision.decision_id,
            detail=f"{decision.action_kind} ({decision.decision_id})",
        )


def _reset_interventions(timeline: Iterable[TimelineEvent]) -> Iterable[Intervention]:
    for event in timeline:
        record = event.record
        name = record.source_event or record.event
        if name == EventName.ISSUE_UNBLOCKED.value and record.data.get("reason") == RESET_RETRY_REASON:
            yield Intervention(
                at=instant(record.timestamp),
                kind="reset_retry",
                subject=f"#{event.issue_number}",
                detail=str(record.data.get("source", "")),
            )


@dataclass(frozen=True)
class Scorecard:
    """One exam scorecard file: when it was written, what it says, and its bytes."""

    written_at: datetime
    head: ScorecardHead
    text: str


@dataclass(frozen=True)
class ExamSeries:
    """The latest scorecard of every case, and the one before it where there is one."""

    latest: dict[str, Scorecard]
    previous: dict[str, Scorecard]

    @property
    def comparable(self) -> bool:
        """Whether exam scores can be compared run over run: the same case set
        on both sides (the prompt's rule), and at least one case."""
        return bool(self.latest) and set(self.latest) == set(self.previous)


def exam_series(scorecards: Iterable[Scorecard]) -> ExamSeries:
    by_case: dict[str, list[Scorecard]] = {}
    for card in scorecards:
        by_case.setdefault(card.head.case_id, []).append(card)
    latest: dict[str, Scorecard] = {}
    previous: dict[str, Scorecard] = {}
    for case_id, cards in by_case.items():
        ordered = sorted(cards, key=lambda c: c.written_at, reverse=True)
        latest[case_id] = ordered[0]
        if len(ordered) > 1:
            previous[case_id] = ordered[1]
    return ExamSeries(latest=latest, previous=previous)


__all__ = [
    "ExamSeries",
    "NOT_DERIVABLE_INTERVENTIONS",
    "RESET_RETRY_REASON",
    "Scorecard",
    "UNPROVEN_COMMIT_BOUNDARY",
    "UnparseableTimestampError",
    "applied_at",
    "case_files_input",
    "charter_decisions_input",
    "engine_local",
    "engine_start_input",
    "exam_series",
    "instant",
    "interventions_input",
    "ledger_coverage",
    "staged_decision",
]
