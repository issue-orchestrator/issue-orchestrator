"""Improver exam case IM1: blocked items the tech lead ignored (#7490 step 4).

The tech-lead exam (:mod:`.cases`) grades the tech lead; this case grades the
IMPROVER, the second-order tech lead, against the operator's objective:
blocked issues get resolved, so every blocked item must be accounted for and
the tech lead graded on it.

The fixture is a staged ``improver-data/`` in the shapes porchpin showed on
2026-10-02, when the improver's first blind run accepted ZERO findings and
missed both real defects (#7490 "Handover grade #1"):

* **#262** asked the operator a question (an agent's split question,
  ``agent_completion``). The tech lead never looked at it.
* **#326** is blocked (``blocked-cross-milestone``, then the stuck sweep's
  budget ran out) with no explanation anywhere. The tech lead never looked.
* **#364** is a parked publish: recovery put the reserved ``needs-human`` in
  ``pr_labels`` and was refused five times. The tech lead DIAGNOSED it in a
  case file, and did nothing more. A diagnosis without a fix is
  ``noticed_not_acted``, not a reason to drop it.

All three predate the engine start and are still blocked after it: live,
whatever their origin. The right answer accounts for each with a finding:
#364 at ``noticed_not_acted`` and #262 and #326 at ``not_noticed`` or
``unknown`` (the inputs cannot prove coverage complete, #7525).

:func:`build_case` writes the fixture; :func:`grade` grades a findings file
the validator ACCEPTED. The deterministic half (the validator refuses the
blind run's answer; a reference answer passes) runs in the validation gate;
the live half runs the real improver model on it (``make test-improver-exam``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from ...contracts.engine_audit import (
    ActionLivenessSection,
    Anomaly,
    AnomalyKind,
    AuditSource,
    EngineAuditReport,
    GitHubSection,
    LabelledIssue,
    LogCoverage,
    NoProgressSection,
    ParkedAction,
    SourceReading,
    SourceStatus,
)
from ...contracts.engine_start import EffectiveCharter
from ...contracts.improver_findings import ImproverFindings
from ...contracts.improver_inputs import (
    BLOCKED_ITEMS_FILE,
    CASE_FILES_FILE,
    CHARTER_DECISIONS_FILE,
    CHARTER_FILE,
    ENGINE_SOURCE_DIRNAME,
    ENGINE_START_FILE,
    EXAM_DIRNAME,
    IMPROVER_DATA_DIRNAME,
    INPUTS_FILE,
    OPEN_ISSUES_FILE,
    AUDIT_FILE,
    BlockedItem,
    BlockedItemsInput,
    BlockEventInput,
    BlockingLabelInput,
    CaseFileObservationInput,
    CaseFilesInput,
    CharterDecisionsInput,
    Coverage,
    EngineStartInput,
    InputsManifest,
    NeedsHumanCauseInput,
    OpenIssue,
    OpenIssuesInput,
    StagedCaseFile,
    StagedDecision,
    StagedDiagnosis,
    StagedInput,
    to_json,
)
from .cases import EXAM_CASE_IDS

CASE_ID = "IM1-ignored-blocked-items"

ENGINE_ID = "repo-89afeb99eff0542e787f28211c93f58cd90aa3e0fe9d2bf3acd6fcdc63e2fb73"
AUDITED_REPO = "porchpin/porchpin"
OUTPUTS_REPO = "issue-orchestrator/issue-orchestrator"
STARTED = datetime.fromisoformat("2026-10-02T06:32:58+00:00")
CUTOFF = datetime.fromisoformat("2026-10-02T09:47:06+00:00")
WINDOW_START = CUTOFF - timedelta(hours=24)

#: The case file in which the tech lead diagnosed #364, and nothing more.
CASE_FILE_364 = "case-file:exchange-escalation-pr-label-refused-at-completion-door"
DIAGNOSIS_364 = "tech-lead-run:20261002-050000Z-cd8666c9:tech-lead-1"

#: The stall points a correct answer may grade each item at.
EXPECTED_STALLS: dict[int, frozenset[str]] = {
    262: frozenset({"not_noticed", "unknown"}),
    326: frozenset({"not_noticed", "unknown"}),
    364: frozenset({"noticed_not_acted"}),
}

_RESERVED_REFUSAL = (
    "pr_labels may not contain the reserved shared block label(s) ['needs-human']:"
    " use the needs_human completion outcome instead"
)
_QUESTION_262 = (
    "#262 is more than one session. Its share-page slice is done and gate-green on this"
    " branch; the live D1 seller index (the rest of the acceptance list) is not started."
    " Should I split #262: land this branch as a PR under 'Refs #262' and move the live"
    " index into its own issue(s)?"
)
_CASE_FILE_364_BODY = (
    "First observation (health review #435, run 20261002-050000Z). A coder inside a review"
    " exchange has no escalation that works, and the one it used poisons its own completion."
    " On #364 / PR #379 the coder answered reviewer A1 (a maintainer decision) with"
    " `coding-done completed --pr-labels needs-human` in every round from 1 to 5. The rule"
    " from #6999 F2 refuses any completion whose pr_labels name the shared needs-human block,"
    " at completion_processor.py and at retained_completion_policy.py. After the exchange"
    " halted (05:04:01Z), validated-work recovery hit the same door three times."
    " action_liveness recorded each refusal as transient, so a permanent refusal of fixed"
    " evidence spent 5 backed-off attempts and parked at 05:24:55Z. Its escalation on #364"
    " (needs-human + comment, 05:24:57Z) tells the operator to Retry, which cannot succeed"
    " on unchanged facts. Expected: coding-done refuses reserved pr_labels at submission; a"
    " reserved-label door refusal is classed permanent; and an exchange coder has one"
    " submittable way to escalate."
)


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


# -- the staged inputs ---------------------------------------------------------


def _parked_364() -> ParkedAction:
    return ParkedAction(
        subject="validated_work:r1:d953bc782eaae5b5", action="recover_validated_work",
        fingerprint="ed7763482019c4d04bb89a7deff8c557", attempts=5, escalated=True,
        last_failed_at="2026-10-02T05:24:50+00:00",
        last_reason=f"5 attempts failed with unchanged facts; last: {_RESERVED_REFUSAL}",
    )


_ATTENTION: tuple[tuple[int, str], ...] = (
    (262, "needs-human"),
    (326, "needs-human"),
    (364, "needs-human"),
    (364, "recovery-pending"),
    (326, "blocked-cross-milestone"),
)


def audit() -> EngineAuditReport:
    parked = _parked_364()
    anomalies = (
        Anomaly(
            kind=AnomalyKind.PARKED_ACTION, sources=(AuditSource.ACTION_LIVENESS,),
            subject=parked.subject, signature=f"{parked.action}:{parked.fingerprint}",
            detail=f"escalated; {parked.last_reason}", count=parked.attempts,
        ),
        *(
            Anomaly(
                kind=AnomalyKind.ATTENTION_LABEL, sources=(AuditSource.GITHUB,),
                subject=f"#{number}", signature=label, detail=f"open issue carries {label}",
            )
            for number, label in _ATTENTION
        ),
    )
    return EngineAuditReport(
        generated_at=CUTOFF.isoformat(), repo=AUDITED_REPO, state_dir="/state", partial=False,
        sources=tuple(SourceReading(source=s, status=SourceStatus.READ) for s in AuditSource),
        validated_work=None,
        action_liveness=ActionLivenessSection(by_action=(), parked=(parked,), owed_pauses=()),
        tech_lead=None, claims=None,
        github=GitHubSection(
            open_issues=40, label_counts=(),
            attention=tuple(LabelledIssue(label=label, issue_number=n) for n, label in _ATTENTION),
            open_prs_ready=10, draft_prs=(),
        ),
        no_progress=NoProgressSection(
            window_start=WINDOW_START.isoformat(), window_end=CUTOFF.isoformat(),
            log=LogCoverage(
                path="/state/logs/orchestrator.log", bytes_read=1, truncated=False,
                first_read_at=WINDOW_START.isoformat(), first_entry_at=WINDOW_START.isoformat(),
                last_entry_at=CUTOFF.isoformat(), covers_window=True,
            ),
            log_signatures=(), timeline_repeats=(), state_changes=(),
        ),
        fetch_cost=None,
        anomalies=anomalies,
    )


def _unproven(start: datetime | None, what: str) -> Coverage:
    return Coverage(
        from_=start, to=CUTOFF, complete=False,
        detail=f"{what}: not proven: a record dated before the cutoff can commit after the copy (#7525)",
    )


def _flag_436() -> StagedDecision:
    """The tech lead's only decision near #364: flagging the pattern on its
    case-file issue (#436). Advice about the case file, not a remedy for #364."""
    return StagedDecision(
        decision_id="decision:20261002-050000Z:A3", run_id="20261002-050000Z", action_id="A3",
        role="learning", action_kind="flag_pattern", binding="advisory", outcome="executed",
        reason_code="advisory_executes", reason="", effect="applied", execution_reason=None,
        target_number=436, anchor_issue_number=435, proposal_issue_number=None,
        decided_at=_at("2026-10-02T05:29:00+00:00"), applied_at=_at("2026-10-02T05:30:13+00:00"),
    )


def charter_decisions() -> CharterDecisionsInput:
    return CharterDecisionsInput(
        coverage=_unproven(WINDOW_START, "the charter ledger"), decisions=(_flag_436(),)
    )


def case_files() -> CaseFilesInput:
    return CaseFilesInput(
        coverage=_unproven(WINDOW_START, "the case-file ledger"),
        case_files=(
            StagedCaseFile(
                id=CASE_FILE_364, signature=CASE_FILE_364.removeprefix("case-file:"),
                issue_number=436, recorded_at=_at("2026-10-02T05:30:13+00:00"), observation_count=1,
                fix_class="", area="completion-pipeline", disposition="active",
                retirement_pending=False, body=_CASE_FILE_364_BODY, body_known=True,
                observations=(
                    CaseFileObservationInput(
                        observation_id="20261002-050000Z:tech-lead-1:A3",
                        recorded_at=_at("2026-10-02T05:30:13+00:00"),
                    ),
                ),
            ),
        ),
        diagnoses_coverage=Coverage(from_=None, to=CUTOFF, complete=False, detail="best-effort run history"),
        diagnoses=(
            StagedDiagnosis(
                id=DIAGNOSIS_364, run_key="global:health_review", scope_kind="global_health_review",
                flavor="health_review", phase="completed",
                started_at=_at("2026-10-02T05:00:09+00:00"), ended_at=_at("2026-10-02T05:30:16+00:00"),
                subject_issue_number=0, subject_title="", anchor_issue_number=435,
                body="T2: recovery cannot publish #364's validated head. The completion door refuses"
                " the held record because its pr_labels name needs-human. action_liveness counted each"
                " refusal transient; after 5 attempts it parked at 05:24:55Z and added needs-human to"
                " #364. Do not Retry it.",
                findings=7, proposals=3,
            ),
        ),
    )


def _label(label: str, since: str | None, event: str | None = "issue.labels_changed") -> BlockingLabelInput:
    return BlockingLabelInput(
        label=label, since_at=None if since is None else _at(since), since_event=None if since is None else event
    )


def _event(at: str, event: str, detail: str) -> BlockEventInput:
    return BlockEventInput(at=_at(at), event=event, detail=detail)


def _timeline(since: str) -> Coverage:
    return Coverage(
        from_=_at(since), to=CUTOFF, complete=False,
        detail="the issue's retained events: the timeline trims each issue's oldest rows",
    )


def blocked_items() -> BlockedItemsInput:
    stuck_sweep = NeedsHumanCauseInput(
        cause="session_lifecycle", reason="stuck-sweep recovery budget exhausted (#6824)"
    )
    return BlockedItemsInput(
        read_at=CUTOFF,
        blocking_rule="needs-human, tech-lead-needs-human, blocked, blocked-*, blocked:*",
        causes_coverage=Coverage(from_=None, to=CUTOFF, complete=False, detail="the claim store's current rows"),
        decisions_coverage=_unproven(_at("2026-08-16T00:00:00+00:00"), "the whole charter ledger"),
        items=(
            BlockedItem(
                number=262, title="[CUJ:S1] Live seller pickup index",
                labels=("agent:backend", "enhancement", "needs-human", "priority:high", "v1"),
                blocking_labels=(_label("needs-human", "2026-09-23T06:38:06+00:00", "issue.needs_human"),),
                blocked_since=_at("2026-09-23T06:38:06+00:00"),
                needs_human_causes=(
                    NeedsHumanCauseInput(cause="agent_completion", reason="agent requested needs_human on completion"),
                    stuck_sweep,
                ),
                block_events=(
                    _event("2026-09-04T16:46:20+00:00", "issue.labels_changed", "added ['blocked-failed'] removed []"),
                    _event("2026-09-23T06:04:06+00:00", "issue.labels_changed", "added [] removed ['blocked-failed']"),
                    _event("2026-09-23T06:38:06+00:00", "issue.needs_human",
                           f"question: {_QUESTION_262}; reason: Agent requested human input"),
                ),
                timeline_coverage=_timeline("2026-09-04T14:32:02+00:00"),
                decisions=(), case_file_ids=(), diagnosis_ids=(),
            ),
            BlockedItem(
                number=326, title="[CUJ:S0,R2,H1] Seller account deletion",
                labels=("agent:backend", "blocked-cross-milestone", "enhancement", "needs-human", "priority:medium", "v1"),
                blocking_labels=(
                    _label("blocked-cross-milestone", "2026-09-23T05:28:29+00:00"),
                    _label("needs-human", "2026-09-23T21:28:55+00:00"),
                ),
                blocked_since=_at("2026-09-23T05:28:29+00:00"),
                needs_human_causes=(stuck_sweep,),
                block_events=(
                    _event("2026-09-23T05:27:21+00:00", "dependency.blocked", "summary: Blocked - cross-milestone: #289"),
                    _event("2026-09-23T05:28:29+00:00", "issue.labels_changed", "added ['blocked-cross-milestone'] removed []"),
                    _event("2026-09-23T05:29:14+00:00", "dependency.unblocked", "summary: Unblocked"),
                    _event("2026-09-23T21:28:55+00:00", "issue.labels_changed", "added ['needs-human'] removed []"),
                ),
                timeline_coverage=_timeline("2026-09-23T05:27:16+00:00"),
                decisions=(), case_file_ids=(), diagnosis_ids=(),
            ),
            BlockedItem(
                number=364, title="[CUJ:R2,H1] Hold the batch's Delivery-owner provenance",
                labels=("agent:backend", "needs-human", "pr-pending", "recovery-pending", "v1"),
                blocking_labels=(_label("needs-human", "2026-10-02T05:24:57+00:00"),),
                blocked_since=_at("2026-10-02T05:24:57+00:00"),
                needs_human_causes=(
                    NeedsHumanCauseInput(
                        cause="action_liveness",
                        reason=f"parked recover_validated_work: 5 attempts failed with unchanged facts; last: {_RESERVED_REFUSAL}",
                    ),
                ),
                block_events=(
                    _event("2026-10-02T05:24:57+00:00", "issue.labels_changed", "added ['needs-human'] removed []"),
                ),
                timeline_coverage=_timeline("2026-09-23T05:27:15+00:00"),
                decisions=(), case_file_ids=(CASE_FILE_364,), diagnosis_ids=(DIAGNOSIS_364,),
            ),
        ),
    )


def open_issues() -> OpenIssuesInput:
    """io's open issues, WITHOUT the two that track these defects (#7592,
    #7593): the improver must find them unaided."""
    return OpenIssuesInput(
        repo=OUTPUTS_REPO, read_at=CUTOFF,
        issues=(
            OpenIssue(number=7490, title="Tech-lead improver (second-order tech lead)", labels=("enhancement",)),
            OpenIssue(number=7491, title="Incremental refresh costs more GitHub calls than a full one", labels=("bug",)),
        ),
    )


def build_case(
    root: Path,
    *,
    engine_commit: str,
    charter: EffectiveCharter,
    export_source: Callable[[Path], None],
) -> Path:
    """Write the case's ``improver-data/`` under ``root`` and return it.

    ``export_source`` fills ``engine-source/`` (the io tree at
    ``engine_commit`` for a live run; any tree for the validator-level test).
    """
    data = root / IMPROVER_DATA_DIRNAME
    data.mkdir(parents=True)

    def write(name: str, document: BaseModel) -> None:
        (data / name).write_text(to_json(document), encoding="utf-8")

    write(AUDIT_FILE, audit())
    write(ENGINE_START_FILE, EngineStartInput(
        started_at=STARTED, engine_commit=engine_commit, package_version="0.10.0", repo_head=None,
    ))
    write(CHARTER_FILE, charter)
    write(CHARTER_DECISIONS_FILE, charter_decisions())
    write(CASE_FILES_FILE, case_files())
    write(BLOCKED_ITEMS_FILE, blocked_items())
    write(OPEN_ISSUES_FILE, open_issues())
    (data / EXAM_DIRNAME).mkdir()
    export_source(data / ENGINE_SOURCE_DIRNAME)
    write(INPUTS_FILE, InputsManifest(
        staged_at=CUTOFF, engine_id=ENGINE_ID, audited_repo=AUDITED_REPO, outputs_repo=OUTPUTS_REPO,
        inputs=tuple(
            StagedInput(name=name, staged=True, detail="")
            for name in (
                ENGINE_START_FILE, CHARTER_FILE, AUDIT_FILE, CHARTER_DECISIONS_FILE, CASE_FILES_FILE,
                BLOCKED_ITEMS_FILE, OPEN_ISSUES_FILE, ENGINE_SOURCE_DIRNAME,
            )
        ) + tuple(
            StagedInput(name=name, staged=False, detail="no previous improver run")
            for name in ("audit-previous.json", "audit-diff.json")
        ) + (StagedInput(name=EXAM_DIRNAME, staged=False, detail="no scorecards"),),
        existing_exam_case_ids=EXAM_CASE_IDS,
        exam_scores_comparable=False,
    ))
    return data


# -- the grade -------------------------------------------------------------------


@dataclass(frozen=True)
class ImproverExamGrade:
    case_id: str
    failures: tuple[str, ...]
    #: Each expected item's stall point as graded (None: no finding about it).
    stalls: dict[int, str | None]

    @property
    def passed(self) -> bool:
        return not self.failures


def grade(findings: ImproverFindings) -> ImproverExamGrade:
    """Grade an ACCEPTED findings file (the validator already enforced that
    every blocked item is accounted for, and every citation resolves)."""
    accounts = {a.number: a for a in findings.blocked_items}
    by_id = {f.id: f for f in findings.findings}
    failures: list[str] = []
    stalls: dict[int, str | None] = {}
    for number, allowed in sorted(EXPECTED_STALLS.items()):
        account = accounts.get(number)
        finding = None if account is None or account.finding_id is None else by_id.get(account.finding_id)
        stalls[number] = None if finding is None else finding.stall_point
        if finding is None:
            failures.append(
                f"#{number}: the tech lead never handed it over, so it needs a finding"
                f" ({'not accounted for' if account is None else account.disposition})"
            )
        elif finding.stall_point not in allowed:
            failures.append(
                f"#{number}: graded {finding.stall_point}; expected {' or '.join(sorted(allowed))}"
            )
    return ImproverExamGrade(case_id=CASE_ID, failures=tuple(failures), stalls=stalls)


__all__ = [
    "AUDITED_REPO",
    "CASE_FILE_364",
    "CASE_ID",
    "CUTOFF",
    "DIAGNOSIS_364",
    "ENGINE_ID",
    "EXPECTED_STALLS",
    "ImproverExamGrade",
    "STARTED",
    "build_case",
    "grade",
]
