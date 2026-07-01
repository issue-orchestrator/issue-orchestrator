"""Tests for the dependency gate snapshot owner (#6597).

The snapshot owner sits at the dependency-gate *policy* boundary: it evaluates
each in-scope issue's full four-gate report directly through the
:class:`DependencyEvaluator`, independent of the scheduler's availability
short-circuits. These tests cover the producer half of the boundary:

- every lane surfaces its gates, including a ``pr-pending`` / awaiting-merge
  successor the scheduler would short-circuit before dependency evaluation;
- the publish-time ancestry fact (from an active successor worktree) surfaces a
  stale successor branch; and
- the merge-time approval fact surfaces a stale own-approval.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
from issue_orchestrator.control.dependency_gate_snapshot import (
    DependencyGateSnapshotBuilder,
    build_refresh_snapshot,
)
from issue_orchestrator.domain.dependencies import DependencyMode, DependencyTarget
from issue_orchestrator.domain.dependency_gates import Gate, PredecessorFacts
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


class _FactsProvider:
    """Stack-predecessor facts provider returning canned facts per target."""

    def __init__(self, facts: dict[DependencyTarget, PredecessorFacts]):
        self._facts = facts

    def gather_facts(self, targets):
        return {t: self._facts[t] for t in targets if t in self._facts}


class _Ancestry:
    """Ancestry double: successor contains predecessor unless branch is stale."""

    def __init__(self, stale_branches: set[str] | None = None):
        self._stale = stale_branches or set()

    def successor_contains_predecessor(self, worktree, predecessor_branch) -> bool:
        return predecessor_branch not in self._stale


def _evaluator(checker, *, facts=None, ancestry=None) -> DependencyEvaluator:
    return DependencyEvaluator(
        issue_checker=checker,
        events=_Events(),
        predecessor_facts_provider=_FactsProvider(facts) if facts is not None else None,
        branch_ancestry=ancestry,
    )


def _builder(checker, **kwargs) -> DependencyGateSnapshotBuilder:
    return DependencyGateSnapshotBuilder(_evaluator(checker, **kwargs))


def test_snapshot_retains_only_reports_with_dependencies():
    checker = _Checker()
    checker.issues[100] = "open"  # unsatisfied normal dependency
    builder = _builder(checker)
    issues = [
        Issue(number=1, title="Independent", labels=[], body="No deps", milestone="M1"),
        Issue(number=2, title="Blocked", labels=[], body="Depends-on: #100", milestone="M1"),
    ]

    snapshot = builder.build(issues)

    # Only the issue that declares a dependency edge is stored.
    assert set(snapshot.reports) == {2}
    view = project_from_snapshot(snapshot, 2)
    assert view is not None
    assert view.mode == "normal"


def test_snapshot_inverts_stack_edges_into_successors():
    checker = _Checker()
    builder = _builder(checker)
    issues = [
        Issue(number=1, title="Base", labels=[], body="Base slice", milestone="M1"),
        Issue(number=2, title="Mid", labels=[], body="Stack-after: #1", milestone="M1"),
        Issue(number=3, title="Tip", labels=[], body="Stack-after: #2", milestone="M1"),
    ]

    snapshot = builder.build(issues)

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
    checker = _Checker()
    builder = _builder(checker)
    issues = [
        Issue(number=5, title="Self", labels=[], body="Depends-on: #5", milestone="M1"),
        Issue(number=6, title="Cross", labels=[], body="Depends-on: other/repo#1", milestone="M1"),
    ]

    snapshot = builder.build(issues)

    # Self-edge does not make an issue its own successor; cross-repo target is
    # skipped (best-effort same-repo chain context only).
    assert snapshot.successors_for(5) == ()
    assert dict(snapshot.successors) == {}


def test_snapshot_dedupes_duplicate_edges_to_same_target():
    checker = _Checker()
    builder = _builder(checker)
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

    snapshot = builder.build(issues)

    assert [e.issue_number for e in snapshot.successors_for(1)] == [2]


def test_issue_without_edges_is_not_stored():
    # An issue with a body but no dependency edges yields an all-open, edge-less
    # report that renders no stack section, so it must not be stored.
    checker = _Checker()
    builder = _builder(checker)
    issues = [Issue(number=9, title="Plain", labels=[], body="Just prose", milestone="M1")]

    snapshot = builder.build(issues)

    assert snapshot.reports == {}


def test_builder_without_evaluator_projects_successors_only():
    # No evaluator (e.g. dependencies disabled) still surfaces chain context but
    # carries no gate reports.
    builder = DependencyGateSnapshotBuilder(None)
    issues = [
        Issue(number=1, title="Base", labels=[], body="Base", milestone="M1"),
        Issue(number=2, title="Mid", labels=[], body="Stack-after: #1", milestone="M1"),
    ]

    snapshot = builder.build(issues)

    assert snapshot.reports == {}
    assert [e.issue_number for e in snapshot.successors_for(1)] == [2]


def test_pr_pending_stack_successor_still_surfaces_gates():
    # An awaiting-merge successor carries a pr-pending label the scheduler would
    # short-circuit on before dependency evaluation, dropping its report. The
    # snapshot owner evaluates every lane, so its stack gates still surface — the
    # merge gate (most relevant here) included.
    checker = _Checker()
    checker.issues[10] = "open"  # predecessor not yet merged
    facts = {
        DependencyTarget(10): PredecessorFacts(
            branch_usable=True, validation_passed=True, agent_reviewed=True,
            branch_name="10-base",
        )
    }
    builder = _builder(checker, facts=facts)
    successor = Issue(
        number=2, title="Awaiting merge", labels=["pr-pending"],
        body="Stack-after: #10", milestone="M1",
    )

    snapshot = builder.build([successor])

    view = project_from_snapshot(snapshot, 2)
    assert view is not None
    assert view.has_stack_edges is True
    gates = {g.gate: g.open for g in view.gates}
    # Predecessor branch is ready, so work/publish open, but merge stays ordered
    # behind the predecessor's own merge.
    assert gates[Gate.WORK.value] is True
    assert gates[Gate.MERGE.value] is False
    assert Gate.MERGE.value in view.blocked_gates
    assert "predecessor_not_merged" in view.blocked_reason_codes


def test_stale_publish_ancestry_surfaces_predecessor_branch_advanced():
    # An active successor whose branch no longer descends from the current
    # predecessor head must show a stale publish gate. Ancestry needs the
    # successor's worktree, supplied here via worktrees_by_issue.
    checker = _Checker()
    checker.issues[10] = "open"
    facts = {
        DependencyTarget(10): PredecessorFacts(
            branch_usable=True, validation_passed=True, agent_reviewed=True,
            branch_name="10-base",
        )
    }
    builder = _builder(checker, facts=facts, ancestry=_Ancestry(stale_branches={"10-base"}))
    successor = Issue(
        number=2, title="Stale slice", labels=[], body="Stack-after: #10", milestone="M1",
    )

    snapshot = builder.build([successor], worktrees_by_issue={2: Path("/wt/2")})

    view = project_from_snapshot(snapshot, 2)
    assert view is not None
    assert view.stale is True
    assert "predecessor_branch_advanced" in view.stale_reason_codes
    publish = next(g for g in view.gates if g.gate == Gate.PUBLISH.value)
    assert publish.open is False
    assert "predecessor_branch_advanced" in publish.reason_codes


def test_no_worktree_keeps_publish_ancestry_at_contained_default():
    # Without an active worktree the ancestry check cannot run, so publish keeps
    # its contained default (the same stance the live publish gate takes with no
    # working copy) rather than a fabricated stale/open verdict.
    checker = _Checker()
    checker.issues[10] = "open"
    facts = {
        DependencyTarget(10): PredecessorFacts(
            branch_usable=True, validation_passed=True, agent_reviewed=True,
            branch_name="10-base",
        )
    }
    builder = _builder(checker, facts=facts, ancestry=_Ancestry(stale_branches={"10-base"}))
    successor = Issue(
        number=2, title="No worktree", labels=[], body="Stack-after: #10", milestone="M1",
    )

    snapshot = builder.build([successor])  # no worktrees_by_issue

    view = project_from_snapshot(snapshot, 2)
    assert view is not None
    assert view.stale is False
    publish = next(g for g in view.gates if g.gate == Gate.PUBLISH.value)
    assert publish.open is True


def test_stale_approval_surfaces_approval_stale_on_merge_gate():
    # A slice whose reviewed commit is no longer its head re-blocks only the
    # merge gate. The snapshot owner threads the per-issue approval fact through.
    checker = _Checker()
    checker.issues[10] = "closed"  # predecessor merged: merge otherwise open
    builder = _builder(checker)
    successor = Issue(
        number=2, title="Stale approval", labels=[], body="Stack-after: #10", milestone="M1",
    )

    snapshot = builder.build([successor], approval_current_by_issue={2: False})

    view = project_from_snapshot(snapshot, 2)
    assert view is not None
    assert view.stale is True
    assert view.approval_freshness == "stale"
    assert "approval_stale" in view.stale_reason_codes
    merge = next(g for g in view.gates if g.gate == Gate.MERGE.value)
    assert merge.open is False
    assert "approval_stale" in merge.reason_codes


def test_absent_approval_is_modelled_unknown_not_fresh():
    # The reviewer's core F1 concern: an issue absent from the approval map must
    # NOT be silently rendered fresh. It is modelled explicitly as unknown, so an
    # awaiting-merge successor whose predecessor merged shows merge open but its
    # approval freshness unverified — never a fabricated "fresh".
    checker = _Checker()
    checker.issues[10] = "closed"  # predecessor merged: merge otherwise open
    builder = _builder(checker)
    successor = Issue(
        number=2, title="Unverified approval", labels=[], body="Stack-after: #10", milestone="M1",
    )

    snapshot = builder.build([successor])  # no approval map at all

    view = project_from_snapshot(snapshot, 2)
    assert view is not None
    assert view.approval_freshness == "unknown"
    assert view.stale is False  # unknown is not stale
    merge = next(g for g in view.gates if g.gate == Gate.MERGE.value)
    assert merge.open is True  # unknown does not block


class _ApprovalFreshnessSource:
    """Approval-freshness double: canned per-issue tri-state verdicts."""

    def __init__(self, verdicts):
        self._verdicts = verdicts

    def approval_current_for(self, issues):
        return {i.number: self._verdicts[i.number] for i in issues if i.number in self._verdicts}


def test_refresh_path_without_source_reports_unknown_not_fresh():
    # Production boundary: build_refresh_snapshot is what the refresh loop calls.
    # With no approval-freshness source wired (today's reality), an awaiting-merge
    # stacked successor's merge approval is reported unknown, never fresh.
    checker = _Checker()
    checker.issues[10] = "closed"
    evaluator = _evaluator(checker)
    successor = Issue(
        number=2, title="Awaiting merge", labels=["pr-pending"],
        body="Stack-after: #10", milestone="M1",
    )

    snapshot = build_refresh_snapshot(evaluator, [successor], active_sessions=[])

    view = project_from_snapshot(snapshot, 2)
    assert view is not None
    assert view.approval_freshness == "unknown"


def test_refresh_path_surfaces_stale_from_source():
    # Production boundary: when an ApprovalFreshnessSource answers False for a
    # stacked awaiting-merge successor, build_refresh_snapshot threads it through
    # and the projected merge gate renders approval_stale.
    checker = _Checker()
    checker.issues[10] = "closed"  # predecessor merged: merge otherwise open
    evaluator = _evaluator(checker)
    successor = Issue(
        number=2, title="Awaiting merge", labels=["pr-pending"],
        body="Stack-after: #10", milestone="M1",
    )
    source = _ApprovalFreshnessSource({2: False})

    snapshot = build_refresh_snapshot(
        evaluator, [successor], active_sessions=[], approval_freshness=source
    )

    view = project_from_snapshot(snapshot, 2)
    assert view is not None
    assert view.approval_freshness == "stale"
    merge = next(g for g in view.gates if g.gate == Gate.MERGE.value)
    assert merge.open is False
    assert "approval_stale" in merge.reason_codes
