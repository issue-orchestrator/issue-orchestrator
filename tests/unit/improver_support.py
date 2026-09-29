"""A small, fully staged ``improver-data/`` for validator tests and examples (#7490).

The engine started at :data:`STARTED`; the current audit ran at :data:`CUTOFF`
over a 24-hour window, and the previous one a day earlier. Four anomalies:

* ``#410`` — a no-progress log signature seen since BEFORE the start (the
  previous audit saw it first at :data:`A1_FIRST_SEEN`) and still repeating;
  the tech lead flagged it (decision ``D1``, case file ``CF1``);
* ``#320`` — a parked action last failing before the start, still parked; the
  tech lead's remedy ``D2`` was applied at :data:`D2_APPLIED`;
* ``#353`` — an open issue carrying ``needs-human`` (a snapshot, nothing dated);
* ``#500`` — a log signature first seen AFTER the start, inside a log read
  that began before it (a proven onset), which nothing the tech lead recorded
  refers to.

``examples/improver/findings/*.json`` are written against exactly this
evidence, one per output kind.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from issue_orchestrator.contracts.engine_audit import (
    ActionLivenessSection,
    Anomaly,
    AnomalyKind,
    AuditSource,
    EngineAuditReport,
    GitHubSection,
    LabelledIssue,
    LogCoverage,
    LogSignature,
    NoProgressSection,
    ParkedAction,
    SourceReading,
    SourceStatus,
)
from issue_orchestrator.contracts.improver_inputs import (
    CaseFileObservationInput,
    CaseFilesInput,
    CharterDecisionsInput,
    Coverage,
    EngineStartInput,
    InputsManifest,
    OpenIssue,
    OpenIssuesInput,
    StagedCaseFile,
    StagedDecision,
    StagedDiagnosis,
    StagedInput,
    to_json,
)
from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
from issue_orchestrator.infra.config import Config
from issue_orchestrator.observation.engine_audit_diff import diff_reports
from issue_orchestrator.testing.exam.cases import EXAM_CASE_IDS

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "improver" / "findings"

ENGINE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
STARTED = datetime.fromisoformat("2026-09-28T12:00:00+00:00")
CUTOFF = datetime.fromisoformat("2026-09-28T18:00:00+00:00")
PREVIOUS = CUTOFF - timedelta(days=1)
A1_FIRST_SEEN = "2026-09-27T09:00:00+00:00"
D2_APPLIED = "2026-09-28T14:00:00+00:00"
SOURCE_FILE = "src/issue_orchestrator/domain/tech_lead_charter.py"
OPEN_TRACKER = 7491

A1 = Anomaly(
    kind=AnomalyKind.NO_PROGRESS_LOG, sources=(AuditSource.LOG, AuditSource.TIMELINE),
    subject="#410", signature="WARNING io.retry: validation retry refused",
    detail="184 since the subject last changed state", count=184,
)
A2 = Anomaly(
    kind=AnomalyKind.PARKED_ACTION, sources=(AuditSource.ACTION_LIVENESS,),
    subject="#320", signature="publish:fp1", detail="divergent_validated_heads", count=5,
)
A3 = Anomaly(
    kind=AnomalyKind.ATTENTION_LABEL, sources=(AuditSource.GITHUB,),
    subject="#353", signature="needs-human", detail="open issue carries needs-human",
)
A4 = Anomaly(
    kind=AnomalyKind.NO_PROGRESS_LOG, sources=(AuditSource.LOG, AuditSource.TIMELINE),
    subject="#500", signature="ERROR io.boom: exploded", detail="12 since", count=12,
)


def _signature(anomaly: Anomaly, *, first: str, last: str) -> LogSignature:
    level, rest = anomaly.signature.split(" ", 1)
    logger, text = rest.split(": ", 1)
    return LogSignature(
        subject=anomaly.subject, level=level, logger=logger, signature=text,
        count=anomaly.count or 0, since_state_change=anomaly.count,
        first_seen=first, last_seen=last,
    )


def _report(
    at: datetime, *, anomalies: tuple[Anomaly, ...], signatures: tuple[LogSignature, ...],
    parked: tuple[ParkedAction, ...],
) -> EngineAuditReport:
    window_start = at - timedelta(hours=24)
    return EngineAuditReport(
        generated_at=at.isoformat(), repo="porchpin/porchpin", state_dir="/state", partial=False,
        sources=tuple(SourceReading(source=s, status=SourceStatus.READ) for s in AuditSource),
        validated_work=None,
        action_liveness=ActionLivenessSection(by_action=(), parked=parked, owed_pauses=()),
        tech_lead=None, claims=None,
        github=GitHubSection(
            open_issues=3, label_counts=(), attention=(LabelledIssue(label="needs-human", issue_number=353),),
            open_prs_ready=0, draft_prs=(),
        ),
        no_progress=NoProgressSection(
            window_start=window_start.isoformat(), window_end=at.isoformat(),
            log=LogCoverage(
                path="/state/logs/orchestrator.log", bytes_read=1, truncated=False,
                first_read_at=(window_start - timedelta(hours=1)).isoformat(),
                first_entry_at=window_start.isoformat(), last_entry_at=at.isoformat(),
                covers_window=True,
            ),
            log_signatures=signatures, timeline_repeats=(), state_changes=(),
        ),
        fetch_cost=None,
        anomalies=anomalies,
    )


def _parked() -> ParkedAction:
    return ParkedAction(
        subject="#320", action="publish", fingerprint="fp1", attempts=5, escalated=False,
        last_failed_at="2026-09-28T10:00:00+00:00", last_reason="divergent_validated_heads",
    )


def audits() -> tuple[EngineAuditReport, EngineAuditReport]:
    previous = _report(
        PREVIOUS, anomalies=(A1,),
        signatures=(_signature(A1, first=A1_FIRST_SEEN, last="2026-09-27T17:00:00+00:00"),),
        parked=(),
    )
    current = _report(
        CUTOFF, anomalies=(A1, A2, A3, A4),
        signatures=(
            _signature(A1, first="2026-09-27T18:05:00+00:00", last="2026-09-28T17:30:00+00:00"),
            _signature(A4, first="2026-09-28T13:00:00+00:00", last="2026-09-28T17:00:00+00:00"),
        ),
        parked=(_parked(),),
    )
    return previous, current.model_copy(update={"diff": diff_reports(previous, current)})


def _coverage() -> Coverage:
    return Coverage(from_=CUTOFF - timedelta(hours=24), to=CUTOFF, complete=True, detail="whole window")


def decisions() -> CharterDecisionsInput:
    def decision(did: str, kind: str, target: int, *, effect: str, applied: str | None) -> StagedDecision:
        return StagedDecision(
            decision_id=did, run_id="run-1", action_id=did, role="flow", action_kind=kind,
            binding="advisory", outcome="executed", reason_code="advisory_executes", reason="",
            effect=effect, execution_reason=None, target_number=target, anchor_issue_number=target,
            proposal_issue_number=None, decided_at=datetime.fromisoformat("2026-09-28T13:10:00+00:00"),
            applied_at=None if applied is None else datetime.fromisoformat(applied),
        )

    return CharterDecisionsInput(
        coverage=_coverage(),
        decisions=(
            decision("D1", "flag_pattern", 410, effect="withheld", applied=None),
            decision("D2", "release_withheld_review", 320, effect="applied", applied=D2_APPLIED),
        ),
    )


def case_files() -> CaseFilesInput:
    return CaseFilesInput(
        coverage=_coverage(),
        case_files=(
            StagedCaseFile(
                id="case-file:validation-retry-refused", signature="validation-retry-refused",
                issue_number=372, recorded_at=datetime.fromisoformat("2026-09-28T13:10:00+00:00"),
                observation_count=1, fix_class="code", area="", disposition="active",
                retirement_pending=False, body="#410's validation retry is refused every tick.",
                observations=(
                    CaseFileObservationInput(
                        observation_id="run-1:tl:A1",
                        recorded_at=datetime.fromisoformat("2026-09-28T13:10:00+00:00"),
                    ),
                ),
            ),
        ),
        diagnoses_coverage=Coverage(from_=None, to=CUTOFF, complete=False, detail="best-effort"),
        diagnoses=(
            StagedDiagnosis(
                id="tech-lead-run:run-1:tech-lead-1", run_key="global:health_review",
                scope_kind="global_health_review", flavor="health_review", phase="completed",
                started_at=datetime.fromisoformat("2026-09-28T13:00:00+00:00"),
                ended_at=datetime.fromisoformat("2026-09-28T13:20:00+00:00"),
                subject_issue_number=0, subject_title="", anchor_issue_number=418,
                body="#410's pending validation retry is refused once per tick.", findings=1, proposals=1,
            ),
        ),
    )


def build_improver_data(root: Path, *, exam_comparable: bool = False) -> Path:
    """Write the staged inputs under ``root/improver-data`` and return that directory."""
    data = root / "improver-data"
    data.mkdir(parents=True)
    previous, current = audits()

    def write(name: str, document: BaseModel) -> None:
        (data / name).write_text(to_json(document), encoding="utf-8")

    write("audit.json", (current))
    write("audit-previous.json", (previous))
    assert current.diff is not None
    write("audit-diff.json", (current.diff))
    write("engine-start.json", (EngineStartInput(
        started_at=STARTED, engine_commit=ENGINE_COMMIT, package_version="0.10.0", repo_head=None,
    )))
    write("charter.json", (TechLeadCharterPolicy.from_config(Config()).effective_charter()))
    write("charter-decisions.json", (decisions()))
    write("case-files.json", (case_files()))
    write("open-issues.json", (OpenIssuesInput(
        repo="issue-orchestrator/issue-orchestrator", read_at=CUTOFF,
        issues=(OpenIssue(number=OPEN_TRACKER, title="Fetch cost", labels=("bug",)),),
    )))
    (data / "exam").mkdir()
    source = data / "engine-source" / Path(SOURCE_FILE).parent
    source.mkdir(parents=True)
    (source / Path(SOURCE_FILE).name).write_text("# charter\n", encoding="utf-8")
    write("inputs.json", (InputsManifest(
        staged_at=CUTOFF, audited_repo="porchpin/porchpin",
        outputs_repo="issue-orchestrator/issue-orchestrator",
        inputs=(StagedInput(name="audit.json", staged=True, detail=""),),
        existing_exam_case_ids=EXAM_CASE_IDS,
        exam_scores_comparable=exam_comparable,
    )))
    return data


def example(name: str) -> dict:
    """``examples/improver/findings/<name>.json`` as data, for a test to mutate."""
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


EXAMPLE_NAMES = (
    "exam_case",
    "capability_issue",
    "charter_proposal",
    "prompt_proposal",
    "needs_investigation",
)


# -- fakes for the run and its effects (#7490 step 3b) -------------------------


class MemoryRunStore:
    """An :class:`~issue_orchestrator.ports.improver.ImproverRunStore` in a tmp dir."""

    def __init__(self, root: Path) -> None:
        from issue_orchestrator.execution.improver_run_store import FileImproverRunStore

        self._files = FileImproverRunStore(root)
        self.recorded: list = []

    def exclusive(self):  # type: ignore[no-untyped-def]
        return self._files.exclusive()

    def new_run_dir(self, run_id: str) -> Path:
        return self._files.new_run_dir(run_id)

    def record(self, run) -> None:  # type: ignore[no-untyped-def]
        self.recorded.append(run)
        self._files.record(run)

    def runs(self):  # type: ignore[no-untyped-def]
        return self._files.runs()

    def accepted_findings(self, run):  # type: ignore[no-untyped-def]
        return self._files.accepted_findings(run)


class FakeIssueHost:
    """The repository-host calls the improver's effects make, recorded."""

    def __init__(self, open_issues: list | None = None) -> None:
        from issue_orchestrator.ports.engine_audit import OpenIssueLabels

        self.open = list(open_issues or [OpenIssueLabels(number=OPEN_TRACKER, title="Fetch cost", labels=())])
        self.created: list[dict] = []
        self.comments: list[tuple[int, str]] = []
        self.fail_on_create: Exception | None = None
        #: Raise AFTER the issue is created: the POST landed, its result was lost.
        self.lose_create_result: Exception | None = None
        #: Newly created issues the open listing does not show yet.
        self.stale_listing = False
        self.create_calls = 0
        self.next_number = 9000

    def list_open_issue_labels_complete(self):  # type: ignore[no-untyped-def]
        created = {c["number"] for c in self.created}
        return [i for i in self.open if not (self.stale_listing and i.number in created)]

    def find_open_issue_by_marker(self, *, marker):  # type: ignore[no-untyped-def]
        """Uncached, by body: sees what the (stale, retitled) listing does not."""
        return next((c["number"] for c in self.created if marker in c["body"]), None)

    def find_issue_by_marker(self, *, title, marker, authoritative=False):  # type: ignore[no-untyped-def]
        assert authoritative
        return next((c["number"] for c in self.created if marker in c["body"]), None)

    def create_issue(self, title, body, labels=None, milestone=None):  # type: ignore[no-untyped-def]
        self.create_calls += 1
        if self.fail_on_create is not None:
            raise self.fail_on_create
        from issue_orchestrator.ports.engine_audit import OpenIssueLabels

        self.next_number += 1
        self.created.append({"title": title, "body": body, "labels": labels, "number": self.next_number})
        self.open.append(OpenIssueLabels(number=self.next_number, title=title, labels=tuple(labels or ())))
        if self.lose_create_result is not None:
            raise self.lose_create_result
        return {"number": self.next_number, "html_url": f"https://x/{self.next_number}"}

    def add_comment(self, issue_or_pr_number, body):  # type: ignore[no-untyped-def]
        self.comments.append((issue_or_pr_number, body))
        return "https://x/comment"

    def issue_comment_marker_present(self, issue_number, marker):  # type: ignore[no-untyped-def]
        return any(n == issue_number and marker in body for n, body in self.comments)
