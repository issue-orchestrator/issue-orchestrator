"""The one validation owner of ``improver-findings.json`` (#7490).

The improver is an untrusted agent. Its findings file becomes GitHub
artefacts only if EVERY rule here holds; one violation rejects the whole file
and nothing is applied (never partially). The rules are the improver prompt's
"Field rules" (``examples/prompts/tech-lead-improver.md``), checked against
the inputs the orchestrator staged for the run (:class:`StagedEvidence`), and
each is named by a :class:`Rule` so a rejection says exactly which one broke.

The file's shape (types, closed models, three-valued liveness strings) is
:mod:`..contracts.improver_findings`; a shape error is :attr:`Rule.SCHEMA`.
Everything that relates a field to another field or to the staged evidence
is here, and only here.

Citations are checked, not trusted:

* an ``observed`` entry's ``source`` is ``<staged file>#<JSON pointer>`` and
  must resolve in that file;
* a *snapshot* can only show presence, and only the current audit's snapshot
  (taken at its ``generated_at``) counts;
* an *occurrence* must be a dated record of one of the finding's own
  anomalies (a log signature's ``first_seen``/``last_seen``, a parked
  action's ``last_failed_at``, an unresolved record's ``created_at``) in the
  current or previous audit, and its ``at`` must be that record's time;
* ``stall_evidence`` names a decision, case file or tech-lead run that was
  staged, a ``charter.json#<pointer>`` that resolves, or an
  ``engine-source:<path>`` that exists.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from ..contracts.engine_audit import AnomalyKind, AuditDiff, EngineAuditReport
from ..contracts.engine_start import EffectiveCharter
from ..contracts.improver_findings import Finding, ImproverFindings, Observed
from ..contracts.improver_inputs import (
    AUDIT_FILE,
    AUDIT_PREVIOUS_FILE,
    CHARTER_FILE,
    CaseFilesInput,
    CharterDecisionsInput,
    EngineStartInput,
    InterventionsInput,
    OpenIssuesInput,
    StagedDecision,
)

#: ``stall_evidence`` prefix naming a file of the engine's source tree.
ENGINE_SOURCE_CITATION = "engine-source:"
#: ``stall_evidence`` prefix naming a setting in the effective charter.
CHARTER_CITATION = f"{CHARTER_FILE}#"

_NOTICE_GRADES = frozenset({"noticed_not_acted", "acted_not_effective", "not_in_charter"})


class Rule(StrEnum):
    """Every rule a findings file must satisfy; a rejection names the ones it broke."""

    SCHEMA = "schema"
    ENGINE_IDENTITY = "engine_identity"
    UNIQUE_FINDING_IDS = "unique_finding_ids"
    ANOMALY_KEY_EXISTS = "anomaly_key_exists"
    TRACKED_ISSUE_OPEN = "tracked_issue_open"
    NEW_DEFECT_UNTRACKED = "new_defect_untracked"
    UNKNOWN_CLASSIFICATION_INVESTIGATES = "unknown_classification_investigates"
    UNKNOWN_LIVENESS_INVESTIGATES = "unknown_liveness_investigates"
    NO_PURE_HISTORY = "no_pure_history"
    CITATION_RESOLVES = "citation_resolves"
    SNAPSHOT_SHOWS_PRESENCE_ONLY = "snapshot_shows_presence_only"
    OCCURRENCE_IS_A_DATED_RECORD = "occurrence_is_a_dated_record"
    PRESENCE_MATCHES_CURRENT_AUDIT = "presence_matches_current_audit"
    RECURRENCE_NEEDS_POST_START_OCCURRENCE = "recurrence_needs_post_start_occurrence"
    ORIGIN_MATCHES_PRE_START_OCCURRENCE = "origin_matches_pre_start_occurrence"
    WINDOW_ENDS_AT_AUDIT_CUTOFF = "window_ends_at_audit_cutoff"
    WINDOW_STARTS_AT_EARLIEST_OCCURRENCE = "window_starts_at_earliest_occurrence"
    STALL_EVIDENCE_REQUIRED = "stall_evidence_required"
    STALL_EVIDENCE_RESOLVES = "stall_evidence_resolves"
    NOT_NOTICED_NEEDS_PROVEN_ONSET = "not_noticed_needs_proven_onset"
    NOT_NOTICED_NEEDS_COVERAGE = "not_noticed_needs_coverage"
    NOT_NOTICED_CITES_NO_NOTICE = "not_noticed_cites_no_notice"
    NOT_NOTICED_UNREFERENCED = "not_noticed_unreferenced"
    STALL_EVIDENCE_ABOUT_THE_ANOMALY = "stall_evidence_about_the_anomaly"
    ACTED_NOT_EFFECTIVE_NEEDS_APPLIED_DECISION = "acted_not_effective_needs_applied_decision"
    ACTED_NOT_EFFECTIVE_NEEDS_LATER_OBSERVATION = "acted_not_effective_needs_later_observation"
    NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE = "not_in_charter_cites_charter_or_source"
    INVESTIGATION_SHAPE = "investigation_shape"
    REMEDY_SHAPE = "remedy_shape"
    EXAM_CASE_NEEDS_EXAM_REPRODUCTION = "exam_case_needs_exam_reproduction"
    CASE_ID_ONLY_FOR_EXAM_REPRODUCTION = "case_id_only_for_exam_reproduction"
    EXAM_CASE_ID_IS_NEW = "exam_case_id_is_new"
    REPRODUCTION_FAILS_ON_ENGINE_COMMIT = "reproduction_fails_on_engine_commit"
    TREND_UNOBSERVED_WHEN_INCOMPARABLE = "trend_unobserved_when_incomparable"


@dataclass(frozen=True)
class Violation:
    rule: Rule
    #: The finding it is about; None for the file as a whole.
    finding_id: str | None
    message: str

    def describe(self) -> str:
        where = f"finding {self.finding_id}: " if self.finding_id else ""
        return f"[{self.rule.value}] {where}{self.message}"


class ImproverFindingsRejected(ValueError):
    """The findings file broke at least one rule; nothing in it may be applied."""

    def __init__(self, violations: tuple[Violation, ...]) -> None:
        if not violations:
            raise ValueError("a rejection needs at least one violation")
        self.violations = violations
        super().__init__("; ".join(v.describe() for v in violations))

    @property
    def rules(self) -> frozenset[Rule]:
        return frozenset(v.rule for v in self.violations)


@dataclass(frozen=True)
class StagedEvidence:
    """What the orchestrator staged for the run, as the validator reads it.

    ``documents`` is every staged JSON file (name relative to improver-data,
    e.g. ``audit.json`` or ``exam/<case>.json``) as parsed JSON, for citation
    lookups; the typed fields are the same files read through their contracts.
    A None field is an input that was not staged.
    """

    documents: Mapping[str, Any]
    audit: EngineAuditReport
    previous_audit: EngineAuditReport | None
    diff: AuditDiff | None
    engine_start: EngineStartInput
    charter: EffectiveCharter | None
    decisions: CharterDecisionsInput | None
    case_files: CaseFilesInput | None
    interventions: InterventionsInput | None
    open_issues: OpenIssuesInput
    existing_exam_case_ids: frozenset[str]
    exam_comparable: bool
    #: Every file of the staged engine source, relative and ``/``-separated.
    engine_source_files: frozenset[str]


def validate_findings(raw: str | bytes, evidence: StagedEvidence) -> ImproverFindings:
    """``raw`` as typed findings if it breaks no rule; otherwise raises
    :class:`ImproverFindingsRejected` naming every rule it broke."""
    try:
        findings = ImproverFindings.model_validate_json(raw)
    except ValidationError as error:
        raise ImproverFindingsRejected(
            tuple(
                Violation(
                    Rule.SCHEMA,
                    None,
                    f"{'.'.join(str(p) for p in e['loc']) or '<file>'}: {e['msg']}",
                )
                for e in error.errors()
            )
        ) from error
    violations = tuple(_Checker(findings, evidence).violations())
    if violations:
        raise ImproverFindingsRejected(violations)
    return findings


class _Checker:
    def __init__(self, findings: ImproverFindings, evidence: StagedEvidence) -> None:
        self._findings = findings
        self._evidence = evidence
        self._start = evidence.engine_start.started_at
        self._cutoff = datetime.fromisoformat(evidence.audit.generated_at)
        self._current_keys = {a.key for a in evidence.audit.anomalies}
        diff = evidence.diff
        self._diff_keys = (
            set()
            if diff is None
            else {
                *(a.key for a in diff.new),
                *(a.key for a in diff.resolved),
                *(p.anomaly.key for p in diff.persisting),
                *(a.key for a in diff.unobserved),
            }
        )
        self._unobserved_keys = set() if diff is None else {a.key for a in diff.unobserved}
        self._decisions: dict[str, StagedDecision] = (
            {} if evidence.decisions is None
            else {d.decision_id: d for d in evidence.decisions.decisions}
        )
        cases = evidence.case_files
        self._notice_ids: set[str] = (
            set() if cases is None
            else {*(c.id for c in cases.case_files), *(d.id for d in cases.diagnoses)}
        ) | set(self._decisions)
        self._open_issues = {i.number for i in evidence.open_issues.issues}

    def violations(self) -> Iterator[Violation]:
        yield from self._file_rules()
        for finding in self._findings.findings:
            records = _AnomalyRecords(self._evidence, finding)
            yield from (
                Violation(rule, finding.id, message)
                for rule, message in self._finding_rules(finding, records)
            )

    # -- the file ------------------------------------------------------------

    def _file_rules(self) -> Iterator[Violation]:
        f, start = self._findings, self._evidence.engine_start
        if f.engine_commit != start.engine_commit or f.engine_started_at != start.started_at:
            yield Violation(
                Rule.ENGINE_IDENTITY, None,
                f"names engine {f.engine_commit} started {f.engine_started_at.isoformat()};"
                f" engine-start.json says {start.engine_commit} started {start.started_at.isoformat()}",
            )
        seen: set[str] = set()
        case_ids: set[str] = set()
        for finding in f.findings:
            if finding.id in seen:
                yield Violation(Rule.UNIQUE_FINDING_IDS, finding.id, "the id is used twice")
            seen.add(finding.id)
            case_id = finding.reproduction.case_id if finding.reproduction else None
            if case_id is not None and case_id in case_ids:
                yield Violation(Rule.EXAM_CASE_ID_IS_NEW, finding.id, f"case id {case_id} is proposed twice")
            if case_id is not None:
                case_ids.add(case_id)
        if f.trend.exam_scores != "unobserved" and not self._evidence.exam_comparable:
            yield Violation(
                Rule.TREND_UNOBSERVED_WHEN_INCOMPARABLE, None,
                "exam scores have no comparable previous run over the same case set",
            )
        interventions = self._evidence.interventions
        if f.trend.operator_interventions != "unobserved" and (
            interventions is None or not interventions.complete
        ):
            yield Violation(
                Rule.TREND_UNOBSERVED_WHEN_INCOMPARABLE, None,
                "operator interventions are not completely recorded, so no trend is comparable",
            )

    # -- one finding ---------------------------------------------------------

    def _finding_rules(self, f: Finding, records: "_AnomalyRecords") -> Iterator[tuple[Rule, str]]:
        yield from self._keys_and_classification(f)
        yield from self._liveness(f, records)
        yield from self._citations(f, records)
        yield from self._window(f, records)
        yield from self._stall(f, records)
        yield from self._output(f)

    def _keys_and_classification(self, f: Finding) -> Iterator[tuple[Rule, str]]:
        for key in f.anomaly_keys:
            if key.key not in self._current_keys and key.key not in self._diff_keys:
                yield Rule.ANOMALY_KEY_EXISTS, f"no anomaly {key.key} in audit.json or audit-diff.json"
        if f.classification == "tracked":
            if f.tracked_issue is None or f.tracked_issue not in self._open_issues:
                yield Rule.TRACKED_ISSUE_OPEN, f"tracked issue {f.tracked_issue} is not open"
        elif f.tracked_issue is not None:
            rule = Rule.NEW_DEFECT_UNTRACKED if f.classification == "new_defect" else Rule.UNKNOWN_CLASSIFICATION_INVESTIGATES
            yield rule, f"a {f.classification} finding names no tracked issue"
        if f.classification == "unknown" and f.output != "needs_investigation":
            yield Rule.UNKNOWN_CLASSIFICATION_INVESTIGATES, "an unknown classification is only a needs_investigation"

    def _liveness(self, f: Finding, records: "_AnomalyRecords") -> Iterator[tuple[Rule, str]]:
        present, recurs = f.present_after_start, f.recurs_after_start
        if present == "unknown" and recurs == "unknown" and f.output != "needs_investigation":
            yield Rule.UNKNOWN_LIVENESS_INVESTIGATES, "liveness is unknown both ways, so it needs investigation"
        if present == "false" and recurs == "false":
            yield Rule.NO_PURE_HISTORY, "neither present nor recurring after the start: history is not emitted"
        yield from self._presence(f)
        yield from self._recurrence_and_origin(f, records)

    def _presence(self, f: Finding) -> Iterator[tuple[Rule, str]]:
        keys = {k.key for k in f.anomaly_keys}
        if f.present_after_start == "true":
            if not keys & self._current_keys:
                yield Rule.PRESENCE_MATCHES_CURRENT_AUDIT, "none of its anomalies is in the current audit"
            if self._cutoff <= self._start:
                yield Rule.PRESENCE_MATCHES_CURRENT_AUDIT, "the current audit predates the engine start"
            if not any(o.kind == "snapshot" and o.supports == "present_after_start" for o in f.observed):
                yield Rule.PRESENCE_MATCHES_CURRENT_AUDIT, "presence needs a snapshot from the current audit"
        if f.present_after_start == "false" and keys & (self._current_keys | self._unobserved_keys):
            yield Rule.PRESENCE_MATCHES_CURRENT_AUDIT, "the current audit shows it present, or could not observe it"

    def _recurrence_and_origin(self, f: Finding, records: "_AnomalyRecords") -> Iterator[tuple[Rule, str]]:
        occurrences = [o for o in f.observed if o.kind == "occurrence"]
        if f.recurs_after_start == "false" and any(t > self._start for t in records.known_times()):
            yield Rule.RECURRENCE_NEEDS_POST_START_OCCURRENCE, (
                "the staged records show an occurrence after the start, so it recurs"
            )
        if f.recurs_after_start == "true" and not any(
            o.supports == "recurs_after_start" and o.at > self._start for o in occurrences
        ):
            yield Rule.RECURRENCE_NEEDS_POST_START_OCCURRENCE, "recurrence needs an occurrence dated after the start"
        for o in occurrences:
            if o.supports == "recurs_after_start" and o.at <= self._start:
                yield Rule.RECURRENCE_NEEDS_POST_START_OCCURRENCE, f"{o.source} predates the start"
            if o.supports == "origin" and o.at >= self._start:
                yield Rule.ORIGIN_MATCHES_PRE_START_OCCURRENCE, f"{o.source} does not predate the start"
        pre_start = any(o.at < self._start for o in occurrences)
        if f.origin != "before_start" and any(t < self._start for t in records.known_times()):
            yield Rule.ORIGIN_MATCHES_PRE_START_OCCURRENCE, (
                "the staged records show an occurrence before the start, so its origin is before_start"
            )
        elif (f.origin == "before_start") != pre_start:
            yield Rule.ORIGIN_MATCHES_PRE_START_OCCURRENCE, (
                "origin is before_start exactly when a cited occurrence predates the start"
            )

    def _citations(self, f: Finding, records: "_AnomalyRecords") -> Iterator[tuple[Rule, str]]:
        for o in f.observed:
            value = _resolve(self._evidence.documents, o.file, o.ref)
            if value is _MISSING:
                yield Rule.CITATION_RESOLVES, f"{o.source} does not resolve in the staged inputs"
                continue
            if o.kind == "snapshot":
                if o.supports != "present_after_start":
                    yield Rule.SNAPSHOT_SHOWS_PRESENCE_ONLY, f"a snapshot cannot support {o.supports}"
                if o.file != AUDIT_FILE or o.at != self._cutoff or o.ref not in records.snapshots:
                    yield Rule.SNAPSHOT_SHOWS_PRESENCE_ONLY, (
                        f"{o.source}: a snapshot is one of this finding's own anomalies in the"
                        " current audit (audit.json#/anomalies/<i>), dated its generated_at"
                    )
                continue
            if o.supports == "present_after_start":
                yield Rule.OCCURRENCE_IS_A_DATED_RECORD, "an occurrence may since have cleared; it cannot show presence"
            if not records.is_record(o.file, o.ref):
                yield Rule.OCCURRENCE_IS_A_DATED_RECORD, (
                    f"{o.source} is not a dated record of this finding's anomalies"
                )
            elif records.dated_field(o) is None:
                yield Rule.OCCURRENCE_IS_A_DATED_RECORD, f"{o.source} is not dated {o.at.isoformat()}"

    def _window(self, f: Finding, records: "_AnomalyRecords") -> Iterator[tuple[Rule, str]]:
        window = f.grading_window
        if window.to != self._cutoff:
            yield Rule.WINDOW_ENDS_AT_AUDIT_CUTOFF, f"the window must end at audit.json's generated_at ({self._cutoff.isoformat()})"
        if window.from_ == "unknown":
            return
        occurrences = [o.at for o in f.observed if o.kind == "occurrence"]
        if window.from_ not in occurrences:
            yield Rule.WINDOW_STARTS_AT_EARLIEST_OCCURRENCE, "the window starts at a cited occurrence or is unknown"
        earlier = [t for t in (*occurrences, *records.known_times()) if t < window.from_]
        if earlier:
            yield Rule.WINDOW_STARTS_AT_EARLIEST_OCCURRENCE, f"an occurrence at {min(earlier).isoformat()} is earlier"

    def _stall(self, f: Finding, records: "_AnomalyRecords") -> Iterator[tuple[Rule, str]]:
        evidence = f.stall_evidence
        if f.stall_point in _NOTICE_GRADES and not evidence:
            yield Rule.STALL_EVIDENCE_REQUIRED, f"{f.stall_point} must cite its evidence"
        for item in evidence:
            if not self._stall_citation_resolves(item):
                yield Rule.STALL_EVIDENCE_RESOLVES, f"{item!r} is not a staged decision, case file, run, charter setting or source file"
        if f.stall_point in ("noticed_not_acted", "acted_not_effective"):
            yield from self._evidence_about(f)
        if f.stall_point == "not_noticed":
            yield from self._not_noticed(f, records)
        elif f.stall_point == "acted_not_effective":
            yield from self._acted_not_effective(f)
        elif f.stall_point == "not_in_charter":
            if self._evidence.charter is None:
                yield Rule.NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE, "charter.json was not staged, so the grade is unknown"
            if not any(i.startswith((CHARTER_CITATION, ENGINE_SOURCE_CITATION)) for i in evidence):
                yield Rule.NOT_IN_CHARTER_CITES_CHARTER_OR_SOURCE, "cite the charter.json setting or the source that lacks the action"

    def _evidence_about(self, f: Finding) -> Iterator[tuple[Rule, str]]:
        """A cited decision, case file or run must refer to the finding's own
        issue: someone else's remedy does not show this anomaly noticed or
        acted on. An engine anomaly names no issue, so this cannot be checked."""
        if not _issue_numbers(f):
            return
        about = set(_notices_about(self._evidence, f, None, None))
        for item in f.stall_evidence:
            if item in self._notice_ids and item not in about:
                yield Rule.STALL_EVIDENCE_ABOUT_THE_ANOMALY, f"{item} does not refer to the anomaly's issue"

    def _stall_citation_resolves(self, item: str) -> bool:
        if item.startswith(CHARTER_CITATION):
            return _resolve(self._evidence.documents, CHARTER_FILE, item[len(CHARTER_CITATION):]) is not _MISSING
        if item.startswith(ENGINE_SOURCE_CITATION):
            return item[len(ENGINE_SOURCE_CITATION):] in self._evidence.engine_source_files
        return item in self._notice_ids

    def _not_noticed(self, f: Finding, records: "_AnomalyRecords") -> Iterator[tuple[Rule, str]]:
        window = f.grading_window
        if any(i in self._notice_ids for i in f.stall_evidence):
            yield Rule.NOT_NOTICED_CITES_NO_NOTICE, "it cites a decision, case file or run that refers to it"
        if window.from_ != "unknown":
            for notice in _notices_about(self._evidence, f, window.from_, window.to):
                yield Rule.NOT_NOTICED_UNREFERENCED, f"{notice} refers to it inside the grading window"
        if window.from_ == "unknown" or not records.proves_onset(f, window.from_):
            yield Rule.NOT_NOTICED_NEEDS_PROVEN_ONSET, "the onset is not a proven earliest occurrence"
            return
        for name, source in (
            ("charter-decisions.json", self._evidence.decisions),
            ("case-files.json", self._evidence.case_files),
        ):
            if source is None or not source.coverage.contains(window.from_, window.to):
                yield Rule.NOT_NOTICED_NEEDS_COVERAGE, f"{name} does not completely cover the grading window"

    def _acted_not_effective(self, f: Finding) -> Iterator[tuple[Rule, str]]:
        about = set(_notices_about(self._evidence, f, None, None)) if _issue_numbers(f) else None
        applied = [
            d.applied_at
            for d in (self._decisions.get(i) for i in f.stall_evidence)
            if d is not None and d.applied_at is not None and d.applied_at <= f.grading_window.to
            and (about is None or d.decision_id in about)
        ]
        if not applied:
            yield Rule.ACTED_NOT_EFFECTIVE_NEEDS_APPLIED_DECISION, "no cited decision was applied by the audit cutoff"
            return
        live = [o.at for o in f.observed if o.supports in ("present_after_start", "recurs_after_start")]
        if not any(t > at for at in applied for t in live):
            yield Rule.ACTED_NOT_EFFECTIVE_NEEDS_LATER_OBSERVATION, "no presence or recurrence is observed after the decision was applied"

    def _output(self, f: Finding) -> Iterator[tuple[Rule, str]]:
        remedy = {"root_cause": f.root_cause, "reproduction": f.reproduction, "proposal": f.proposal}
        if f.output == "needs_investigation":
            if not f.missing_evidence:
                yield Rule.INVESTIGATION_SHAPE, "name the missing evidence"
            present = [name for name, value in remedy.items() if value is not None]
            if present:
                yield Rule.INVESTIGATION_SHAPE, f"an investigation has no {', '.join(present)}"
        else:
            absent = [name for name, value in remedy.items() if value is None]
            if absent:
                yield Rule.REMEDY_SHAPE, f"a {f.output} needs {', '.join(absent)}"
        repro = f.reproduction
        if repro is None:
            return
        if f.output == "exam_case" and repro.kind != "exam_case":
            yield Rule.EXAM_CASE_NEEDS_EXAM_REPRODUCTION, "an exam case is reproduced by an exam case"
        if (repro.case_id is not None) != (repro.kind == "exam_case"):
            yield Rule.CASE_ID_ONLY_FOR_EXAM_REPRODUCTION, "an exam-case reproduction, and only one, names a case id"
        if repro.case_id is not None and repro.case_id in self._evidence.existing_exam_case_ids:
            yield Rule.EXAM_CASE_ID_IS_NEW, f"case id {repro.case_id} already exists; cases are add-only"
        if repro.fails_on != self._findings.engine_commit:
            yield Rule.REPRODUCTION_FAILS_ON_ENGINE_COMMIT, "the reproduction must fail on the engine's commit"


class _AnomalyRecords:
    """The dated records of one finding's anomalies in the current and previous audit."""

    #: For each anomaly kind: the audit list holding its records, how a record
    #: matches the anomaly key, its dated fields, and which one is its onset.
    def __init__(self, evidence: StagedEvidence, finding: Finding) -> None:
        self._evidence = evidence
        keys = {k.key for k in finding.anomaly_keys}
        #: ``(file, record pointer) -> {field: time}`` for every dated field of
        #: a matching record.
        self._records: dict[tuple[str, str], dict[str, datetime]] = {}
        #: ``(file, record pointer, field)`` of every onset field (a first occurrence).
        self._onsets: set[tuple[str, str, str]] = set()
        #: Pointers to this finding's own anomalies in the CURRENT audit.
        self.snapshots = frozenset(
            f"/anomalies/{index}"
            for index, anomaly in enumerate(evidence.audit.anomalies)
            if anomaly.key in keys
        )
        for name, report in ((AUDIT_FILE, evidence.audit), (AUDIT_PREVIOUS_FILE, evidence.previous_audit)):
            if report is not None:
                self._collect(name, report, keys)

    def _collect(self, name: str, report: EngineAuditReport, keys: set[tuple[str, str, str]]) -> None:
        for index, s in enumerate(report.no_progress.log_signatures):
            if (AnomalyKind.NO_PROGRESS_LOG.value, s.subject, f"{s.level} {s.logger}: {s.signature}") in keys:
                base = f"/no_progress/log_signatures/{index}"
                self._add(name, base, "first_seen", s.first_seen, onset=True)
                self._add(name, base, "last_seen", s.last_seen, onset=False)
        if report.action_liveness is not None:
            for index, p in enumerate(report.action_liveness.parked):
                if (AnomalyKind.PARKED_ACTION.value, p.subject, f"{p.action}:{p.fingerprint}") in keys:
                    self._add(name, f"/action_liveness/parked/{index}", "last_failed_at", p.last_failed_at, onset=False)
        if report.validated_work is not None:
            for index, w in enumerate(report.validated_work.unresolved):
                if (AnomalyKind.STALE_UNRESOLVED_WORK.value, f"#{w.issue_number}", w.record_id) in keys:
                    self._add(name, f"/validated_work/unresolved/{index}", "created_at", w.created_at, onset=True)

    def _add(self, name: str, record: str, field: str, stamp: str, *, onset: bool) -> None:
        self._records.setdefault((name, record), {})[field] = datetime.fromisoformat(stamp)
        if onset:
            self._onsets.add((name, record, field))

    def _split(self, name: str, pointer: str) -> tuple[tuple[str, str], str | None]:
        """``(file, record pointer)`` and the field named, if the pointer names one."""
        record, _, field = pointer.rpartition("/")
        if (name, record) in self._records and field in self._records[(name, record)]:
            return (name, record), field
        return (name, pointer), None

    def is_record(self, name: str, pointer: str) -> bool:
        return self._split(name, pointer)[0] in self._records

    def dated_field(self, o: Observed) -> str | None:
        """The dated field an occurrence cites: the one its pointer names, or,
        for a pointer to the whole record, the one dated ``o.at``."""
        record, field = self._split(o.file, o.ref)
        fields = self._records.get(record, {})
        if field is not None:
            return field if fields[field] == o.at else None
        return next((f for f, t in sorted(fields.items()) if t == o.at), None)

    def is_onset(self, o: Observed) -> bool:
        field = self.dated_field(o)
        record, _ = self._split(o.file, o.ref)
        return field is not None and (*record, field) in self._onsets

    def known_times(self) -> tuple[datetime, ...]:
        return tuple(t for fields in self._records.values() for t in fields.values())

    def proves_onset(self, finding: Finding, onset: datetime) -> bool:
        """Whether ``onset`` is a proven first occurrence: a cited log
        signature's ``first_seen`` whose log read began BEFORE it, so the
        read shows no earlier occurrence. No other source here says where its
        coverage begins, so no other onset is proven."""
        for o in finding.observed:
            if o.kind != "occurrence" or o.at != onset or not self.is_onset(o):
                continue
            report = self._evidence.audit if o.file == AUDIT_FILE else self._evidence.previous_audit
            if report is None or not o.ref.startswith("/no_progress/log_signatures/"):
                continue
            log = report.no_progress.log
            if log is None:
                continue
            began = report.no_progress.window_start if log.covers_window else log.first_entry_at
            if began is not None and datetime.fromisoformat(began) < onset:
                return True
        return False


