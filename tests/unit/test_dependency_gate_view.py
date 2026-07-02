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
    stack_chip,
    stack_signal,
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


def test_normal_dependent_successor_preserves_normal_mode():
    # A plain ``Depends-on: #this`` dependent inverts to a NORMAL-mode successor
    # edge. The mode must survive into the payload so the drawer can label it a
    # dependent rather than a stack relationship.
    successors = [SuccessorEdge(issue_number=2, ref="#2", mode=DependencyMode.NORMAL)]
    dump = _project(build_gate_report(1, []), successors)
    assert dump["successors"] == [{"issue_number": 2, "ref": "#2", "mode": "normal"}]
    # A purely-normal dependent does not make the base a stack participant.
    assert dump["has_stack_edges"] is False


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


def _stack_view(pred_number: int, successors=()):
    dep = Dependency(
        issue_number=pred_number,
        mode=DependencyMode.STACK,
        state=DependencyState.UNSATISFIED,
    )
    return project_stack_dependency_view(1, build_gate_report(1, [dep]), successors)


def test_stack_signal_is_empty_without_stack_participation():
    dep = Dependency(
        issue_number=100, mode=DependencyMode.NORMAL, state=DependencyState.UNSATISFIED
    )
    view = project_stack_dependency_view(1, build_gate_report(1, [dep]), ())
    assert stack_signal(view) == ""


def test_stack_signal_changes_when_predecessor_ref_changes():
    # The compact chip renders "after #10" in its hover/title chain context, so a
    # predecessor moving from #10 to #11 (same gates, same successor count) must
    # change the signal — otherwise the reused card keeps stale chain text.
    assert stack_signal(_stack_view(10)) != stack_signal(_stack_view(11))


def test_projection_surfaces_tri_state_approval_freshness():
    dep = Dependency(issue_number=20, mode=DependencyMode.STACK,
                     state=DependencyState.SATISFIED)
    assert _project(build_gate_report(1, [dep], approval_current=True))["approval_freshness"] == "fresh"
    assert _project(build_gate_report(1, [dep], approval_current=False))["approval_freshness"] == "stale"
    # Unknown is surfaced explicitly — the merge gate is not rendered fresh.
    unknown = _project(build_gate_report(1, [dep], approval_current=None))
    assert unknown["approval_freshness"] == "unknown"
    assert _gate(unknown, "merge")["open"] is True  # unknown never blocks
    assert unknown["stale"] is False  # unknown is not stale


def test_stack_signal_changes_when_successor_ref_changes():
    # The chip also renders "before #30"; a successor changing from #30 to #31
    # must likewise re-fingerprint the card.
    succ_a = (SuccessorEdge(issue_number=30, ref="#30", mode=DependencyMode.STACK),)
    succ_b = (SuccessorEdge(issue_number=31, ref="#31", mode=DependencyMode.STACK),)
    assert stack_signal(_stack_view(10, succ_a)) != stack_signal(_stack_view(10, succ_b))


def test_stack_chip_is_none_without_stack_participation():
    dep = Dependency(issue_number=100, mode=DependencyMode.NORMAL,
                     state=DependencyState.UNSATISFIED)
    view = project_stack_dependency_view(1, build_gate_report(1, [dep]), ())
    assert stack_chip(view) is None


def test_stack_chip_ready_when_all_gates_open():
    dep = Dependency(issue_number=20, mode=DependencyMode.STACK,
                     state=DependencyState.SATISFIED)
    chip = stack_chip(project_stack_dependency_view(1, build_gate_report(1, [dep]), ()))
    assert chip is not None
    assert chip.tone == "ok"
    assert chip.status_text == "ready"
    assert chip.mode_label == "Stack"


def test_stack_chip_blocked_counts_extra_gates():
    # Unsatisfied predecessor with no facts blocks all four gates; the chip shows
    # the first blocked gate plus a "+N" extras count, as text (not colour only).
    dep = Dependency(issue_number=20, mode=DependencyMode.STACK,
                     state=DependencyState.UNSATISFIED)
    chip = stack_chip(project_stack_dependency_view(1, build_gate_report(1, [dep]), ()))
    assert chip is not None
    assert chip.tone == "blocked"
    assert chip.status_text == "work +3 blocked"


def test_stack_chip_stale_takes_precedence_over_blocked():
    dep = Dependency(issue_number=20, mode=DependencyMode.STACK,
                     state=DependencyState.SATISFIED)
    view = project_stack_dependency_view(
        1, build_gate_report(1, [dep], approval_current=False), ()
    )
    chip = stack_chip(view)
    assert chip is not None
    assert chip.tone == "stale"
    assert chip.status_text == "stale"


def test_stack_chip_base_of_stack_shows_chain_context_in_title():
    successors = (SuccessorEdge(issue_number=30, ref="#30", mode=DependencyMode.STACK),)
    chip = stack_chip(project_stack_dependency_view(1, None, successors))
    assert chip is not None
    assert chip.mode_label == "Base"
    assert chip.status_text == "ready"
    assert "before #30" in chip.title
