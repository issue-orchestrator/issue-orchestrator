"""Tests for the dependency gate snapshot owner (#6597).

Covers both halves of the producer boundary:
- the scheduler retains the gate report it evaluated on each decision, and
- ``build_dependency_gate_snapshot`` turns decisions + issue bodies into the
  stored snapshot (reports keyed by issue, inverted successor edges).
"""

from __future__ import annotations

from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
from issue_orchestrator.control.dependency_gate_snapshot import (
    build_dependency_gate_snapshot,
)
from issue_orchestrator.control.scheduler import IssueAvailabilityDecision, Scheduler
from issue_orchestrator.domain.dependencies import DependencyMode
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.view_models.dependency_gate import project_from_snapshot


class _Checker:
    def __init__(self):
        self.issues: dict[int, str] = {}

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        return self.issues.get(issue_number)

    def get_issue_milestone(self, issue_number: int, repo: str | None = None) -> str | None:
        return "M1"


class _Events:
    def publish(self, event):  # pragma: no cover - trivial sink
        pass


def _scheduler(checker) -> Scheduler:
    from issue_orchestrator.infra.config import Config

    evaluator = DependencyEvaluator(issue_checker=checker, events=_Events())
    return Scheduler(config=Config(), dependency_evaluator=evaluator)


def test_scheduler_retains_gate_report_for_dependency_edges():
    checker = _Checker()
    checker.issues[100] = "open"  # unsatisfied normal dependency
    scheduler = _scheduler(checker)
    issues = [
        Issue(number=1, title="Independent", labels=[], body="No deps", milestone="M1"),
        Issue(number=2, title="Blocked", labels=[], body="Depends-on: #100", milestone="M1"),
    ]

    decisions = {d.issue.number: d for d in scheduler.evaluate_issues(issues)}

    # The dependency-blocked issue keeps its evaluated report...
    blocked = decisions[2]
    assert blocked.reason == "dependency_blocked"
    assert blocked.gate_report is not None
    assert blocked.gate_report.can_start_work is False
    # ...and an issue with no edges has no report to retain.
    assert decisions[1].gate_report is not None
    assert decisions[1].gate_report.dependencies == ()


def test_snapshot_retains_only_reports_with_dependencies():
    checker = _Checker()
    checker.issues[100] = "open"
    scheduler = _scheduler(checker)
    issues = [
        Issue(number=1, title="Independent", labels=[], body="No deps", milestone="M1"),
        Issue(number=2, title="Blocked", labels=[], body="Depends-on: #100", milestone="M1"),
    ]
    decisions = scheduler.evaluate_issues(issues)

    snapshot = build_dependency_gate_snapshot(decisions, issues)

    # Only the issue that declares a dependency edge is stored.
    assert set(snapshot.reports) == {2}
    view = project_from_snapshot(snapshot, 2)
    assert view is not None
    assert view.mode == "normal"


def test_snapshot_inverts_stack_edges_into_successors():
    issues = [
        Issue(number=1, title="Base", labels=[], body="Base slice", milestone="M1"),
        Issue(number=2, title="Mid", labels=[], body="Stack-after: #1", milestone="M1"),
        Issue(number=3, title="Tip", labels=[], body="Stack-after: #2", milestone="M1"),
    ]

    snapshot = build_dependency_gate_snapshot([], issues)

    base_successors = snapshot.successors_for(1)
    assert [(e.issue_number, e.mode) for e in base_successors] == [(2, DependencyMode.STACK)]
    assert [e.issue_number for e in snapshot.successors_for(2)] == [3]
    assert snapshot.successors_for(3) == ()
    # A base with only successors still projects a chain-context view.
    view = project_from_snapshot(snapshot, 1)
    assert view is not None
    assert view.has_stack_edges is True
    assert [s.issue_number for s in view.successors] == [2]


def test_snapshot_skips_cross_repo_and_self_edges():
    issues = [
        Issue(number=5, title="Self", labels=[], body="Depends-on: #5", milestone="M1"),
        Issue(number=6, title="Cross", labels=[], body="Depends-on: other/repo#1", milestone="M1"),
    ]

    snapshot = build_dependency_gate_snapshot([], issues)

    # Self-edge does not make an issue its own successor; cross-repo target is
    # skipped (best-effort same-repo chain context only).
    assert snapshot.successors_for(5) == ()
    assert dict(snapshot.successors) == {}


def test_snapshot_dedupes_duplicate_edges_to_same_target():
    issues = [
        Issue(number=1, title="Base", labels=[], body="Base", milestone="M1"),
        Issue(
            number=2,
            title="Dup",
            labels=[],
            body="Depends-on: #1\nStack-after: #1",
            milestone="M1",
        ),
    ]

    snapshot = build_dependency_gate_snapshot([], issues)

    assert [e.issue_number for e in snapshot.successors_for(1)] == [2]


def test_decision_without_report_is_ignored_by_snapshot():
    # A short-circuited decision (e.g. in-progress) carries no report and must
    # not appear in the stored reports map.
    issue = Issue(number=9, title="Active", labels=[], body="Depends-on: #1", milestone="M1")
    decision = IssueAvailabilityDecision(
        issue=issue, available=False, reason="in_progress_active_session"
    )

    snapshot = build_dependency_gate_snapshot([decision], [issue])

    assert snapshot.reports == {}
