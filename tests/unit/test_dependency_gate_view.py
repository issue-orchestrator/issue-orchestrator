"""Producer-to-contract tests for the stack dependency gate projection (#6597).

These assert the projection from the domain ``DependencyGateReport`` (the
producer) onto the public ``StackDependencyGateView`` contract — the boundary
the dashboard and issue detail consume. Every projected view is also validated
against the strict generated OpenAPI model so the two contract systems cannot
drift.
"""

from __future__ import annotations

from issue_orchestrator.contracts.ui_openapi_models import (
    StackDependencyGateViewPayload,
)
from issue_orchestrator.domain.dependencies import (
    Dependency,
    DependencyMode,
    DependencyState,
    DependencyTarget,
    EdgeProblem,
)
from issue_orchestrator.domain.dependency_gates import (
    PredecessorFacts,
    SuccessorEdge,
    build_gate_report,
)
from issue_orchestrator.view_models.dependency_gate import (
    project_from_snapshot,
    project_stack_dependency_view,
)
from issue_orchestrator.domain.dependency_gates import DependencyGateSnapshot


def _gate(view_dump: dict, gate: str) -> dict:
    return next(g for g in view_dump["gates"] if g["gate"] == gate)


def _project(report, successors=()):
    view = project_stack_dependency_view(report.issue_number, report, successors)
    dump = view.model_dump(mode="json")
    # The projection must satisfy the strict (extra="forbid") OpenAPI contract
    # exactly — same keys, no extras — so the issue-detail payload validates.
    StackDependencyGateViewPayload.model_validate(dump)
    return dump


def test_no_dependencies_all_gates_open_mode_none():
    dump = _project(build_gate_report(42, []))
    assert dump["mode"] == "none"
    assert dump["has_stack_edges"] is False
    assert dump["predecessors"] == []
    assert dump["blocked_gates"] == []
    assert dump["stale"] is False
    assert all(g["open"] for g in dump["gates"])
    assert [g["gate"] for g in dump["gates"]] == ["work", "review", "publish", "merge"]


def test_normal_open_dependency_blocks_every_gate_with_code():
    dep = Dependency(issue_number=100, mode=DependencyMode.NORMAL,
                     state=DependencyState.UNSATISFIED)
    dump = _project(build_gate_report(2, [dep]))
    assert dump["mode"] == "normal"
    assert dump["has_stack_edges"] is False
    assert set(dump["blocked_gates"]) == {"work", "review", "publish", "merge"}
    assert dump["blocked_reason_codes"] == ["dependency_open"]
    # Predecessor edge carries mode + state for distinct display.
    assert dump["predecessors"] == [
        {"ref": "#100", "mode": "normal", "state": "unsatisfied", "problem": None}
    ]


def test_stack_predecessor_without_facts_blocks_stack_gates():
    dep = Dependency(issue_number=10, mode=DependencyMode.STACK,
                     state=DependencyState.UNSATISFIED)
    dump = _project(build_gate_report(20, [dep]))
    assert dump["mode"] == "stack"
    assert dump["has_stack_edges"] is True
    # work/review/publish blocked on unusable branch; merge on not-merged.
    for gate in ("work", "review", "publish"):
        assert "predecessor_branch_unusable" in _gate(dump, gate)["reason_codes"]
    assert _gate(dump, "merge")["reason_codes"] == ["predecessor_not_merged"]
    # Human phrasing is present for the drawer.
    assert any("usable base" in r for r in _gate(dump, "work")["reasons"])


def test_stack_successor_ready_to_work_but_ordered_behind_merge():
    dep = Dependency(issue_number=10, mode=DependencyMode.STACK,
                     state=DependencyState.UNSATISFIED)
    facts = {DependencyTarget(issue_number=10): PredecessorFacts(
        branch_usable=True, validation_passed=True, agent_reviewed=True,
        branch_name="feat/base", head_sha="abc",
    )}
    dump = _project(build_gate_report(20, [dep], facts))
    assert _gate(dump, "work")["open"] is True
    assert _gate(dump, "review")["open"] is True
    assert _gate(dump, "publish")["open"] is True
    assert _gate(dump, "merge")["open"] is False
    assert dump["blocked_gates"] == ["merge"]
    assert dump["stack_base_branch"] == "feat/base"
    assert dump["stale"] is False


def test_predecessor_branch_advanced_is_stale_and_blocks_publish_merge():
    dep = Dependency(issue_number=10, mode=DependencyMode.STACK,
                     state=DependencyState.UNSATISFIED)
    facts = {DependencyTarget(issue_number=10): PredecessorFacts(
        branch_usable=True, validation_passed=True, agent_reviewed=True,
        branch_name="feat/base", head_sha="abc", contained_in_successor=False,
    )}
    dump = _project(build_gate_report(20, [dep], facts))
    assert dump["stale"] is True
    assert dump["stale_reason_codes"] == ["predecessor_branch_advanced"]
    assert set(dump["blocked_gates"]) == {"publish", "merge"}
    assert "predecessor_branch_advanced" in _gate(dump, "publish")["reason_codes"]


def test_stale_own_approval_marks_stale_and_blocks_merge():
    dep = Dependency(issue_number=10, mode=DependencyMode.STACK,
                     state=DependencyState.UNSATISFIED)
    facts = {DependencyTarget(issue_number=10): PredecessorFacts(
        branch_usable=True, validation_passed=True, agent_reviewed=True,
        branch_name="feat/base", head_sha="abc",
    )}
    dump = _project(build_gate_report(20, [dep], facts, approval_current=False))
    assert dump["stale"] is True
    assert "approval_stale" in dump["stale_reason_codes"]
    assert "approval_stale" in _gate(dump, "merge")["reason_codes"]


def test_structural_problem_surfaces_on_predecessor_edge():
    dep = Dependency(issue_number=20, mode=DependencyMode.STACK,
                     state=DependencyState.UNSATISFIED,
                     problem=EdgeProblem.CYCLE)
    dump = _project(build_gate_report(20, [dep]))
    assert dump["predecessors"][0]["problem"] == "cycle"
    assert set(dump["blocked_gates"]) == {"work", "review", "publish", "merge"}
    assert "cycle" in dump["blocked_reason_codes"]


def test_successor_edges_render_chain_context():
    dep = Dependency(issue_number=10, mode=DependencyMode.STACK,
                     state=DependencyState.UNSATISFIED)
    successors = [SuccessorEdge(issue_number=30, ref="#30", mode=DependencyMode.STACK)]
    dump = _project(build_gate_report(20, [dep]), successors)
    assert dump["successors"] == [{"issue_number": 30, "ref": "#30", "mode": "stack"}]


def test_base_of_stack_with_no_report_still_shows_successors():
    # An issue that is the base of a stack has no dependency report of its own,
    # but must still surface its successors as chain context.
    snapshot = DependencyGateSnapshot(
        reports={},
        successors={7: (SuccessorEdge(issue_number=8, ref="#8", mode=DependencyMode.STACK),)},
    )
    view = project_from_snapshot(snapshot, 7)
    assert view is not None
    dump = view.model_dump(mode="json")
    StackDependencyGateViewPayload.model_validate(dump)
    assert dump["mode"] == "none"
    assert dump["has_stack_edges"] is True  # participates via a stack successor
    assert dump["gates"] == []
    assert dump["successors"] == [{"issue_number": 8, "ref": "#8", "mode": "stack"}]


def test_snapshot_without_report_or_successors_projects_none():
    assert project_from_snapshot(DependencyGateSnapshot(), 99) is None