_ISSUE_SUBJECT = re.compile(r"^(?:PR )?#(\d+)$")


def _issue_numbers(finding: Finding) -> set[int]:
    return {
        int(match.group(1))
        for key in finding.anomaly_keys
        if (match := _ISSUE_SUBJECT.match(key.subject))
    }


def _notices_about(
    evidence: StagedEvidence, finding: Finding, start: datetime | None, end: datetime | None
) -> Iterator[str]:
    """Staged decisions, case files and runs that refer to the finding's
    issues between ``start`` and ``end`` (unbounded where None).

    A reference is structural (a decision about the issue, a run whose
    subject it is) or a ``#<n>`` mention in what the tech lead wrote. An
    anomaly of the engine itself names no issue, so nothing refers to it
    structurally.
    """
    numbers = _issue_numbers(finding)
    if not numbers:
        return
    span = _Span(start, end)
    mentioned = re.compile(r"(?<![\w/])#(?:" + "|".join(map(str, sorted(numbers))) + r")\b")
    if evidence.decisions is not None:
        yield from (
            d.decision_id
            for d in evidence.decisions.decisions
            if (d.target_number if d.target_number is not None else d.anchor_issue_number) in numbers
            and span.holds(d.decided_at)
        )
    if evidence.case_files is not None:
        yield from (
            c.id
            for c in evidence.case_files.case_files
            if mentioned.search(c.body)
            and any(span.holds(t) for t in (c.recorded_at, *(o.recorded_at for o in c.observations)))
        )
        yield from (
            r.id
            for r in evidence.case_files.diagnoses
            if span.overlaps(r.started_at, r.ended_at)
            and (r.subject_issue_number in numbers or mentioned.search(r.body))
        )


@dataclass(frozen=True)
class _Span:
    """``start``..``end``, either end open when None."""

    start: datetime | None
    end: datetime | None

    def holds(self, t: datetime) -> bool:
        return (self.start is None or self.start <= t) and (self.end is None or t <= self.end)

    def overlaps(self, begin: datetime, finish: datetime | None) -> bool:
        return (self.end is None or begin <= self.end) and (
            self.start is None or finish is None or finish >= self.start
        )


_MISSING = object()


def _resolve(documents: Mapping[str, Any], name: str, pointer: str) -> Any:
    """RFC 6901 ``pointer`` in staged file ``name``; ``_MISSING`` when either is absent."""
    if name not in documents or not pointer.startswith("/"):
        return _MISSING
    node = documents[name]
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return _MISSING
    return node


def parse_documents(texts: Mapping[str, str]) -> dict[str, Any]:
    """Staged JSON texts by file name, parsed for citation lookups."""
    return {name: json.loads(text) for name, text in texts.items()}


__all__ = [
    "CHARTER_CITATION",
    "ENGINE_SOURCE_CITATION",
    "ImproverFindingsRejected",
    "Rule",
    "StagedEvidence",
    "Violation",
    "parse_documents",
    "validate_findings",
]
